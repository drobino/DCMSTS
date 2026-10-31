import pytest
from sqlalchemy.exc import DatabaseError

from conftest import PASSWORD, login, make_asset, make_user, make_vendor
from dcmsts.models import AuditLog, db, utcnow
from dcmsts.security import password_problems, verify_audit_chain
from dcmsts.services.workorders import create_work_order


def test_tc07_unauthorized_admin_access_denied_and_logged(client):
    tech = make_user("Technician")
    login(client, tech)
    resp = client.get("/admin/users")
    assert resp.status_code == 403
    entry = AuditLog.query.filter_by(action="access_denied").one()
    assert entry.user_id == tech.user_id and entry.after_value["path"] == "/admin/users"


def test_anonymous_redirected_to_login(client):
    resp = client.get("/assets/")
    assert resp.status_code == 302 and "/login" in resp.headers["Location"]


def test_lockout_after_repeated_failures(client, app):
    tech = make_user("Technician")
    for _ in range(app.config["LOGIN_MAX_FAILURES"]):
        client.post("/login", data={"email": tech.email, "password": "wrong-password-1"})
    db.session.refresh(tech)
    assert tech.locked_until and tech.locked_until > utcnow()
    resp = login(client, tech)  # correct password still refused while locked
    assert resp.status_code == 401
    assert AuditLog.query.filter_by(action="login_failed").count() == app.config["LOGIN_MAX_FAILURES"] + 1
    assert client.get("/").status_code == 302


def test_manager_requires_mfa_enrolment(client):
    mgr = make_user("Manager", mfa=False)
    mgr.mfa_enabled, mgr.mfa_secret = False, None
    db.session.commit()
    resp = client.post("/login", data={"email": mgr.email, "password": PASSWORD})
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/mfa/setup")
    assert client.get("/").status_code == 302  # not signed in until MFA completes


def test_mfa_wrong_code_rejected(client):
    mgr = make_user("Manager")
    client.post("/login", data={"email": mgr.email, "password": PASSWORD})
    client.post("/mfa", data={"code": "000000"})
    assert client.get("/").status_code == 302
    assert AuditLog.query.filter_by(action="mfa_failed").count() == 1


def test_role_change_invalidates_sessions(client, app):
    admin = make_user("Administrator", "a@t.local")
    tech = make_user("Technician")
    other = app.test_client()
    login(other, tech)
    assert other.get("/").status_code == 200
    login(client, admin)
    resp = client.post(f"/admin/users/{tech.user_id}", data={"role_id": tech.role_id, "status": "suspended"})
    assert resp.status_code == 302
    assert other.get("/").status_code == 302
    assert AuditLog.query.filter_by(action="permission_change").one().before_value["status"] == "active"


def test_last_admin_cannot_be_removed(client):
    admin = make_user("Administrator")
    login(client, admin)
    from dcmsts.security import role_by_name
    client.post(f"/admin/users/{admin.user_id}", data={"role_id": role_by_name("Manager").role_id, "status": "active"})
    db.session.refresh(admin)
    assert admin.role_name == "Administrator"


def test_idle_timeout(client, app):
    tech = make_user("Technician")
    login(client, tech)
    with client.session_transaction() as s:
        s["seen"] = s["seen"] - app.config["IDLE_TIMEOUT_MINUTES"] * 60 - 5
    resp = client.get("/")
    assert resp.status_code == 302 and "expired=1" in resp.headers["Location"]


def test_password_policy():
    assert password_problems("short1")
    assert password_problems("onlyletterslong")
    assert not password_problems("LongEnough123")


def test_passwords_hashed_with_salt(app):
    a = make_user("Technician", "a@t.local")
    b = make_user("Technician", "b@t.local")
    assert PASSWORD not in a.password_hash and a.password_hash != b.password_hash


def test_csrf_enforced(app):
    app.config["WTF_CSRF_ENABLED"] = True
    tech = make_user("Technician")
    c = app.test_client()
    resp = c.post("/login", data={"email": tech.email, "password": PASSWORD})
    assert resp.status_code == 400


def test_security_headers(client):
    resp = client.get("/login")
    assert "default-src 'self'" in resp.headers["Content-Security-Policy"]
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"


def test_tc09_script_in_note_is_escaped(client):
    tech = make_user("Technician")
    asset = make_asset()
    wo = create_work_order(asset=asset, type="corrective", priority="low", title="t")
    wo.assigned_to, wo.status = tech.user_id, "assigned"
    db.session.commit()
    login(client, tech)
    client.post(f"/work-orders/{wo.work_order_id}/log", data={"note": "<script>alert(1)</script>"})
    html = client.get(f"/work-orders/{wo.work_order_id}").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_audit_log_is_append_only_and_chained(client):
    tech = make_user("Technician")
    login(client, tech)
    client.get("/admin/users")
    assert verify_audit_chain() == (True, None)
    with pytest.raises(DatabaseError):
        db.session.execute(db.text("UPDATE audit_log SET action='x'"))
        db.session.commit()
    db.session.rollback()
    with pytest.raises(DatabaseError):
        db.session.execute(db.text("DELETE FROM audit_log"))
        db.session.commit()
    db.session.rollback()


def test_audit_chain_detects_tampering(client):
    tech = make_user("Technician")
    login(client, tech)
    client.get("/admin/users")
    db.session.execute(db.text("DROP TRIGGER audit_no_update"))
    db.session.execute(db.text("UPDATE audit_log SET action='benign' WHERE action='access_denied'"))
    db.session.commit()
    ok, bad = verify_audit_chain()
    assert not ok and bad is not None


def test_technician_cannot_open_others_work_order(client):
    t1 = make_user("Technician", "t1@t.local")
    t2 = make_user("Technician", "t2@t.local")
    wo = create_work_order(asset=make_asset(), type="corrective", priority="low", title="x")
    wo.assigned_to = t1.user_id
    db.session.commit()
    login(client, t2)
    assert client.get(f"/work-orders/{wo.work_order_id}").status_code == 403


def test_vendor_sees_only_own_orders(client):
    v = make_vendor()
    vu = make_user("Vendor", "v@t.local", vendor=v)
    mine = create_work_order(asset=make_asset(), type="corrective", priority="low", title="mine")
    other = create_work_order(asset=make_asset("UPS-2"), type="corrective", priority="low", title="other")
    mine.vendor_id = v.vendor_id
    db.session.commit()
    login(client, vu)
    assert client.get(f"/work-orders/{mine.work_order_id}").status_code == 200
    assert client.get(f"/work-orders/{other.work_order_id}").status_code == 403
    assert client.get("/assets/").status_code == 403


def test_open_redirect_blocked(client):
    tech = make_user("Technician")
    resp = client.post("/login?next=//evil.example", data={"email": tech.email, "password": PASSWORD})
    assert resp.headers["Location"] == "/"

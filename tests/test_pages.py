"""Render every screen for each role against the demo dataset (catches template and permission regressions)."""
from datetime import date

import pyotp
import pytest

from dcmsts.models import Asset, Prediction, User, WorkOrder, db
from dcmsts.seed import seed_demo
from dcmsts.services.predictive import score_assets

PW = "DemoPass12345"


@pytest.fixture
def demo(app):
    seed_demo(PW, date.today())
    for u in User.query.all():
        if u.role_name in ("Administrator", "Manager"):
            u.mfa_secret, u.mfa_enabled = pyotp.random_base32(), True
    db.session.commit()
    score_assets()
    return app


def _login(client, email):
    u = User.query.filter_by(email=email).one()
    client.post("/login", data={"email": email, "password": PW})
    if u.mfa_enabled:
        client.post("/mfa", data={"code": pyotp.TOTP(u.mfa_secret).now()})
    return u


def _urls():
    asset = Asset.query.first()
    wo = WorkOrder.query.filter(WorkOrder.status != "completed").first()
    tech = User.query.filter_by(email="esi@dcmsts.local").one()
    pred = Prediction.query.first()
    return [
        "/", "/calendar", "/calendar?status=overdue", "/notifications", "/password",
        "/work-orders/?status=open", "/work-orders/new", f"/work-orders/{wo.work_order_id}",
        f"/work-orders/{wo.work_order_id}/assign",
        "/assets/", "/assets/new", "/assets/import", f"/assets/{asset.asset_id}", f"/assets/{asset.asset_id}/edit",
        "/schedules/", "/schedules/new",
        "/predictive/", "/predictive/queue", f"/predictive/predictions/{pred.prediction_id}", "/predictive/models",
        "/predictive/ingest",
        "/people/technicians", f"/people/technicians/{tech.user_id}", "/people/vendors", "/people/vendors/new",
        "/reports/", "/reports/?type=compliance", "/reports/?type=sla", "/reports/?type=downtime",
        "/admin/users", "/admin/users/new", "/admin/settings", "/admin/sla", "/admin/audit", "/admin/notifications",
    ]


def test_admin_and_manager_render_all_pages(demo):
    for email in ("admin@dcmsts.local", "manager@dcmsts.local"):
        c = demo.test_client()
        u = _login(c, email)
        for url in _urls():
            resp = c.get(url)
            expected = {200}
            if url.startswith("/admin/") and not url.startswith("/admin/sla") and u.role_name == "Manager":
                expected = {403}
            assert resp.status_code in expected, (email, url, resp.status_code)


def test_technician_and_vendor_scoped(demo):
    c = demo.test_client()
    tech = _login(c, "esi@dcmsts.local")
    for url in ("/", "/calendar", "/work-orders/", "/assets/", "/work-orders/new"):
        assert c.get(url).status_code == 200, url
    for url in ("/schedules/", "/predictive/", "/reports/", "/admin/users", "/people/technicians"):
        assert c.get(url).status_code == 403, url
    mine = WorkOrder.query.filter_by(assigned_to=tech.user_id).first()
    assert c.get(f"/work-orders/{mine.work_order_id}").status_code == 200

    v = demo.test_client()
    _login(v, "vendor@coolair.example")
    assert v.get("/").status_code == 200 and v.get("/calendar").status_code == 200
    assert v.get("/assets/").status_code == 403


def test_health_endpoint(client):
    assert client.get("/healthz").json == {"status": "ok"}


def test_calendar_date_picker_jumps_to_month_and_highlights_day(demo):
    client = demo.test_client()
    _login(client, "manager@dcmsts.local")
    html = client.get("/calendar?date=2025-03-15").get_data(as_text=True)
    assert "March 2025" in html and 'value="2025-03-15"' in html and " selected" in html
    # the date wins over year/month, and filters are kept on the jump form
    html = client.get("/calendar?year=2024&month=1&date=2025-03-15&status=overdue").get_data(as_text=True)
    assert "March 2025" in html and 'name="status" value="overdue"' in html
    # garbage or out-of-range dates fall back to the current month instead of erroring
    this_month = date.today().strftime("%B %Y")
    for bad in ("not-a-date", "0001-01-01", "2025-13-40", "9999-12-31"):
        resp = client.get(f"/calendar?date={bad}")
        assert resp.status_code == 200 and this_month in resp.get_data(as_text=True)



def test_manager_adds_technician_from_people_page(demo):
    from dcmsts.models import AuditLog, Role
    client = demo.test_client()
    _login(client, "manager@dcmsts.local")
    assert "Add technician" in client.get("/people/technicians").get_data(as_text=True)
    assert client.get("/people/technicians/new").status_code == 200
    admin_role = Role.query.filter_by(role_name="Administrator").one()
    # a smuggled role_id is ignored: this route only ever creates technicians
    resp = client.post("/people/technicians/new", data={
        "full_name": "Ama Owusu", "email": "Ama@dcmsts.local", "skills": "electrical, ups",
        "password": "NewTechPass123", "role_id": admin_role.role_id})
    assert resp.status_code == 302 and "/people/technicians/" in resp.headers["Location"]
    ama = User.query.filter_by(email="ama@dcmsts.local").one()
    assert ama.role_name == "Technician" and ama.skills == "electrical, ups"
    assert "Ama Owusu" in client.get("/people/technicians").get_data(as_text=True)
    entry = AuditLog.query.filter_by(entity_id=ama.user_id, action="create").one()
    assert entry.user_id == User.query.filter_by(email="manager@dcmsts.local").one().user_id
    # the new technician can sign in
    tc = demo.test_client()
    assert tc.post("/login", data={"email": "ama@dcmsts.local", "password": "NewTechPass123"}).status_code == 302
    assert tc.get("/").status_code == 200


def test_add_technician_validation_and_limits(demo):
    client = demo.test_client()
    _login(client, "manager@dcmsts.local")
    before = User.query.count()
    for data, msg in [
        ({"full_name": "X", "email": "esi@dcmsts.local", "password": "NewTechPass123"}, "already in use"),
        ({"full_name": "X", "email": "x@dcmsts.local", "password": "short1"}, "Password needs"),
        ({"full_name": "X", "email": "not-an-email", "password": "NewTechPass123"}, "Email is invalid"),
    ]:
        resp = client.post("/people/technicians/new", data=data, follow_redirects=True)
        assert msg in resp.get_data(as_text=True)
    assert User.query.count() == before
    # managers still cannot use the general user form (other roles stay admin-only)
    assert client.get("/admin/users/new").status_code == 403
    # technicians and vendors cannot add technicians
    for email in ("esi@dcmsts.local", "vendor@coolair.example"):
        c = demo.test_client()
        _login(c, email)
        assert c.get("/people/technicians/new").status_code == 403

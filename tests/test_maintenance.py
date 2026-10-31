from datetime import date, timedelta

import pytest

from conftest import default_sla, login, make_asset, make_user, make_vendor
from dcmsts.models import MaintSchedule, Notification, SlaPolicy, TaskLog, WorkOrder, db, utcnow
from dcmsts.services import jobs
from dcmsts.services.scheduling import RuleError, generate_instances, occurrences, parse_rule
from dcmsts.services.workorders import (RuleViolation, assign, candidates, change_status, create_work_order,
                                        reschedule)

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


# ---------- TC-01 scheduling ----------

def test_tc01_monthly_schedule_generates_correct_due_dates(client):
    mgr = make_user("Manager")
    asset = make_asset()
    login(client, mgr)
    start = date.today()
    resp = client.post("/schedules/new", data={
        "asset_id": asset.asset_id, "title": "UPS monthly", "freq": "MONTHLY", "interval": "1",
        "start_date": start.isoformat(), "priority": "medium", "checklist": "Check batteries\nCheck alarms"})
    assert resp.status_code == 302
    wos = WorkOrder.query.order_by(WorkOrder.due_date).all()
    expected = list(occurrences("FREQ=MONTHLY;INTERVAL=1", start, start + timedelta(days=90)))
    assert [w.due_date.date() for w in wos] == expected
    assert all(w.type == "preventive" and len(w.checklist) == 2 for w in wos)


def test_month_end_dates_clamp():
    got = list(occurrences("FREQ=MONTHLY;INTERVAL=1", date(2026, 1, 31), date(2026, 4, 30)))
    assert got == [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31), date(2026, 4, 30)]


def test_invalid_rules_rejected():
    with pytest.raises(RuleError):
        parse_rule("FREQ=HOURLY")
    with pytest.raises(RuleError):
        parse_rule("FREQ=DAILY;INTERVAL=0")


def test_schedule_end_before_start_rejected_without_partial_save(client):
    login(client, make_user("Manager"))
    asset = make_asset()
    client.post("/schedules/new", data={
        "asset_id": asset.asset_id, "title": "x", "freq": "WEEKLY", "interval": "1", "start_date": "2026-05-01",
        "end_date": "2026-04-01", "priority": "low", "checklist": "a"})
    assert MaintSchedule.query.count() == 0 and WorkOrder.query.count() == 0


def test_schedule_requires_checklist_and_confirms_overlap(client):
    login(client, make_user("Manager"))
    asset = make_asset()
    base = {"asset_id": asset.asset_id, "title": "x", "freq": "WEEKLY", "interval": "1",
            "start_date": date.today().isoformat(), "priority": "low"}
    client.post("/schedules/new", data={**base, "checklist": ""})
    assert MaintSchedule.query.count() == 0
    client.post("/schedules/new", data={**base, "checklist": "a"})
    client.post("/schedules/new", data={**base, "checklist": "b"})  # overlap without confirmation
    assert MaintSchedule.query.count() == 1
    client.post("/schedules/new", data={**base, "checklist": "b", "confirm_overlap": "yes"})
    assert MaintSchedule.query.count() == 2


def test_generation_is_idempotent(app):
    asset = make_asset()
    s = MaintSchedule(asset_id=asset.asset_id, title="w", recurrence_rule="FREQ=WEEKLY;INTERVAL=1",
                      checklist_template=["a"], start_date=date.today(), next_due_date=date.today())
    s.asset = asset
    db.session.add(s)
    db.session.flush()
    first = generate_instances(s, date.today(), 28)
    again = generate_instances(s, date.today(), 28)
    assert len(first) == 5 and again == []


# ---------- TC-02 assignment ----------

def _wo(asset, priority="high", skill=None, due=None):
    wo = create_work_order(asset=asset, type="corrective", priority=priority, title=f"job {priority}",
                           required_skill=skill, due_date=due or utcnow() + timedelta(hours=5))
    db.session.commit()
    return wo


def test_tc02_conflict_flagged_and_alternative_suggested(app):
    busy = make_user("Technician", "busy@t.local", skills="electrical")
    free = make_user("Technician", "free@t.local", skills="electrical")
    mgr = make_user("Manager")
    asset = make_asset()
    existing = _wo(asset)
    existing.assigned_to, existing.status = busy.user_id, "assigned"
    db.session.commit()
    new = _wo(asset, skill="electrical")
    c = candidates(new)
    assert c["technicians"][0]["user"].user_id == free.user_id
    busy_entry = next(x for x in c["technicians"] if x["user"].user_id == busy.user_id)
    assert busy_entry["conflicts"]
    with pytest.raises(RuleViolation):
        assign(new, mgr, technician=busy)  # overlapping high-priority without override
    assign(new, mgr, technician=busy, override_reason="only one on site")
    assert new.status == "assigned"
    assert Notification.query.filter_by(user_id=busy.user_id, message_type="assignment").count() == 1


def test_expired_certification_removes_skill_match(app):
    make_user("Technician", "x@t.local", skills="fire",
              certs=[{"name": "fire", "expires": (date.today() - timedelta(days=1)).isoformat()}])
    wo = _wo(make_asset(), skill="fire")
    c = candidates(wo)
    assert c["technicians"] == [] and c["no_qualified"]


def test_vendor_requires_valid_contract(app):
    mgr = make_user("Manager")
    wo = _wo(make_asset())
    with pytest.raises(RuleViolation):
        assign(wo, mgr, vendor=make_vendor(valid=False))
    assign(wo, mgr, vendor=make_vendor(valid=True))
    assert wo.vendor_id and wo.assigned_to is None


def test_assignment_outside_suggestions_needs_reason(app):
    mgr = make_user("Manager")
    unskilled = make_user("Technician", "u@t.local", skills="network")
    wo = _wo(make_asset(), skill="hvac")
    with pytest.raises(RuleViolation):
        assign(wo, mgr, technician=unskilled)
    assign(wo, mgr, technician=unskilled, override_reason="training")
    assert "override: training" in TaskLog.query.filter_by(action="assigned").one().notes


# ---------- TC-03 completion rules ----------

def test_tc03_completion_blocked_without_checklist_and_evidence(client):
    tech = make_user("Technician")
    mgr = make_user("Manager")
    asset = make_asset(category="UPS")  # UPS requires evidence by default
    wo = create_work_order(asset=asset, type="preventive", priority="medium", title="pm",
                           checklist=[{"item": "Check batteries", "done": False}], due_date=utcnow() + timedelta(days=1))
    wo.assigned_to, wo.status = tech.user_id, "assigned"
    db.session.commit()
    login(client, tech)
    url = f"/work-orders/{wo.work_order_id}"
    client.post(url + "/status", data={"status": "in_progress"})
    client.post(url + "/status", data={"status": "completed"})
    db.session.refresh(wo)
    assert wo.status == "in_progress"
    client.post(url + "/checklist", data={"done": ["0"]})
    client.post(url + "/status", data={"status": "completed"})
    db.session.refresh(wo)
    assert wo.status == "in_progress"  # still missing evidence
    resp = client.post(url + "/evidence", data={"file": (__import__("io").BytesIO(PNG), "photo.png")},
                       content_type="multipart/form-data")
    assert resp.status_code == 302
    client.post(url + "/status", data={"status": "completed", "root_cause": "routine"})
    db.session.refresh(wo)
    assert wo.status == "completed" and wo.completed_at
    assert Notification.query.filter_by(user_id=mgr.user_id, message_type="task_completed").count() == 1


def test_evidence_content_type_checked(client):
    tech = make_user("Technician")
    wo = create_work_order(asset=make_asset(), type="corrective", priority="low", title="x")
    wo.assigned_to, wo.status = tech.user_id, "assigned"
    db.session.commit()
    login(client, tech)
    client.post(f"/work-orders/{wo.work_order_id}/evidence",
                data={"file": (__import__("io").BytesIO(b"<html>"), "fake.png")}, content_type="multipart/form-data")
    assert wo.evidence == []


def test_blocking_requires_reason_and_notifies_manager(app):
    tech = make_user("Technician")
    mgr = make_user("Manager")
    wo = _wo(make_asset())
    wo.assigned_to, wo.status = tech.user_id, "assigned"
    with pytest.raises(RuleViolation):
        change_status(wo, tech, "blocked")
    change_status(wo, tech, "blocked", reason="missing part")
    assert Notification.query.filter_by(user_id=mgr.user_id, message_type="task_blocked").count() == 1


def test_parts_log_locked_after_24h_for_non_admin(client):
    tech = make_user("Technician")
    wo = create_work_order(asset=make_asset(), type="corrective", priority="low", title="x")
    wo.assigned_to, wo.status = tech.user_id, "in_progress"
    db.session.commit()
    login(client, tech)
    client.post(f"/work-orders/{wo.work_order_id}/log", data={"part_name": "fuse", "quantity": "2"})
    part = wo.parts[0]
    part.logged_at = utcnow() - timedelta(hours=25)
    db.session.commit()
    assert client.post(f"/work-orders/{wo.work_order_id}/parts/{part.part_use_id}/delete").status_code == 403


# ---------- TC-04 alerts & escalation ----------

def test_tc04_overdue_marked_and_escalated(app):
    admin = make_user("Administrator", "boss@t.local")
    mgr = make_user("Manager")
    vendor = make_vendor()
    default_sla(chain=[admin.user_id], final_vendor=vendor.vendor_id)
    tech = make_user("Technician")
    wo = _wo(make_asset(), priority="high", due=utcnow() - timedelta(minutes=5))
    wo.assigned_to, wo.status = tech.user_id, "assigned"
    db.session.commit()
    now = utcnow()
    jobs.run_all(now)
    db.session.refresh(wo)
    assert wo.is_overdue and wo.escalation_level == 1
    assert Notification.query.filter_by(user_id=mgr.user_id, message_type="escalation").count() == 1
    jobs.run_all(now + timedelta(minutes=10))  # within escalation window: no repeat
    assert Notification.query.filter_by(message_type="escalation").count() == 1
    jobs.run_all(now + timedelta(hours=2, minutes=1))
    assert Notification.query.filter_by(user_id=admin.user_id, message_type="escalation").count() == 1
    jobs.run_all(now + timedelta(hours=4, minutes=2))
    draft = WorkOrder.query.filter(WorkOrder.title.like("[Vendor escalation]%")).one()
    assert draft.status == "unassigned"  # drafted, awaiting manager confirmation


def test_reminder_sent_once_before_due(app):
    tech = make_user("Technician")
    wo = _wo(make_asset(), due=utcnow() + timedelta(hours=3))
    wo.assigned_to, wo.status = tech.user_id, "assigned"
    db.session.commit()
    jobs.run_all()
    jobs.run_all()
    assert Notification.query.filter_by(user_id=tech.user_id, message_type="reminder").count() == 1


def test_sla_change_does_not_affect_in_flight_orders(app):
    default_sla()
    wo = _wo(make_asset(), priority="high", due=None)
    SlaPolicy.query.filter_by(priority="high").one().resolution_hours = 1
    db.session.commit()
    later = _wo(make_asset("UPS-2"), priority="high", due=None)
    assert wo.sla_resolution_hours == 24 and later.sla_resolution_hours == 1


def test_failed_delivery_retries_then_logs(app):
    from dcmsts.services.notify import dispatch_pending, notify
    tech = make_user("Technician")
    notify(tech, "reminder", "s", "b")
    db.session.commit()

    def broken(*_):
        raise OSError("smtp down")

    now = utcnow()
    for i in range(5):
        dispatch_pending(now + timedelta(hours=i * 2), sender=broken)
    n = Notification.query.one()
    assert n.status == "failed" and n.attempts == 5 and "smtp down" in n.last_error


def test_reschedule_limit_requires_note(app):
    mgr = make_user("Manager")
    wo = _wo(make_asset())
    for _ in range(2):
        reschedule(wo, mgr, wo.due_date + timedelta(days=1))
    with pytest.raises(RuleViolation):
        reschedule(wo, mgr, wo.due_date + timedelta(days=1))
    reschedule(wo, mgr, wo.due_date + timedelta(days=1), note="supplier delay")
    assert wo.reschedule_count == 3


def test_technician_reschedule_is_a_request(client):
    tech = make_user("Technician")
    make_user("Manager")
    wo = _wo(make_asset())
    wo.assigned_to, wo.status = tech.user_id, "assigned"
    db.session.commit()
    original = wo.due_date
    login(client, tech)
    client.post(f"/work-orders/{wo.work_order_id}/reschedule", data={"due_date": "2030-01-01T10:00", "note": "x"})
    db.session.refresh(wo)
    assert wo.due_date == original
    assert Notification.query.filter_by(message_type="change_request").count() == 1


# ---------- assets ----------

def test_duplicate_asset_tag_rejected(client):
    login(client, make_user("Administrator"))
    make_asset("UPS-1")
    resp = client.post("/assets/new", data={"asset_tag": "UPS-1", "name": "dup", "category": "UPS"})
    assert resp.status_code == 409


def test_asset_csv_import_all_or_nothing(client):
    from dcmsts.models import Asset
    login(client, make_user("Administrator"))
    bad = "asset_tag,name,category\nA1,One,UPS\nA1,Dup,UPS\n"
    client.post("/assets/import", data={"file": (__import__("io").BytesIO(bad.encode()), "a.csv")},
                content_type="multipart/form-data")
    assert Asset.query.count() == 0
    good = "asset_tag,name,category,install_date\nA1,One,UPS,2021-01-01\nA2,Two,PDU,\n"
    client.post("/assets/import", data={"file": (__import__("io").BytesIO(good.encode()), "a.csv")},
                content_type="multipart/form-data")
    assert Asset.query.count() == 2


def test_retiring_asset_deactivates_schedules(client):
    login(client, make_user("Administrator"))
    asset = make_asset()
    s = MaintSchedule(asset_id=asset.asset_id, title="w", recurrence_rule="FREQ=WEEKLY;INTERVAL=1",
                      checklist_template=["a"], start_date=date.today(), next_due_date=date.today())
    db.session.add(s)
    db.session.commit()
    client.post(f"/assets/{asset.asset_id}/status", data={"lifecycle_status": "retired"})
    db.session.refresh(s)
    assert not s.active


def test_asset_with_history_cannot_be_hard_deleted(app):
    from sqlalchemy.exc import IntegrityError
    asset = make_asset()
    _wo(asset)
    with pytest.raises(IntegrityError):
        db.session.execute(db.text("DELETE FROM asset WHERE asset_id = :id"), {"id": asset.asset_id})
        db.session.commit()
    db.session.rollback()

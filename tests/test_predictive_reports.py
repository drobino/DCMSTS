import io
import zipfile
from datetime import date, timedelta

import pytest

from conftest import login, make_asset, make_user
from dcmsts.models import (ModelVersion, Notification, Prediction, SensorReading, WorkOrder, db,
                           utcnow)
from dcmsts.services import predictive as pm
from dcmsts.services.backup import create_backup, restore_backup, verify_backup
from dcmsts.services.ingest import ingest_telemetry_csv, rollback_batch
from dcmsts.services.reports import compliance_report, downtime_report, sla_report, to_xlsx


def _readings(asset, temp, load, days=40):
    now = utcnow()
    for d in range(0, days, 5):
        for m, v in (("temperature", temp), ("load_pct", load)):
            db.session.add(SensorReading(asset_id=asset.asset_id, metric_name=m, metric_value=v, source="manual",
                                         recorded_at=now - timedelta(days=d)))


def _corrective(asset, days_ago, hours=4, planned=False, end=True):
    start = utcnow() - timedelta(days=days_ago)
    db.session.add(WorkOrder(asset_id=asset.asset_id, type="corrective", priority="high", title="fault",
                             status="completed" if end else "in_progress", created_at=start,
                             due_date=start + timedelta(hours=24), completed_at=start + timedelta(hours=hours) if end else None,
                             sla_resolution_hours=24, downtime_start=start,
                             downtime_end=start + timedelta(hours=hours) if end else None, downtime_planned=planned,
                             downtime_cause="overheating"))


# ---------- TC-08 predictive ----------

def test_tc08_degrading_asset_scores_higher_with_alert_and_factors(app):
    mgr = make_user("Manager")
    healthy, sick = make_asset("OK-1"), make_asset("BAD-1")
    _readings(healthy, 24, 55)
    _readings(sick, 34, 85)
    _corrective(sick, 20)
    _corrective(sick, 50)
    db.session.commit()
    preds = {p.asset.asset_tag: p for p in pm.score_assets()}
    assert float(preds["BAD-1"].risk_score) > float(preds["OK-1"].risk_score)
    assert float(preds["BAD-1"].risk_score) >= 0.6
    factors = " ".join(f["text"] for f in preds["BAD-1"].contributing_factors)
    assert "temperature" in factors.lower()
    alert = Notification.query.filter_by(user_id=mgr.user_id, message_type="predictive_alert").one()
    assert "BAD-1" in alert.subject
    assert preds["BAD-1"].recommended_window_end <= date.today() + timedelta(days=7)


def test_sparse_data_marked_low_confidence(app):
    make_asset("NEW-1")
    p = pm.score_assets()[0]
    assert p.low_confidence


def test_rescoring_supersedes_pending(app):
    make_asset()
    first = pm.score_assets()[0]
    pm.score_assets()
    db.session.refresh(first)
    assert first.reviewer_decision == "superseded"
    assert Prediction.query.filter_by(reviewer_decision="pending").count() == 1


def test_review_approve_creates_work_order_and_dismiss_needs_note(app):
    mgr = make_user("Manager")
    make_asset()
    p = pm.score_assets()[0]
    with pytest.raises(ValueError):
        pm.review(p, mgr, "dismissed")
    wo = pm.review(p, mgr, "approved")
    assert wo.prediction_id == p.prediction_id and wo.status == "unassigned"
    with pytest.raises(ValueError):
        pm.review(p, mgr, "approved")  # already reviewed


def test_adjust_records_reason_and_window(app):
    mgr = make_user("Manager")
    make_asset()
    p = pm.score_assets()[0]
    s, e = date.today() + timedelta(days=3), date.today() + timedelta(days=5)
    wo = pm.review(p, mgr, "adjusted", note="align with downtime slot", start=s, end=e)
    assert p.adjusted_window_end == e and p.reviewer_decision == "adjusted"
    assert wo.due_date.date() == e and p.review_note == "align with downtime slot"


def test_queue_empty_state_and_bulk_dismiss(client):
    mgr = make_user("Manager")
    login(client, mgr)
    assert "No pending recommendations" in client.get("/predictive/queue").get_data(as_text=True)
    sick = make_asset("BAD-1")
    _readings(sick, 36, 90)
    _corrective(sick, 10)
    db.session.commit()
    p = pm.score_assets()[0]
    client.post("/predictive/queue/bulk-dismiss", data={"ids": [p.prediction_id], "note": "insufficient data"})
    db.session.refresh(p)
    assert p.reviewer_decision == "dismissed"
    assert Prediction.query.count() == 1  # dismissed retained, not deleted


def test_training_with_insufficient_data_fails_cleanly(app):
    make_asset()
    with pytest.raises(pm.TrainingError):
        pm.train(None)
    assert ModelVersion.query.count() == 0


def test_training_on_demo_history_creates_unpromoted_candidate(app):
    from dcmsts.seed import seed_demo
    seed_demo("DemoPass12345", date.today())
    mv = pm.train(None)
    assert mv.status == "candidate"
    assert pm.active_model()[0] == "rules-v1"  # never auto-promoted
    assert 0 <= mv.metrics["recall"] <= 1 and mv.metrics["auc"] > 0.5
    pm.promote(mv, None)
    assert pm.active_model()[0] == mv.version
    second = ModelVersion(version="lr-x", status="candidate", params=mv.params, metrics={})
    db.session.add(second)
    db.session.commit()
    pm.promote(second, None)
    pm.promote(mv, None)  # rollback to the earlier version
    assert pm.active_model()[0] == mv.version and second.status == "retired"


def test_features_do_not_leak_future(app):
    asset = make_asset()
    _corrective(asset, 5)
    db.session.commit()
    hist = pm.load_history()
    values, _ = pm.features_at(asset, hist[asset.asset_id], utcnow() - timedelta(days=10))
    assert values["corrective_90d"] == 0


# ---------- UC-15 ingestion ----------

def test_telemetry_ingest_quarantines_and_rolls_back(app):
    user = make_user("Manager")
    make_asset("UPS-1")
    rows = ["asset_tag,metric_name,metric_value,recorded_at"] + [
        f"UPS-1,temperature,{20 + i},2026-01-0{i + 1}T10:00:00" for i in range(9)] + ["UPS-1,temperature,999,2026-01-10T10:00"]
    batch = ingest_telemetry_csv("\n".join(rows), "t.csv", user)
    assert batch.status == "loaded" and batch.loaded == 9 and batch.rejected == 1
    assert "outside" in batch.errors[0]["error"]
    rollback_batch(batch, user)
    assert SensorReading.query.count() == 0 and batch.status == "rolled_back"


def test_telemetry_ingest_halts_on_high_invalid_ratio(app):
    user = make_user("Manager")
    make_asset("UPS-1")
    text = "asset_tag,metric_name,metric_value,recorded_at\nUPS-1,temperature,22,2026-01-01T00:00\nNOPE,temperature,1,x\nUPS-1,bogus,1,2026-01-01\n"
    batch = ingest_telemetry_csv(text, "bad.csv", user)
    assert batch.status == "halted" and SensorReading.query.count() == 0


# ---------- TC-05 / UC-18 reports ----------

def _pm(asset, due, completed):
    db.session.add(WorkOrder(asset_id=asset.asset_id, type="preventive", priority="medium", title="pm",
                             status="completed" if completed else "assigned", due_date=due, completed_at=completed,
                             created_at=due - timedelta(days=30), sla_resolution_hours=None))


def test_tc05_compliance_report_matches_data(app):
    a = make_asset()
    now = utcnow()
    _pm(a, now - timedelta(days=10), now - timedelta(days=11))  # on time
    _pm(a, now - timedelta(days=20), now - timedelta(days=15))  # late
    _pm(a, now - timedelta(days=5), None)                        # overdue
    db.session.commit()
    r = compliance_report(date.today() - timedelta(days=30), date.today())
    assert r["rows"] == [["UPS", 3, 2, 1, 1, "67%"]]
    assert r["summary"]["On-time rate"] == "33%"


def test_downtime_report_mttr_mtbf_planned_and_ongoing(app):
    a = make_asset()
    _corrective(a, 10, hours=2)
    _corrective(a, 20, hours=4)
    _corrective(a, 15, hours=8, planned=True)
    _corrective(a, 1, end=False)
    db.session.commit()
    start, end = date.today() - timedelta(days=30), date.today()
    r = downtime_report(start, end)
    row = r["rows"][0]
    assert row[2] == 2 and row[3] == 6.0 and row[4] == 3.0  # failures, unplanned hours, MTTR
    period_h = (31 * 24) - (1 / 3600)  # inclusive day range ends at 23:59:59.999999
    assert abs(row[5] - round((period_h - 6) / 2, 1)) <= 0.1
    assert row[6] == 8.0 and row[7] == 1  # planned separate; ongoing excluded from MTTR


def test_sla_report_counts_breaches(app):
    a = make_asset()
    _corrective(a, 3, hours=2)    # within 24h
    _corrective(a, 4, hours=30)   # breached
    db.session.commit()
    r = sla_report(date.today() - timedelta(days=7), date.today())
    assert r["rows"][0][:5] == ["high", 2, 2, 1, 1]


def test_empty_range_message_and_exports(client):
    login(client, make_user("Manager"))
    html = client.get("/reports/?type=compliance&start=2001-01-01&end=2001-02-01").get_data(as_text=True)
    assert "No maintenance data" in html
    a = make_asset()
    _corrective(a, 3)
    db.session.commit()
    x = client.get("/reports/export.xlsx?type=downtime")
    assert x.status_code == 200 and x.data[:2] == b"PK"
    p = client.get("/reports/export.pdf?type=downtime")
    assert p.status_code == 200 and p.data[:5] == b"%PDF-"
    assert client.get("/reports/export.xlsx?type=compliance&start=2001-01-01&end=2001-02-01").status_code == 404


def test_xlsx_neutralises_formula_injection():
    from openpyxl import load_workbook
    data = to_xlsx({"title": "t", "summary": {}, "columns": ["c"], "rows": [["=HYPERLINK(\"x\")"], [-5]]})
    ws = load_workbook(io.BytesIO(data)).active
    values = [r[0] for r in ws.iter_rows(values_only=True)]
    assert "'=HYPERLINK(\"x\")" in values and -5 in values


def test_technician_cannot_view_reports(client):
    login(client, make_user("Technician"))
    assert client.get("/reports/").status_code == 403


# ---------- TC-10 backup ----------

def test_tc10_backup_verify_restore_roundtrip(app, tmp_path):
    make_asset("KEEP-1")
    db.session.commit()
    db_file = app.config["SQLALCHEMY_DATABASE_URI"].replace("sqlite:///", "")
    (tmp_path / "uploads").mkdir(exist_ok=True)
    (tmp_path / "uploads" / "e.png").write_bytes(b"\x89PNG data")
    path = create_backup(db_file, str(tmp_path / "uploads"), str(tmp_path / "bk"), retention=2)
    ok, detail = verify_backup(path)
    assert ok, detail
    restore_backup(path, str(tmp_path / "restore" / "r.db"), str(tmp_path / "restore" / "up"))
    import sqlite3
    conn = sqlite3.connect(tmp_path / "restore" / "r.db")
    assert conn.execute("SELECT asset_tag FROM asset").fetchall() == [("KEEP-1",)]
    conn.close()
    assert (tmp_path / "restore" / "up" / "e.png").read_bytes() == b"\x89PNG data"


def test_corrupted_backup_detected_and_retention(app, tmp_path):
    db_file = app.config["SQLALCHEMY_DATABASE_URI"].replace("sqlite:///", "")
    paths = [create_backup(db_file, None, str(tmp_path / "bk"), retention=2) for _ in range(3)]
    assert len(list((tmp_path / "bk").glob("*.zip"))) == 2
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(paths[-1]) as src, zipfile.ZipFile(bad, "w") as dst:
        for item in src.infolist():
            data = src.read(item)
            dst.writestr(item, data + b"x" if item.filename == "dcmsts.db" else data)
    ok, detail = verify_backup(bad)
    assert not ok and "checksum" in detail

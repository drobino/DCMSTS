"""CSV ingestion for telemetry (UC-15) and assets (UC-12 alternate flow)."""
import csv
import io
from datetime import date, datetime

from ..models import METRICS, Asset, IngestBatch, SensorReading, db, get_setting
from ..security import audit

METRIC_RANGES = {"temperature": (-40, 120), "load_pct": (0, 150), "runtime_hours": (0, 500000), "vibration": (0, 1000)}
MAX_ROWS = 50000


def _rows(file_text):
    reader = csv.DictReader(io.StringIO(file_text))
    reader.fieldnames = [f.strip().lower() for f in (reader.fieldnames or [])]
    return reader


def validate_reading(asset_by_tag, tag, metric, value, recorded_at):
    asset = asset_by_tag.get((tag or "").strip())
    if asset is None:
        return None, "unknown asset_tag"
    metric = (metric or "").strip().lower()
    if metric not in METRICS:
        return None, f"unknown metric '{metric}'"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None, "value is not a number"
    lo, hi = METRIC_RANGES[metric]
    if not lo <= v <= hi:
        return None, f"value {v} outside {lo}..{hi}"
    try:
        ts = datetime.fromisoformat((recorded_at or "").strip())
    except ValueError:
        return None, "recorded_at is not ISO-8601"
    return {"asset_id": asset.asset_id, "metric_name": metric, "metric_value": v, "recorded_at": ts.replace(tzinfo=None)}, None


def ingest_telemetry_csv(file_text, filename, actor):
    """Load valid rows, quarantine invalid ones; halt if the invalid ratio exceeds the configured limit."""
    asset_by_tag = {a.asset_tag: a for a in Asset.query.all()}
    good, errors, total = [], [], 0
    for i, row in enumerate(_rows(file_text), start=2):
        total += 1
        if total > MAX_ROWS:
            errors.append({"line": i, "error": f"file exceeds {MAX_ROWS} rows"})
            break
        rec, err = validate_reading(asset_by_tag, row.get("asset_tag"), row.get("metric_name"),
                                    row.get("metric_value"), row.get("recorded_at"))
        if err:
            errors.append({"line": i, "error": err})
        else:
            good.append(rec)
    batch = IngestBatch(source="csv_import", filename=(filename or "")[:200], rejected=len(errors),
                        errors=errors[:200], created_by=actor.user_id)
    ratio = len(errors) / total if total else 1.0
    if total == 0 or ratio > get_setting("telemetry_max_invalid_ratio"):
        batch.status, batch.loaded = "halted", 0
    else:
        batch.status, batch.loaded = "loaded", len(good)
    db.session.add(batch)
    db.session.flush()
    if batch.status == "loaded":
        for rec in good:
            db.session.add(SensorReading(source="csv_import", batch_id=batch.batch_id, **rec))
    audit("ingest", "INGEST_BATCH", batch.batch_id,
          after={"status": batch.status, "loaded": batch.loaded, "rejected": batch.rejected, "file": batch.filename})
    db.session.commit()
    return batch


def rollback_batch(batch, actor):
    if batch.status != "loaded":
        raise ValueError("Only loaded batches can be rolled back.")
    n = SensorReading.query.filter_by(batch_id=batch.batch_id).delete()
    batch.status = "rolled_back"
    audit("rollback", "INGEST_BATCH", batch.batch_id, after={"readings_removed": n})
    db.session.commit()
    return n


def _date(value):
    value = (value or "").strip()
    return date.fromisoformat(value) if value else None


def import_assets_csv(file_text, actor):
    """All-or-nothing import. Returns (created_count, errors)."""
    existing = {t for (t,) in db.session.query(Asset.asset_tag)}
    seen, new, errors = set(), [], []
    for i, row in enumerate(_rows(file_text), start=2):
        tag = (row.get("asset_tag") or "").strip()
        name = (row.get("name") or "").strip()
        category = (row.get("category") or "").strip()
        if not tag or not name or not category:
            errors.append(f"line {i}: asset_tag, name and category are required")
            continue
        if tag in existing or tag in seen:
            errors.append(f"line {i}: duplicate asset_tag '{tag}'")
            continue
        try:
            install, warranty = _date(row.get("install_date")), _date(row.get("warranty_expiry"))
        except ValueError:
            errors.append(f"line {i}: dates must be YYYY-MM-DD")
            continue
        seen.add(tag)
        new.append(Asset(asset_tag=tag[:50], name=name[:150], category=category[:80],
                         location=(row.get("location") or "").strip()[:150] or None,
                         manufacturer=(row.get("manufacturer") or "").strip()[:100] or None,
                         install_date=install, warranty_expiry=warranty))
    if errors:
        return 0, errors
    for a in new:
        db.session.add(a)
    db.session.flush()
    audit("import", "ASSET", after={"count": len(new), "tags": [a.asset_tag for a in new][:100]})
    db.session.commit()
    return len(new), []

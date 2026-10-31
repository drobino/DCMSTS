"""Predictive maintenance (UC-06, UC-16, UC-17).

Model = standardized features -> logistic regression. Parameters are stored as JSON on ModelVersion, so
scoring needs no deserialization of executable artifacts and every score can be explained by its terms.
"""
import math
from collections import defaultdict
from datetime import timedelta

from ..models import (Asset, ModelVersion, Prediction, SensorReading, WorkOrder, db, get_setting, utcnow)
from ..security import audit
from .notify import notify_role
from .workorders import create_work_order

HORIZON_DAYS = 30
FEATURES = ["age_years", "days_since_pm", "corrective_90d", "corrective_365d", "overdue_pm",
            "temperature", "load_pct", "runtime_khours", "warranty_expired"]
LABELS = {
    "age_years": "Asset age (years)",
    "days_since_pm": "Days since last completed preventive maintenance",
    "corrective_90d": "Corrective work orders in last 90 days",
    "corrective_365d": "Corrective work orders in last 12 months",
    "overdue_pm": "Overdue preventive tasks",
    "temperature": "Average temperature, last 30 days",
    "load_pct": "Average load %, last 30 days",
    "runtime_khours": "Runtime (thousand hours)",
    "warranty_expired": "Warranty expired (1 = yes)",
}
RULES_V1 = {
    "features": FEATURES,
    "mean": [5, 90, 0.3, 1.0, 0.2, 25, 60, 20, 0.3],
    "scale": [3, 60, 0.6, 1.2, 0.5, 4, 15, 15, 0.46],
    "coef": [0.4, 0.6, 0.7, 0.4, 0.6, 0.6, 0.5, 0.3, 0.3],
    "intercept": -2.0,
}


class TrainingError(RuntimeError):
    pass


# ---------- features ----------

def load_history(asset_ids=None):
    """Return {asset_id: {"wos": [...], "readings": [...]}} using two queries."""
    hist = defaultdict(lambda: {"wos": [], "readings": []})
    q = db.session.query(WorkOrder.asset_id, WorkOrder.type, WorkOrder.created_at, WorkOrder.due_date,
                         WorkOrder.completed_at)
    r = db.session.query(SensorReading.asset_id, SensorReading.metric_name, SensorReading.metric_value,
                         SensorReading.recorded_at)
    if asset_ids is not None:
        q = q.filter(WorkOrder.asset_id.in_(asset_ids))
        r = r.filter(SensorReading.asset_id.in_(asset_ids))
    for row in q:
        hist[row.asset_id]["wos"].append(row)
    for row in r:
        hist[row.asset_id]["readings"].append(row)
    return hist


def features_at(asset, history, t):
    """Feature values using only data observed at or before t (no label leakage)."""
    wos = [w for w in history["wos"] if w.created_at <= t]
    readings = [x for x in history["readings"] if x.recorded_at <= t]
    pm_done = [w.completed_at for w in wos if w.type == "preventive" and w.completed_at and w.completed_at <= t]
    if pm_done:
        days_since_pm = (t - max(pm_done)).days
    elif asset.install_date:
        days_since_pm = (t.date() - asset.install_date).days
    else:
        days_since_pm = 365
    corrective = [w.created_at for w in wos if w.type == "corrective"]

    def avg(metric, days):
        vals = [float(x.metric_value) for x in readings if x.metric_name == metric and x.recorded_at > t - timedelta(days=days)]
        return sum(vals) / len(vals) if vals else None

    runtime = sorted((x.recorded_at, float(x.metric_value)) for x in readings if x.metric_name == "runtime_hours")
    values = {
        "age_years": (t.date() - asset.install_date).days / 365.25 if asset.install_date else None,
        "days_since_pm": min(max(days_since_pm, 0), 365),
        "corrective_90d": sum(1 for c in corrective if c > t - timedelta(days=90)),
        "corrective_365d": sum(1 for c in corrective if c > t - timedelta(days=365)),
        "overdue_pm": sum(1 for w in wos if w.type == "preventive" and w.due_date < t
                          and (w.completed_at is None or w.completed_at > t)),
        "temperature": avg("temperature", 30),
        "load_pct": avg("load_pct", 30),
        "runtime_khours": runtime[-1][1] / 1000 if runtime else None,
        "warranty_expired": (1.0 if asset.warranty_expiry < t.date() else 0.0) if asset.warranty_expiry else None,
    }
    recent_readings = sum(1 for x in readings if x.recorded_at > t - timedelta(days=90))
    low_confidence = recent_readings < 3 or len(wos) < 2
    return values, low_confidence


def score(params, values):
    """Return (probability, factors) where factors are the top positive contributions in plain language."""
    logit = params["intercept"]
    terms = []
    for name, mean, scale, coef in zip(params["features"], params["mean"], params["scale"], params["coef"]):
        v = values.get(name)
        z = 0.0 if v is None else (v - mean) / (scale or 1.0)
        z = max(min(z, 6.0), -6.0)
        contribution = coef * z
        logit += contribution
        terms.append((contribution, name, v, mean))
    p = 1 / (1 + math.exp(-logit))
    terms.sort(reverse=True)
    factors = []
    for contribution, name, v, mean in terms[:3]:
        if contribution <= 0.05 or v is None:
            continue
        direction = "higher" if v > mean else "lower"
        factors.append({
            "feature": name, "value": round(v, 2), "typical": round(mean, 2), "weight": round(contribution, 3),
            "text": f"{LABELS[name]} is {round(v, 1)}, {direction} than typical ({round(mean, 1)}), which raises risk.",
        })
    if not factors:
        factors.append({"feature": None, "text": "No factor is notably above typical levels."})
    return p, factors


def recommended_window(p, today):
    if p >= get_setting("risk_threshold"):
        return today, today + timedelta(days=7)
    if p >= get_setting("review_threshold"):
        return today + timedelta(days=7), today + timedelta(days=30)
    return today + timedelta(days=30), today + timedelta(days=90)


def active_model():
    mv = ModelVersion.query.filter_by(status="production").first()
    return (mv.version, mv.params) if mv else ("rules-v1", RULES_V1)


# ---------- UC-06: scoring ----------

def score_assets(now=None, assets=None):
    now = now or utcnow()
    version, params = active_model()
    assets = assets or Asset.query.filter(Asset.lifecycle_status != "retired").all()
    hist = load_history([a.asset_id for a in assets])
    threshold = get_setting("risk_threshold")
    created = []
    for asset in assets:
        values, low_conf = features_at(asset, hist[asset.asset_id], now)
        p, factors = score(params, values)
        start, end = recommended_window(p, now.date())
        Prediction.query.filter_by(asset_id=asset.asset_id, reviewer_decision="pending").update(
            {"reviewer_decision": "superseded"})
        pred = Prediction(asset_id=asset.asset_id, model_version=version, risk_score=round(p, 4),
                          contributing_factors=factors, low_confidence=low_conf,
                          recommended_window_start=start, recommended_window_end=end, created_at=now)
        db.session.add(pred)
        db.session.flush()
        audit("create", "PREDICTION", pred.prediction_id,
              after={"asset": asset.asset_tag, "risk": round(p, 4), "model": version, "low_confidence": low_conf},
              user_id=None)
        if p >= threshold:
            notify_role("Manager", "predictive_alert", f"High failure risk: {asset.asset_tag}",
                        f"{asset.name} ({asset.asset_tag}) scored {p:.0%} failure risk"
                        f"{' (low confidence)' if low_conf else ''}. Recommended window {start} to {end}.\n"
                        + "\n".join(f"- {f['text']}" for f in factors))
        created.append(pred)
    db.session.commit()
    return created


def latest_predictions():
    sub = db.session.query(Prediction.asset_id, db.func.max(Prediction.created_at).label("mx")).group_by(
        Prediction.asset_id).subquery()
    return (Prediction.query.join(sub, (Prediction.asset_id == sub.c.asset_id) & (Prediction.created_at == sub.c.mx))
            .order_by(Prediction.risk_score.desc()).all())


def review_queue():
    return (Prediction.query.filter(Prediction.reviewer_decision == "pending",
                                    Prediction.risk_score >= get_setting("review_threshold"))
            .order_by(Prediction.risk_score.desc()).all())


# ---------- UC-17: review ----------

def review(pred, actor, decision, note=None, start=None, end=None):
    if pred.reviewer_decision != "pending":
        raise ValueError("This recommendation has already been reviewed.")
    if decision in ("dismissed", "adjusted") and not note:
        raise ValueError("A reason note is required.")
    if decision == "adjusted":
        if not start or not end or end < start:
            raise ValueError("A valid adjusted window is required.")
        pred.adjusted_window_start, pred.adjusted_window_end = start, end
    elif decision not in ("approved", "dismissed"):
        raise ValueError("Unknown decision.")
    before = {"decision": pred.reviewer_decision}
    pred.reviewer_decision, pred.review_note = decision, note
    pred.reviewer_id, pred.reviewed_at = actor.user_id, utcnow()
    wo = None
    if decision in ("approved", "adjusted"):
        due_day = pred.adjusted_window_end if decision == "adjusted" else pred.recommended_window_end
        from datetime import datetime, time
        priority = "high" if float(pred.risk_score) >= get_setting("risk_threshold") else "medium"
        wo = create_work_order(
            asset=pred.asset, type="preventive", priority=priority,
            title=f"Predictive inspection: {pred.asset.name}", due_date=datetime.combine(due_day, time(17, 0)),
            checklist=[{"item": "Inspect contributing factors listed in prediction", "done": False},
                       {"item": "Record readings (temperature, load, runtime)", "done": False}],
            description="\n".join(f.get("text", "") for f in pred.contributing_factors),
            created_by=actor, prediction=pred)
    audit("review", "PREDICTION", pred.prediction_id, before=before,
          after={"decision": decision, "note": note, "work_order": wo.work_order_id if wo else None})
    return wo


# ---------- UC-16: training ----------

def build_dataset(now, step_days=14):
    assets = Asset.query.all()
    hist = load_history()
    starts = [min([w.created_at for w in hist[a.asset_id]["wos"]] + [x.recorded_at for x in hist[a.asset_id]["readings"]] or [now])
              for a in assets]
    if not assets:
        return []
    t = min(starts) + timedelta(days=90)
    end = now - timedelta(days=HORIZON_DAYS)
    rows = []
    while t <= end:
        for a in assets:
            if a.install_date and a.install_date > t.date():
                continue
            values, _ = features_at(a, hist[a.asset_id], t)
            label = any(w.type == "corrective" and t < w.created_at <= t + timedelta(days=HORIZON_DAYS)
                        for w in hist[a.asset_id]["wos"])
            rows.append((t, values, int(label)))
        t += timedelta(days=step_days)
    return rows


def _matrix(rows, means):
    return [[means[i] if r[1][f] is None else r[1][f] for i, f in enumerate(FEATURES)] for r in rows]


def train(actor, now=None):
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import precision_score, recall_score, roc_auc_score

    now = now or utcnow()
    rows = build_dataset(now)
    if len(rows) < 50:
        raise TrainingError(f"Only {len(rows)} training snapshots available; at least 50 are needed.")
    times = sorted({r[0] for r in rows})
    cutoff = times[int(len(times) * 0.75)]
    train_rows = [r for r in rows if r[0] < cutoff]
    test_rows = [r for r in rows if r[0] >= cutoff]
    pos_train, pos_test = sum(r[2] for r in train_rows), sum(r[2] for r in test_rows)
    if pos_train < 5 or pos_test < 2 or pos_train == len(train_rows):
        raise TrainingError(f"Insufficient positive examples (train={pos_train}, holdout={pos_test}); "
                            "need at least 5 and 2 respectively, plus some negatives.")
    means, scales = [], []
    for f in FEATURES:
        vals = [r[1][f] for r in train_rows if r[1][f] is not None]
        m = sum(vals) / len(vals) if vals else 0.0
        sd = math.sqrt(sum((v - m) ** 2 for v in vals) / len(vals)) if vals else 1.0
        means.append(m)
        scales.append(sd or 1.0)

    def standardize(matrix):
        return [[(v - m) / s for v, m, s in zip(row, means, scales)] for row in matrix]

    X_train = standardize(_matrix(train_rows, means))
    y_train, y_test = [r[2] for r in train_rows], [r[2] for r in test_rows]
    clf = LogisticRegression(class_weight="balanced", max_iter=2000)
    clf.fit(X_train, y_train)
    params = {"features": FEATURES, "mean": means, "scale": scales,
              "coef": [float(c) for c in clf.coef_[0]], "intercept": float(clf.intercept_[0])}
    probs = [score(params, dict(zip(FEATURES, [None if r[1][f] is None else r[1][f] for f in FEATURES])))[0]
             for r in test_rows]
    threshold = 0.5
    preds = [int(p >= threshold) for p in probs]
    metrics = {
        "train_snapshots": len(train_rows), "holdout_snapshots": len(test_rows),
        "train_positives": pos_train, "holdout_positives": pos_test, "holdout_from": cutoff.date().isoformat(),
        "threshold": threshold,
        "precision": round(precision_score(y_test, preds, zero_division=0), 3),
        "recall": round(recall_score(y_test, preds, zero_division=0), 3),
        "auc": round(roc_auc_score(y_test, probs), 3) if 0 < pos_test < len(y_test) else None,
    }
    metrics["meets_recall_target"] = metrics["recall"] >= 0.8
    version = "lr-" + now.strftime("%Y%m%d%H%M%S")
    mv = ModelVersion(version=version, status="candidate", params=params, metrics=metrics,
                      trained_by=actor.user_id if actor else None, trained_at=now)
    db.session.add(mv)
    audit("create", "MODEL_VERSION", version, after=metrics)
    db.session.commit()
    return mv


def promote(mv, actor):
    if mv.status not in ("candidate", "retired"):
        raise ValueError("Only candidate or retired versions can be promoted.")
    current = ModelVersion.query.filter_by(status="production").first()
    if current:
        current.status = "retired"
    mv.status, mv.promoted_at = "production", utcnow()
    audit("promote", "MODEL_VERSION", mv.version, before={"production": current.version if current else "rules-v1"},
          after={"production": mv.version})
    db.session.commit()


def discard(mv, actor):
    if mv.status != "candidate":
        raise ValueError("Only candidate versions can be discarded.")
    mv.status = "discarded"
    audit("discard", "MODEL_VERSION", mv.version)
    db.session.commit()

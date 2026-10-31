from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from ..models import IngestBatch, ModelVersion, Prediction, db
from ..security import audit, require
from ..services import predictive as pm
from ..services.ingest import ingest_telemetry_csv, rollback_batch
from . import date_field, field

bp = Blueprint("predictive", __name__, url_prefix="/predictive")


@bp.get("/")
@require("predict.view")
def overview():
    version, _ = pm.active_model()
    return render_template("predictive/overview.html", preds=pm.latest_predictions(), version=version)


@bp.post("/score")
@require("predict.review")
def run_scoring():
    preds = pm.score_assets()
    flash(f"Scored {len(preds)} assets.", "info")
    return redirect(url_for("predictive.overview"))


@bp.get("/queue")
@require("predict.review")
def queue():
    return render_template("predictive/queue.html", queue=pm.review_queue())


@bp.route("/predictions/<pred_id>", methods=["GET", "POST"])
@require("predict.review")
def review(pred_id):
    pred = db.session.get(Prediction, pred_id) or abort(404)
    if request.method == "POST":
        try:
            wo = pm.review(pred, g.user, request.form.get("decision", ""), note=field("note", 1000),
                           start=date_field("start"), end=date_field("end"))
            db.session.commit()
            flash("Decision recorded." + (" Work order created." if wo else ""), "info")
            return redirect(url_for("predictive.queue"))
        except ValueError as e:
            db.session.rollback()
            flash(str(e), "error")
    return render_template("predictive/review.html", p=pred)


@bp.post("/queue/bulk-dismiss")
@require("predict.review")
def bulk_dismiss():
    note = (request.form.get("note") or "").strip()[:1000]
    ids = request.form.getlist("ids")
    if not note or not ids:
        flash("Select recommendations and enter a reason.", "error")
        return redirect(url_for("predictive.queue"))
    count = 0
    for pid in ids[:500]:
        p = db.session.get(Prediction, pid)
        if p and p.reviewer_decision == "pending":
            pm.review(p, g.user, "dismissed", note=note)
            count += 1
    db.session.commit()
    flash(f"Dismissed {count} recommendation(s).", "info")
    return redirect(url_for("predictive.queue"))


@bp.get("/models")
@require("predict.train")
def models():
    rows = ModelVersion.query.order_by(ModelVersion.trained_at.desc()).all()
    return render_template("predictive/models.html", rows=rows, active=pm.active_model()[0])


@bp.post("/models/train")
@require("predict.train")
def train():
    try:
        mv = pm.train(g.user)
        flash(f"Trained candidate {mv.version}: precision {mv.metrics['precision']}, recall {mv.metrics['recall']}. "
              "Review and promote manually.", "info")
    except pm.TrainingError as e:
        db.session.rollback()
        audit("train_failed", "MODEL_VERSION", after={"error": str(e)})
        db.session.commit()
        flash(f"Training failed: {e}", "error")
    return redirect(url_for("predictive.models"))


@bp.post("/models/<version>/<action>")
@require("predict.train")
def model_action(version, action):
    mv = db.session.get(ModelVersion, version) or abort(404)
    try:
        if action == "promote":
            pm.promote(mv, g.user)
        elif action == "discard":
            pm.discard(mv, g.user)
        else:
            abort(400)
        flash(f"Model {version}: {action} done.", "info")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("predictive.models"))


@bp.route("/ingest", methods=["GET", "POST"])
@require("telemetry.ingest")
def ingest():
    if request.method == "POST":
        f = request.files.get("file")
        if not f or not f.filename.lower().endswith(".csv"):
            flash("Choose a .csv file.", "error")
        else:
            try:
                batch = ingest_telemetry_csv(f.read().decode("utf-8-sig"), f.filename, g.user)
                if batch.status == "halted":
                    flash(f"Load halted: {batch.rejected} invalid row(s) exceed the allowed ratio. Nothing was loaded.",
                          "error")
                else:
                    flash(f"Loaded {batch.loaded} reading(s); {batch.rejected} quarantined.", "info")
            except UnicodeDecodeError:
                flash("File must be UTF-8 encoded.", "error")
        return redirect(url_for("predictive.ingest"))
    batches = IngestBatch.query.order_by(IngestBatch.created_at.desc()).limit(50).all()
    return render_template("predictive/ingest.html", batches=batches)


@bp.post("/ingest/<batch_id>/rollback")
@require("telemetry.ingest")
def rollback(batch_id):
    batch = db.session.get(IngestBatch, batch_id) or abort(404)
    try:
        n = rollback_batch(batch, g.user)
        flash(f"Rolled back batch; removed {n} reading(s).", "info")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("predictive.ingest"))

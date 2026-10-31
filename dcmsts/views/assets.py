from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from ..models import (LIFECYCLE_STATUSES, METRICS, Asset, MaintSchedule, Prediction, SensorReading, WorkOrder, db,
                      utcnow)
from ..security import audit, require, snapshot
from ..services.ingest import METRIC_RANGES, import_assets_csv
from . import date_field, field, number_field

bp = Blueprint("assets", __name__, url_prefix="/assets")
FIELDS = ["asset_tag", "name", "category", "location", "manufacturer", "install_date", "warranty_expiry",
          "lifecycle_status"]


def _get(asset_id):
    return db.session.get(Asset, asset_id) or abort(404)


@bp.get("/")
@require("assets.view")
def index():
    q = Asset.query
    cat, status, term = request.args.get("category"), request.args.get("status"), request.args.get("q")
    if cat:
        q = q.filter_by(category=cat)
    if status:
        q = q.filter_by(lifecycle_status=status)
    if term:
        like = f"%{term[:50]}%"
        q = q.filter(db.or_(Asset.asset_tag.ilike(like), Asset.name.ilike(like), Asset.location.ilike(like)))
    categories = [c for (c,) in db.session.query(Asset.category).distinct().order_by(Asset.category)]
    return render_template("assets/index.html", assets=q.order_by(Asset.asset_tag).all(), categories=categories)


def _apply_form(asset):
    asset.asset_tag = field("asset_tag", 50, True)
    asset.name = field("name", 150, True)
    asset.category = field("category", 80, True)
    asset.location = field("location", 150)
    asset.manufacturer = field("manufacturer", 100)
    asset.install_date = date_field("install_date")
    asset.warranty_expiry = date_field("warranty_expiry")


@bp.route("/new", methods=["GET", "POST"])
@require("assets.manage")
def new():
    asset = Asset()
    if request.method == "POST":
        try:
            _apply_form(asset)
            dup = Asset.query.filter_by(asset_tag=asset.asset_tag).first()
            if dup:
                flash(f"Asset tag {asset.asset_tag} already exists ({dup.name}).", "error")
                return render_template("assets/form.html", asset=asset, dup=dup), 409
            db.session.add(asset)
            db.session.flush()
            audit("create", "ASSET", asset.asset_id, after=snapshot(asset, FIELDS))
            db.session.commit()
            flash("Asset registered.", "info")
            return redirect(url_for("assets.detail", asset_id=asset.asset_id))
        except ValueError as e:
            flash(str(e), "error")
    return render_template("assets/form.html", asset=asset)


@bp.route("/<asset_id>/edit", methods=["GET", "POST"])
@require("assets.manage")
def edit(asset_id):
    asset = _get(asset_id)
    if request.method == "POST":
        before = snapshot(asset, FIELDS)
        try:
            with db.session.no_autoflush:
                _apply_form(asset)
                dup = Asset.query.filter(Asset.asset_tag == asset.asset_tag, Asset.asset_id != asset.asset_id).first()
            if dup:
                db.session.rollback()
                flash(f"Asset tag already used by {dup.name}.", "error")
                return render_template("assets/form.html", asset=_get(asset_id), dup=dup), 409
            audit("update", "ASSET", asset.asset_id, before=before, after=snapshot(asset, FIELDS))
            db.session.commit()
            flash("Asset updated.", "info")
            return redirect(url_for("assets.detail", asset_id=asset.asset_id))
        except ValueError as e:
            db.session.rollback()
            flash(str(e), "error")
            asset = _get(asset_id)
    return render_template("assets/form.html", asset=asset)


@bp.post("/<asset_id>/status")
@require("assets.manage")
def set_status(asset_id):
    asset = _get(asset_id)
    status = request.form.get("lifecycle_status")
    if status not in LIFECYCLE_STATUSES:
        abort(400)
    before = asset.lifecycle_status
    asset.lifecycle_status = status
    if status == "retired":  # cascade-deactivate schedules on retirement (Section 10.5)
        MaintSchedule.query.filter_by(asset_id=asset.asset_id, active=True).update({"active": False})
    audit("update", "ASSET", asset.asset_id, before={"lifecycle_status": before}, after={"lifecycle_status": status})
    db.session.commit()
    flash(f"Asset marked {status}.", "info")
    return redirect(url_for("assets.detail", asset_id=asset_id))


@bp.route("/import", methods=["GET", "POST"])
@require("assets.manage")
def import_csv():
    errors = []
    if request.method == "POST":
        f = request.files.get("file")
        if not f or not f.filename.lower().endswith(".csv"):
            flash("Choose a .csv file.", "error")
        else:
            try:
                text = f.read().decode("utf-8-sig")
            except UnicodeDecodeError:
                text, errors = None, ["File must be UTF-8 encoded."]
            if text is not None:
                count, errors = import_assets_csv(text, g.user)
                if not errors:
                    flash(f"Imported {count} assets.", "info")
                    return redirect(url_for("assets.index"))
    return render_template("assets/import.html", errors=errors)


@bp.get("/<asset_id>")
@require("assets.view")
def detail(asset_id):
    asset = _get(asset_id)
    wos = WorkOrder.query.filter_by(asset_id=asset_id).order_by(WorkOrder.due_date.desc()).limit(50).all()
    readings = (SensorReading.query.filter_by(asset_id=asset_id).order_by(SensorReading.recorded_at.desc())
                .limit(30).all())
    predictions = []
    if g.user.can("predict.view"):
        predictions = (Prediction.query.filter_by(asset_id=asset_id).order_by(Prediction.created_at.desc())
                       .limit(10).all())
    schedules = MaintSchedule.query.filter_by(asset_id=asset_id).all()
    return render_template("assets/detail.html", asset=asset, wos=wos, readings=readings, predictions=predictions,
                           schedules=schedules, metrics=METRICS)


@bp.post("/<asset_id>/readings")
@require("telemetry.log")
def add_reading(asset_id):
    """Manual telemetry entry by technicians during visits (UC-15 alternate flow)."""
    asset = _get(asset_id)
    try:
        metric = request.form.get("metric_name")
        if metric not in METRICS:
            raise ValueError("Unknown metric.")
        value = number_field("metric_value", required=True)
        lo, hi = METRIC_RANGES[metric]
        if not lo <= value <= hi:
            raise ValueError(f"Value must be between {lo} and {hi}.")
        r = SensorReading(asset_id=asset.asset_id, metric_name=metric, metric_value=value, source="manual",
                          recorded_at=utcnow())
        db.session.add(r)
        db.session.flush()
        audit("create", "SENSOR_READING", r.reading_id, after={"asset": asset.asset_tag, metric: value})
        db.session.commit()
        flash("Reading recorded.", "info")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(request.form.get("back") if request.form.get("back", "").startswith("/work-orders/")
                    else url_for("assets.detail", asset_id=asset_id))

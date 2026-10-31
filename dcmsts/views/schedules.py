from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from ..models import PRIORITIES, Asset, MaintSchedule, db, utcnow
from ..security import audit, require, snapshot
from ..services.scheduling import FREQS, RuleError, build_rule, generate_instances, overlapping_schedules, validate_schedule
from . import choice_field, date_field, field, number_field

bp = Blueprint("schedules", __name__, url_prefix="/schedules")
FIELDS = ["asset_id", "title", "recurrence_rule", "checklist_template", "start_date", "end_date", "priority",
          "required_skill", "compliance_ref", "active"]


@bp.get("/")
@require("schedules.manage")
def index():
    rows = MaintSchedule.query.join(Asset).order_by(MaintSchedule.active.desc(), Asset.asset_tag).all()
    return render_template("schedules/index.html", rows=rows)


@bp.route("/new", methods=["GET", "POST"])
@require("schedules.manage")
def new():
    assets = Asset.query.filter(Asset.lifecycle_status != "retired").order_by(Asset.asset_tag).all()
    form = request.form
    overlaps = []
    if request.method == "POST":
        try:
            asset = db.session.get(Asset, form.get("asset_id")) or abort(400)
            rule = build_rule(choice_field("freq", FREQS), number_field("interval", True, 1, int))
            checklist = [line.strip()[:200] for line in (form.get("checklist") or "").splitlines() if line.strip()]
            start, end = date_field("start_date", True), date_field("end_date")
            errors = validate_schedule(rule, start, end, checklist)
            if errors:
                raise ValueError(" ".join(errors))
            overlaps = overlapping_schedules(asset.asset_id)
            if overlaps and form.get("confirm_overlap") != "yes":
                flash("This asset already has active schedules. Confirm to create an overlapping schedule.", "warn")
                return render_template("schedules/form.html", assets=assets, form=form, overlaps=overlaps,
                                       freqs=FREQS, priorities=PRIORITIES)
            s = MaintSchedule(asset_id=asset.asset_id, title=field("title", 150, True), recurrence_rule=rule,
                              checklist_template=checklist, start_date=start, end_date=end, next_due_date=start,
                              priority=choice_field("priority", PRIORITIES), required_skill=field("required_skill", 80),
                              estimated_minutes=number_field("estimated_minutes", False, 1, int),
                              compliance_ref=field("compliance_ref", 150))
            s.asset = asset
            db.session.add(s)
            db.session.flush()
            created = generate_instances(s, utcnow().date())
            audit("create", "MAINT_SCHEDULE", s.schedule_id, after=snapshot(s, FIELDS))
            db.session.commit()
            flash(f"Schedule created; {len(created)} upcoming task(s) generated.", "info")
            return redirect(url_for("schedules.index"))
        except (ValueError, RuleError) as e:
            db.session.rollback()
            flash(str(e), "error")
    return render_template("schedules/form.html", assets=assets, form=form, overlaps=overlaps, freqs=FREQS,
                           priorities=PRIORITIES)


@bp.post("/<schedule_id>/toggle")
@require("schedules.manage")
def toggle(schedule_id):
    s = db.session.get(MaintSchedule, schedule_id) or abort(404)
    if not s.active and s.asset.lifecycle_status == "retired":
        flash("Cannot activate a schedule for a retired asset.", "error")
        return redirect(url_for("schedules.index"))
    s.active = not s.active
    audit("update", "MAINT_SCHEDULE", s.schedule_id, before={"active": not s.active}, after={"active": s.active})
    db.session.commit()
    flash("Schedule " + ("activated." if s.active else "deactivated."), "info")
    return redirect(url_for("schedules.index"))

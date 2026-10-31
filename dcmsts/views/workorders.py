import hashlib
import os
import uuid

from flask import (Blueprint, abort, current_app, flash, g, redirect, render_template, request, send_file,
                   url_for)

from ..models import PRIORITIES, WO_STATUSES, Asset, Evidence, PartsUsed, User, Vendor, WorkOrder, db, utcnow
from ..security import audit, can_access_work_order, deny, require
from ..services import workorders as svc
from . import choice_field, datetime_field, field, number_field
from .dashboard import scoped_work_orders

bp = Blueprint("workorders", __name__, url_prefix="/work-orders")

UPLOAD_TYPES = {  # extension -> (magic prefix, mime)
    "jpg": (b"\xff\xd8\xff", "image/jpeg"), "jpeg": (b"\xff\xd8\xff", "image/jpeg"),
    "png": (b"\x89PNG\r\n\x1a\n", "image/png"), "pdf": (b"%PDF-", "application/pdf"),
}


def _get(wo_id):
    wo = db.session.get(WorkOrder, wo_id) or abort(404)
    if not can_access_work_order(g.user, wo):
        deny("work order outside scope")
    return wo


def _is_executor(wo):
    """Assigned technician, user of the assigned vendor, or a manager/admin."""
    u = g.user
    return (u.can("workorders.assign") or wo.assigned_to == u.user_id
            or (u.role_name == "Vendor" and wo.vendor_id == u.vendor_id))


def _back(wo):
    return redirect(url_for("workorders.detail", wo_id=wo.work_order_id))


@bp.get("/")
@require("workorders.update_own")
def index():
    q = scoped_work_orders(g.user)
    status, wtype = request.args.get("status"), request.args.get("type")
    if status == "overdue":
        q = q.filter(WorkOrder.is_overdue.is_(True), WorkOrder.status != "completed")
    elif status == "open":
        q = q.filter(WorkOrder.status.in_(svc.OPEN_STATUSES))
    elif status in WO_STATUSES:
        q = q.filter(WorkOrder.status == status)
    if wtype in ("preventive", "corrective"):
        q = q.filter(WorkOrder.type == wtype)
    rows = q.order_by(WorkOrder.due_date).limit(500).all()
    return render_template("workorders/index.html", rows=rows, statuses=WO_STATUSES)


@bp.route("/new", methods=["GET", "POST"])
@require("workorders.create")
def new():
    """Ad-hoc corrective work order / fault report (FR-02)."""
    assets = Asset.query.filter(Asset.lifecycle_status != "retired").order_by(Asset.asset_tag).all()
    if request.method == "POST":
        try:
            asset = db.session.get(Asset, request.form.get("asset_id")) or abort(400)
            incident = None
            if request.form.get("is_incident"):
                incident = {"downtime_start": datetime_field("downtime_start") or utcnow(),
                            "cause": field("downtime_cause", 150), "planned": False}
            checklist = [{"item": line.strip()[:200], "done": False}
                         for line in (request.form.get("checklist") or "").splitlines() if line.strip()]
            wo = svc.create_work_order(
                asset=asset, type="corrective", priority=choice_field("priority", PRIORITIES),
                title=field("title", 200, True), description=field("description", 4000),
                checklist=checklist, required_skill=field("required_skill", 80),
                due_date=datetime_field("due_date"), created_by=g.user, incident=incident)
            if incident and asset.lifecycle_status == "active":
                asset.lifecycle_status = "under_repair"
            audit("create", "WORK_ORDER", wo.work_order_id,
                  after={"asset": asset.asset_tag, "type": "corrective", "priority": wo.priority, "title": wo.title,
                         "incident": bool(incident)})
            if not g.user.can("workorders.assign"):
                from ..services.notify import notify_role
                notify_role("Manager", "fault_reported", f"Fault reported: {wo.title}",
                            f"{g.user.full_name} reported '{wo.title}' on {asset.asset_tag}. Assignment needed.", wo)
            db.session.commit()
            flash("Work order created.", "info")
            if can_access_work_order(g.user, wo):
                return _back(wo)
            return redirect(url_for("dashboard.index"))
        except ValueError as e:
            db.session.rollback()
            flash(str(e), "error")
    return render_template("workorders/new.html", assets=assets, priorities=PRIORITIES,
                           preselect=request.args.get("asset_id"))


@bp.get("/<wo_id>")
@require("workorders.update_own")
def detail(wo_id):
    wo = _get(wo_id)
    from ..models import METRICS
    return render_template("workorders/detail.html", wo=wo, missing=svc.missing_requirements(wo),
                           executor=_is_executor(wo), metrics=METRICS, priorities=PRIORITIES,
                           editable=lambda t: svc.log_is_editable(t, g.user))


@bp.route("/<wo_id>/assign", methods=["GET", "POST"])
@require("workorders.assign")
def assign(wo_id):
    wo = _get(wo_id)
    if request.method == "POST":
        tech = vendor = None
        target = request.form.get("target", "")
        if target.startswith("user:"):
            tech = db.session.get(User, target[5:])
            if tech is None or tech.role_name != "Technician" or tech.status != "active":
                abort(400)
        elif target.startswith("vendor:"):
            vendor = db.session.get(Vendor, target[7:]) or abort(400)
        before = {"assigned_to": wo.assigned_to, "vendor_id": wo.vendor_id, "status": wo.status}
        try:
            svc.assign(wo, g.user, technician=tech, vendor=vendor,
                       override_reason=field("override_reason", 500))
            audit("assign", "WORK_ORDER", wo.work_order_id, before=before,
                  after={"assigned_to": wo.assigned_to, "vendor_id": wo.vendor_id, "status": wo.status,
                         "override_reason": request.form.get("override_reason") or None})
            db.session.commit()
            flash("Assignment saved and notification queued.", "info")
            return _back(wo)
        except ValueError as e:
            db.session.rollback()
            flash(str(e), "error")
            wo = _get(wo_id)
    all_techs = User.query.join(User.role).filter_by(role_name="Technician").filter(User.status == "active").all()
    return render_template("workorders/assign.html", wo=wo, c=svc.candidates(wo), all_techs=all_techs)


@bp.post("/<wo_id>/status")
@require("workorders.update_own")
def status(wo_id):
    wo = _get(wo_id)
    if not _is_executor(wo):
        deny("not the assignee")
    before = {"status": wo.status}
    try:
        svc.change_status(wo, g.user, request.form.get("status", ""), reason=field("reason", 1000),
                          root_cause=field("root_cause", 2000))
        audit("status_change", "WORK_ORDER", wo.work_order_id, before=before, after={"status": wo.status})
        db.session.commit()
        flash(f"Status changed to {wo.status.replace('_', ' ')}.", "info")
    except ValueError as e:
        db.session.rollback()
        flash(str(e), "error")
    return _back(wo)


@bp.post("/<wo_id>/checklist")
@require("workorders.update_own")
def checklist(wo_id):
    wo = _get(wo_id)
    if not _is_executor(wo) or wo.status not in ("in_progress", "assigned", "blocked"):
        flash("Checklist can only be updated on an active task you are assigned to.", "error")
        return _back(wo)
    done = set(request.form.getlist("done"))
    before = [dict(c) for c in wo.checklist]
    wo.checklist = [{"item": c["item"], "done": str(i) in done} for i, c in enumerate(wo.checklist)]
    svc.add_log(wo, g.user, "checklist", f"{len(done)}/{len(wo.checklist)} items done")
    audit("update", "WORK_ORDER", wo.work_order_id, before={"checklist": before}, after={"checklist": wo.checklist})
    db.session.commit()
    flash("Checklist saved.", "info")
    return _back(wo)


@bp.post("/<wo_id>/log")
@require("workorders.update_own")
def log_work(wo_id):
    """Add a note, time spent and/or a part used."""
    wo = _get(wo_id)
    if not _is_executor(wo) or wo.status == "completed":
        flash("Work can only be logged on open tasks you are assigned to.", "error")
        return _back(wo)
    try:
        note = field("note", 4000)
        minutes = number_field("minutes", False, 0, int)
        part = field("part_name", 150)
        qty = number_field("quantity", False, 0.01)
        cost = number_field("cost", False, 0)
        if not (note or minutes or part):
            raise ValueError("Enter a note, time spent or a part.")
        if part and qty is None:
            raise ValueError("Quantity is required for a part.")
        after = {}
        if minutes:
            wo.time_spent_minutes += minutes
            after["minutes"] = minutes
        if part:
            p = PartsUsed(work_order_id=wo.work_order_id, part_name=part, quantity=qty, cost=cost)
            db.session.add(p)
            after.update(part=part, quantity=qty, cost=cost)
        if note:
            after["note"] = note
        svc.add_log(wo, g.user, "work_logged", "; ".join(f"{k}={v}" for k, v in after.items()))
        audit("update", "WORK_ORDER", wo.work_order_id, after=after)
        db.session.commit()
        flash("Work logged.", "info")
    except ValueError as e:
        db.session.rollback()
        flash(str(e), "error")
    return _back(wo)


@bp.post("/<wo_id>/parts/<part_id>/delete")
@require("workorders.update_own")
def delete_part(wo_id, part_id):
    wo = _get(wo_id)
    part = db.session.get(PartsUsed, part_id)
    if part is None or part.work_order_id != wo.work_order_id or not _is_executor(wo):
        abort(404)
    if not svc.log_is_editable(part.logged_at, g.user):
        deny("parts log locked after 24 hours")
    audit("delete", "PARTS_USED", part.part_use_id,
          before={"part": part.part_name, "quantity": str(part.quantity), "work_order": wo.work_order_id})
    db.session.delete(part)
    db.session.commit()
    flash("Part entry removed.", "info")
    return _back(wo)


@bp.post("/<wo_id>/evidence")
@require("workorders.update_own")
def upload_evidence(wo_id):
    wo = _get(wo_id)
    if not _is_executor(wo) or wo.status == "completed":
        flash("Evidence can only be added to open tasks you are assigned to.", "error")
        return _back(wo)
    f = request.files.get("file")
    ext = (f.filename.rsplit(".", 1)[-1].lower() if f and "." in f.filename else "")
    if ext not in UPLOAD_TYPES:
        flash("Upload a JPG, PNG or PDF file.", "error")
        return _back(wo)
    data = f.read()
    magic, mime = UPLOAD_TYPES[ext]
    if not data.startswith(magic):
        flash("File content does not match its type.", "error")
        return _back(wo)
    key = f"{uuid.uuid4().hex}.{ext}"
    with open(os.path.join(current_app.config["UPLOAD_DIR"], key), "wb") as out:
        out.write(data)
    ev = Evidence(work_order_id=wo.work_order_id, file_url=key, original_name=f.filename[:200],
                  sha256=hashlib.sha256(data).hexdigest(), type=mime, uploaded_by=g.user.user_id)
    db.session.add(ev)
    db.session.flush()
    svc.add_log(wo, g.user, "evidence", f"Uploaded {ev.original_name}")
    audit("create", "EVIDENCE", ev.evidence_id, after={"work_order": wo.work_order_id, "sha256": ev.sha256})
    db.session.commit()
    flash("Evidence uploaded.", "info")
    return _back(wo)


@bp.get("/<wo_id>/evidence/<ev_id>")
@require("workorders.update_own")
def evidence(wo_id, ev_id):
    wo = _get(wo_id)
    ev = db.session.get(Evidence, ev_id)
    if ev is None or ev.work_order_id != wo.work_order_id:
        abort(404)
    path = os.path.join(current_app.config["UPLOAD_DIR"], os.path.basename(ev.file_url))
    return send_file(path, mimetype=ev.type, as_attachment=ev.type == "application/pdf",
                     download_name=ev.original_name)


@bp.post("/<wo_id>/reschedule")
@require("workorders.update_own")
def reschedule(wo_id):
    wo = _get(wo_id)
    try:
        new_due = datetime_field("due_date", True)
        note = field("note", 1000)
        if not g.user.can("workorders.assign"):
            if not _is_executor(wo):
                deny("not the assignee")
            # Technician-initiated: pending manager approval (UC-10 1a).
            svc.add_log(wo, g.user, "change_requested", f"Requested new due {new_due:%Y-%m-%d %H:%M}: {note or ''}")
            from ..services.notify import notify_role
            notify_role("Manager", "change_request", f"Reschedule requested: {wo.title}",
                        f"{g.user.full_name} requested moving '{wo.title}' to {new_due:%Y-%m-%d %H:%M}. Reason: {note or '-'}", wo)
            audit("change_request", "WORK_ORDER", wo.work_order_id, after={"requested_due": new_due.isoformat(), "note": note})
            db.session.commit()
            flash("Change request sent to the manager for approval.", "info")
            return _back(wo)
        if svc.outside_sla(wo, new_due) and request.form.get("confirm_sla") != "yes":
            flash("The new date is outside the SLA window. Tick the confirmation box to proceed.", "warn")
            return _back(wo)
        before = {"due_date": wo.due_date.isoformat(), "reschedule_count": wo.reschedule_count}
        svc.reschedule(wo, g.user, new_due, note)
        audit("reschedule", "WORK_ORDER", wo.work_order_id, before=before,
              after={"due_date": new_due.isoformat(), "reschedule_count": wo.reschedule_count, "note": note})
        db.session.commit()
        flash("Task rescheduled.", "info")
    except ValueError as e:
        db.session.rollback()
        flash(str(e), "error")
    return _back(wo)


@bp.post("/<wo_id>/downtime")
@require("workorders.assign")
def downtime(wo_id):
    """Record/close downtime for an incident (FR-14)."""
    wo = _get(wo_id)
    try:
        start, end = datetime_field("downtime_start", True), datetime_field("downtime_end")
        if end and end < start:
            raise ValueError("Downtime end must be after start.")
        before = {"start": wo.downtime_start and wo.downtime_start.isoformat(),
                  "end": wo.downtime_end and wo.downtime_end.isoformat()}
        wo.downtime_start, wo.downtime_end = start, end
        wo.downtime_cause = field("downtime_cause", 150)
        wo.downtime_planned = request.form.get("downtime_planned") == "on"
        if end and wo.asset.lifecycle_status == "under_repair":
            wo.asset.lifecycle_status = "active"
        svc.add_log(wo, g.user, "downtime", f"{start} to {end or 'ongoing'} ({wo.downtime_cause or 'no cause'})")
        audit("update", "WORK_ORDER", wo.work_order_id, before=before,
              after={"start": start.isoformat(), "end": end and end.isoformat(), "cause": wo.downtime_cause,
                     "planned": wo.downtime_planned})
        db.session.commit()
        flash("Downtime saved.", "info")
    except ValueError as e:
        db.session.rollback()
        flash(str(e), "error")
    return _back(wo)

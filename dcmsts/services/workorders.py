"""Work-order lifecycle rules: creation with SLA snapshot, assignment, status changes, rescheduling."""
from datetime import date, timedelta

from ..models import (PRIORITIES, Role, SlaPolicy, TaskLog, User, Vendor, WorkOrder, db, get_setting,
                      utcnow)
from .notify import notify

PRIORITY_WEIGHT = {"low": 1, "medium": 2, "high": 4, "critical": 6}
OPEN_STATUSES = ("unassigned", "assigned", "in_progress", "blocked")


class RuleViolation(ValueError):
    pass


def sla_policy_for(priority, category):
    return (SlaPolicy.query.filter_by(priority=priority, category=category).first()
            or SlaPolicy.query.filter_by(priority=priority, category=None).first())


def create_work_order(*, asset, type, priority, title, due_date=None, checklist=None, schedule=None,
                      required_skill=None, description=None, created_by=None, prediction=None, incident=None):
    if priority not in PRIORITIES:
        raise RuleViolation("Invalid priority.")
    policy = sla_policy_for(priority, asset.category)
    now = utcnow()
    if due_date is None:
        due_date = now + timedelta(hours=policy.resolution_hours if policy else 72)
    wo = WorkOrder(
        asset_id=asset.asset_id, type=type, priority=priority, title=title, due_date=due_date,
        checklist=checklist or [], schedule_id=schedule.schedule_id if schedule else None,
        required_skill=required_skill or None, description=description,
        created_by=created_by.user_id if created_by else None,
        prediction_id=prediction.prediction_id if prediction else None,
    )
    wo.asset = asset
    if policy:
        wo.sla_resolution_hours = policy.resolution_hours
        wo.sla_escalation_hours = policy.escalation_hours
        wo.sla_chain = list(policy.escalation_chain or [])
        wo.sla_final_vendor_id = policy.final_vendor_id
    if incident:
        wo.downtime_start = incident.get("downtime_start")
        wo.downtime_cause = incident.get("cause")
        wo.downtime_planned = bool(incident.get("planned"))
    db.session.add(wo)
    db.session.flush()
    add_log(wo, created_by, "created", f"{type} work order created")
    return wo


def add_log(wo, user, action, notes=None):
    db.session.add(TaskLog(work_order_id=wo.work_order_id, user_id=user.user_id if user else None,
                           action=action, notes=notes))


# ---------- UC-11: candidate search ----------

def _skills(text):
    return {s.strip().lower() for s in (text or "").split(",") if s.strip()}


def technician_has_skill(user, skill, on_date):
    """A skill counts if listed and not tied to an expired certification of the same name."""
    if not skill:
        return True
    skill = skill.lower()
    certs = {c["name"].strip().lower(): c.get("expires") for c in (user.certifications or []) if c.get("name")}
    if skill in certs:
        exp = certs[skill]
        return exp is None or date.fromisoformat(exp) >= on_date
    return skill in _skills(user.skills)


def workload_score(user_id):
    rows = db.session.query(WorkOrder.priority).filter(
        WorkOrder.assigned_to == user_id, WorkOrder.status.in_(OPEN_STATUSES)).all()
    return sum(PRIORITY_WEIGHT[p] for (p,) in rows)


def conflicts_for(user_id, wo):
    day = wo.due_date.date()
    rows = WorkOrder.query.filter(
        WorkOrder.assigned_to == user_id, WorkOrder.status.in_(OPEN_STATUSES),
        WorkOrder.work_order_id != wo.work_order_id).all()
    return [r for r in rows if r.due_date.date() == day]


def candidates(wo):
    """Return {"technicians": [...], "vendors": [...], "no_qualified": bool}, ranked by workload."""
    today = utcnow().date()
    techs = User.query.join(Role).filter(Role.role_name == "Technician", User.status == "active").all()
    ranked = []
    for t in techs:
        if not technician_has_skill(t, wo.required_skill, today):
            continue
        conflicts = conflicts_for(t.user_id, wo)
        ranked.append({"user": t, "workload": workload_score(t.user_id), "conflicts": conflicts})
    ranked.sort(key=lambda c: (len(c["conflicts"]) > 0, c["workload"]))
    vendors = [v for v in Vendor.query.all() if v.contract_valid(today)
               and (not wo.required_skill or wo.required_skill.lower() in _skills(v.skills) | _skills(v.service_type))]
    return {"technicians": ranked, "vendors": vendors, "no_qualified": not ranked and not vendors}


def assign(wo, actor, technician=None, vendor=None, override_reason=None):
    if wo.status == "completed":
        raise RuleViolation("Completed work orders cannot be reassigned.")
    if bool(technician) == bool(vendor):
        raise RuleViolation("Choose exactly one technician or vendor.")
    today = utcnow().date()
    if vendor is not None:
        if not vendor.contract_valid(today):
            raise RuleViolation("Vendor has no valid, non-expired service contract.")
        wo.vendor_id, wo.assigned_to = vendor.vendor_id, None
        target = f"vendor {vendor.company_name}"
        recipients = User.query.filter_by(vendor_id=vendor.vendor_id, status="active").all()
    else:
        suggested = {c["user"].user_id for c in candidates(wo)["technicians"]}
        high = wo.priority in ("high", "critical")
        overlapping_high = [c for c in conflicts_for(technician.user_id, wo) if c.priority in ("high", "critical")]
        if technician.user_id not in suggested and not override_reason:
            raise RuleViolation("Technician is outside the suggested list; an override reason is required.")
        if high and overlapping_high and not override_reason:
            raise RuleViolation("Technician already has an overlapping high-priority task; an override reason is required.")
        wo.assigned_to, wo.vendor_id = technician.user_id, None
        target = technician.full_name
        recipients = [technician]
    if wo.status == "unassigned":
        wo.status = "assigned"
    add_log(wo, actor, "assigned", f"Assigned to {target}" + (f" (override: {override_reason})" if override_reason else ""))
    for r in recipients:
        notify(r, "assignment", f"New task: {wo.title}",
               f"You have been assigned '{wo.title}' on {wo.asset.asset_tag}, due {wo.due_date:%Y-%m-%d %H:%M}.", wo)


# ---------- UC-03: status updates ----------

def missing_requirements(wo):
    missing = [c["item"] for c in wo.checklist if not c.get("done")]
    if wo.asset.category in get_setting("evidence_required_categories") and not wo.evidence:
        missing.append("At least one evidence photo/document")
    return missing


def _managers():
    return User.query.join(Role).filter(Role.role_name == "Manager", User.status == "active").all()


def change_status(wo, actor, new_status, reason=None, root_cause=None):
    allowed = {
        "assigned": {"in_progress", "blocked"},
        "in_progress": {"blocked", "completed"},
        "blocked": {"in_progress"},
    }
    if new_status not in allowed.get(wo.status, set()):
        raise RuleViolation(f"Cannot change status from {wo.status} to {new_status}.")
    if new_status == "blocked":
        if not reason:
            raise RuleViolation("A reason is required when blocking a task.")
        wo.blocked_reason = reason
        for m in _managers():
            notify(m, "task_blocked", f"Blocked: {wo.title}", f"{actor.full_name} blocked '{wo.title}': {reason}", wo)
    if new_status == "completed":
        missing = missing_requirements(wo)
        if missing:
            raise RuleViolation("Cannot complete; missing: " + "; ".join(missing))
        wo.completed_at = utcnow()
        wo.is_overdue = False
        if root_cause:
            wo.root_cause = root_cause
        for m in _managers():
            notify(m, "task_completed", f"Completed: {wo.title}",
                   f"{actor.full_name} completed '{wo.title}' on {wo.asset.asset_tag}. Please review.", wo)
    old = wo.status
    wo.status = new_status
    add_log(wo, actor, "status_change", f"{old} -> {new_status}" + (f": {reason}" if reason else ""))


def log_is_editable(created_at, actor):
    return actor.role_name == "Administrator" or utcnow() - created_at <= timedelta(hours=24)


# ---------- UC-10: reschedule ----------

def reschedule(wo, actor, new_due, note=None):
    if wo.status == "completed":
        raise RuleViolation("Completed work orders cannot be rescheduled.")
    if wo.reschedule_count >= get_setting("max_reschedules") and not note:
        raise RuleViolation("This task has reached the reschedule limit; a supervisor note is required.")
    old = wo.due_date
    wo.due_date = new_due
    wo.reschedule_count += 1
    wo.is_overdue = new_due < utcnow() and wo.is_overdue
    wo.reminder_sent = False
    add_log(wo, actor, "rescheduled", f"{old:%Y-%m-%d %H:%M} -> {new_due:%Y-%m-%d %H:%M}" + (f": {note}" if note else ""))
    if wo.assignee:
        notify(wo.assignee, "schedule_change", f"Rescheduled: {wo.title}",
               f"'{wo.title}' is now due {new_due:%Y-%m-%d %H:%M}.", wo)


def outside_sla(wo, new_due):
    if not wo.sla_resolution_hours:
        return False
    return new_due > wo.created_at + timedelta(hours=wo.sla_resolution_hours)

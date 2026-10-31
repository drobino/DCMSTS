"""Periodic jobs (UC-04): reminders, overdue marking, escalation. Run via `flask run-jobs` from cron."""
from datetime import timedelta

from ..models import User, Vendor, WorkOrder, db, get_setting, utcnow
from ..security import audit
from .notify import dispatch_pending, notify, notify_role
from .scheduling import generate_all
from .workorders import OPEN_STATUSES, add_log, create_work_order


def send_reminders(now):
    window = now + timedelta(hours=get_setting("reminder_hours"))
    count = 0
    rows = WorkOrder.query.filter(WorkOrder.status.in_(("assigned", "in_progress")), WorkOrder.reminder_sent.is_(False),
                                  WorkOrder.due_date <= window, WorkOrder.due_date > now).all()
    for wo in rows:
        recipients = [wo.assignee] if wo.assignee else User.query.filter_by(vendor_id=wo.vendor_id).all()
        for r in recipients:
            notify(r, "reminder", f"Due soon: {wo.title}", f"'{wo.title}' is due {wo.due_date:%Y-%m-%d %H:%M}.", wo)
        wo.reminder_sent = True
        count += 1
    return count


def _escalate(wo, now):
    chain = wo.sla_chain or []
    level = wo.escalation_level  # 0 = not yet overdue-notified
    if level == 0:
        notify_role("Manager", "escalation", f"OVERDUE: {wo.title}",
                    f"'{wo.title}' on {wo.asset.asset_tag} was due {wo.due_date:%Y-%m-%d %H:%M} and is overdue.", wo)
    elif level <= len(chain):
        user = db.session.get(User, chain[level - 1])
        notify(user, "escalation", f"ESCALATION L{level}: {wo.title}",
               f"'{wo.title}' remains unresolved {wo.due_date:%Y-%m-%d %H:%M}. You are escalation tier {level}.", wo)
    elif level == len(chain) + 1 and wo.sla_final_vendor_id:
        vendor = db.session.get(Vendor, wo.sla_final_vendor_id)
        draft = create_work_order(asset=wo.asset, type="corrective", priority=wo.priority,
                                  title=f"[Vendor escalation] {wo.title}", checklist=wo.checklist,
                                  description=f"Drafted automatically after escalation of {wo.work_order_id}.")
        add_log(draft, None, "drafted", f"Draft for vendor {vendor.company_name}; awaiting manager assignment")
        notify_role("Manager", "escalation", f"Vendor work order drafted: {wo.title}",
                    f"Escalation chain exhausted; a draft vendor work order for {vendor.company_name} awaits assignment.", draft)
    else:
        return False
    wo.escalation_level = level + 1
    wo.last_escalated_at = now
    add_log(wo, None, "escalated", f"Escalation level {level + 1}")
    return True


def process_overdue(now):
    marked = escalated = 0
    rows = WorkOrder.query.filter(WorkOrder.status.in_(OPEN_STATUSES), WorkOrder.due_date < now).all()
    for wo in rows:
        if not wo.is_overdue:
            wo.is_overdue = True
            audit("status_change", "WORK_ORDER", wo.work_order_id, after={"is_overdue": True}, user_id=None)
            marked += 1
        window = timedelta(hours=wo.sla_escalation_hours or 4)
        if wo.escalation_level == 0 or (wo.last_escalated_at and now - wo.last_escalated_at >= window):
            escalated += int(_escalate(wo, now))
    return marked, escalated


def run_all(now=None):
    now = now or utcnow()
    generated = generate_all(now.date())
    reminders = send_reminders(now)
    marked, escalated = process_overdue(now)
    db.session.commit()
    sent, failed = dispatch_pending(max(now, utcnow()))
    return {"generated": generated, "reminders": reminders, "overdue_marked": marked,
            "escalations": escalated, "notifications_sent": sent, "notifications_failed": failed}

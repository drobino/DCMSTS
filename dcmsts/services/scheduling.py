"""Recurrence rules (RFC 5545 RRULE subset: FREQ + INTERVAL) and schedule instance generation."""
import calendar
from datetime import date, datetime, time, timedelta

from ..models import MaintSchedule, WorkOrder, db, get_setting
from .workorders import create_work_order

FREQS = ("DAILY", "WEEKLY", "MONTHLY", "YEARLY")


class RuleError(ValueError):
    pass


def parse_rule(rule):
    parts = dict(p.split("=", 1) for p in rule.upper().split(";") if "=" in p)
    freq = parts.get("FREQ")
    if freq not in FREQS:
        raise RuleError("FREQ must be one of " + ", ".join(FREQS))
    try:
        interval = int(parts.get("INTERVAL", "1"))
    except ValueError:
        raise RuleError("INTERVAL must be a whole number")
    if not 1 <= interval <= 366:
        raise RuleError("INTERVAL must be between 1 and 366")
    return freq, interval


def build_rule(freq, interval):
    parse_rule(f"FREQ={freq};INTERVAL={interval}")
    return f"FREQ={freq.upper()};INTERVAL={int(interval)}"


def _add_months(d, months, anchor_day):
    m = d.month - 1 + months
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(anchor_day, calendar.monthrange(y, m)[1]))


def occurrences(rule, start, until):
    """Yield dates from start (inclusive) through until (inclusive)."""
    freq, interval = parse_rule(rule)
    n = 0
    while True:
        if freq == "DAILY":
            d = start + timedelta(days=interval * n)
        elif freq == "WEEKLY":
            d = start + timedelta(weeks=interval * n)
        elif freq == "MONTHLY":
            d = _add_months(start, interval * n, start.day)
        else:
            d = _add_months(start, 12 * interval * n, start.day)
        if d > until:
            return
        yield d
        n += 1


def validate_schedule(rule, start, end, checklist):
    errors = []
    try:
        parse_rule(rule)
    except RuleError as e:
        errors.append(str(e))
    if start is None:
        errors.append("Start date is required.")
    elif end is not None and end < start:
        errors.append("End date must not be before the start date.")
    if not checklist:
        errors.append("At least one checklist item is required.")
    return errors


def overlapping_schedules(asset_id, exclude_id=None):
    q = MaintSchedule.query.filter_by(asset_id=asset_id, active=True)
    if exclude_id:
        q = q.filter(MaintSchedule.schedule_id != exclude_id)
    return q.all()


def generate_instances(schedule, today, horizon_days=None):
    """Create work orders for occurrences between next_due_date and today+horizon. Idempotent."""
    if not schedule.active or schedule.asset.lifecycle_status == "retired":
        return []
    horizon_days = horizon_days or get_setting("schedule_horizon_days")
    until = today + timedelta(days=horizon_days)
    if schedule.end_date:
        until = min(until, schedule.end_date)
    created = []
    last = None
    for d in occurrences(schedule.recurrence_rule, schedule.start_date, until):
        if d < schedule.next_due_date:
            continue
        due = datetime.combine(d, time(17, 0))
        exists = WorkOrder.query.filter_by(schedule_id=schedule.schedule_id, due_date=due).first()
        if not exists:
            created.append(create_work_order(
                asset=schedule.asset, type="preventive", priority=schedule.priority, title=schedule.title,
                checklist=[{"item": i, "done": False} for i in schedule.checklist_template],
                due_date=due, schedule=schedule, required_skill=schedule.required_skill,
                description=f"Compliance ref: {schedule.compliance_ref}" if schedule.compliance_ref else None,
            ))
        last = d
    if last is not None:
        nxt = next((d for d in occurrences(schedule.recurrence_rule, schedule.start_date, last + timedelta(days=800)) if d > last), None)
        if nxt is None or (schedule.end_date and nxt > schedule.end_date):
            schedule.next_due_date = last + timedelta(days=1)
        else:
            schedule.next_due_date = nxt
    return created


def generate_all(today):
    total = 0
    for s in MaintSchedule.query.filter_by(active=True).all():
        total += len(generate_instances(s, today))
    db.session.commit()
    return total

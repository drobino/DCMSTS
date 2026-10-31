import calendar as cal
from datetime import date, datetime, time, timedelta

from flask import Blueprint, g, render_template, request

from ..models import Asset, Notification, User, Vendor, WorkOrder, get_setting, utcnow
from ..security import login_required, require
from ..services.workorders import OPEN_STATUSES

bp = Blueprint("dashboard", __name__)


def scoped_work_orders(user):
    q = WorkOrder.query
    if user.can("workorders.view_all"):
        return q
    if user.role_name == "Vendor":
        return q.filter(WorkOrder.vendor_id == user.vendor_id)
    return q.filter(WorkOrder.assigned_to == user.user_id)


@bp.get("/")
@login_required
def index():
    user = g.user
    q = scoped_work_orders(user)
    now = utcnow()
    open_q = q.filter(WorkOrder.status.in_(OPEN_STATUSES))
    stats = {
        "open": open_q.count(),
        "overdue": open_q.filter(WorkOrder.is_overdue.is_(True)).count(),
        "unassigned": q.filter(WorkOrder.status == "unassigned").count(),
        "due_week": open_q.filter(WorkOrder.due_date <= now + timedelta(days=7)).count(),
    }
    my_tasks = open_q.order_by(WorkOrder.due_date).limit(15).all()
    high_risk = pending = None
    downtime = None
    if user.can("predict.view"):
        from ..services.predictive import latest_predictions, review_queue
        high_risk = [p for p in latest_predictions() if float(p.risk_score) >= get_setting("risk_threshold")][:10]
        pending = len(review_queue())
    if user.can("reports.view"):
        from ..services.reports import downtime_report
        downtime = downtime_report(now.date() - timedelta(days=90), now.date())
    return render_template("dashboard.html", stats=stats, my_tasks=my_tasks, high_risk=high_risk,
                           pending=pending, downtime=downtime)


@bp.get("/calendar")
@require("calendar.view")
def calendar_view():
    today = date.today()
    selected = None
    try:
        # ?date=YYYY-MM-DD (date picker) jumps to that day's month and highlights it; otherwise ?year=&month=
        if request.args.get("date"):
            selected = date.fromisoformat(request.args["date"])
            first = selected.replace(day=1)
        else:
            year = int(request.args.get("year", today.year))
            month = int(request.args.get("month", today.month))
            first = date(year, month, 1)
    except ValueError:
        selected = None
        first = date(today.year, today.month, 1)
    if not 1900 <= first.year <= 2999:  # keeps prev/next month arithmetic in range
        selected, first = None, date(today.year, today.month, 1)
    last = date(first.year, first.month, cal.monthrange(first.year, first.month)[1])
    q = scoped_work_orders(g.user).filter(
        WorkOrder.due_date.between(datetime.combine(first, time.min), datetime.combine(last, time.max)))
    filters = {k: request.args.get(k) or "" for k in ("asset_id", "assigned_to", "vendor_id", "status")}
    if filters["asset_id"]:
        q = q.filter(WorkOrder.asset_id == filters["asset_id"])
    if filters["assigned_to"] and g.user.can("workorders.view_all"):
        q = q.filter(WorkOrder.assigned_to == filters["assigned_to"])
    if filters["vendor_id"] and g.user.can("workorders.view_all"):
        q = q.filter(WorkOrder.vendor_id == filters["vendor_id"])
    if filters["status"] == "overdue":
        q = q.filter(WorkOrder.is_overdue.is_(True), WorkOrder.status != "completed")
    elif filters["status"]:
        q = q.filter(WorkOrder.status == filters["status"])
    by_day = {}
    for wo in q.order_by(WorkOrder.due_date):
        by_day.setdefault(wo.due_date.date(), []).append(wo)
    # the day panel shows the picked date, or today when viewing the current month
    if selected is None and (first.year, first.month) == (today.year, today.month):
        selected = today
    day_tasks = by_day.get(selected, []) if selected else []
    active_filters = {k: v for k, v in filters.items() if v}
    weeks = cal.Calendar(firstweekday=0).monthdatescalendar(first.year, first.month)
    prev_m = (first - timedelta(days=1)).replace(day=1)
    next_m = last + timedelta(days=1)
    ctx = {}
    if g.user.can("workorders.view_all"):
        ctx = {"assets": Asset.query.order_by(Asset.asset_tag).all(),
               "techs": User.query.filter(User.skills.isnot(None)).join(User.role).filter_by(role_name="Technician").all(),
               "vendors": Vendor.query.order_by(Vendor.company_name).all()}
    return render_template("calendar.html", weeks=weeks, by_day=by_day, first=first, selected=selected, prev_m=prev_m, next_m=next_m,
                           filters=filters, active_filters=active_filters, day_tasks=day_tasks, today=today, **ctx)


@bp.get("/notifications")
@login_required
def notifications():
    rows = (Notification.query.filter_by(user_id=g.user.user_id).order_by(Notification.created_at.desc())
            .limit(100).all())
    return render_template("notifications.html", rows=rows)

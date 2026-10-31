"""Report builders (UC-05, UC-18). Each returns {"title", "columns", "rows", "summary", "empty"}."""
import io
from collections import defaultdict
from datetime import datetime, time, timedelta

from ..models import Asset, WorkOrder, utcnow


def _period(start, end):
    return datetime.combine(start, time.min), datetime.combine(end, time.max)


def _hours(delta):
    return round(delta.total_seconds() / 3600, 2)


def compliance_report(start, end, category=None):
    s, e = _period(start, end)
    now = utcnow()
    q = WorkOrder.query.join(Asset).filter(WorkOrder.type == "preventive", WorkOrder.due_date.between(s, e))
    if category:
        q = q.filter(Asset.category == category)
    by_cat = defaultdict(lambda: {"due": 0, "completed": 0, "on_time": 0, "overdue_open": 0})
    for wo in q:
        c = by_cat[wo.asset.category]
        c["due"] += 1
        if wo.status == "completed":
            c["completed"] += 1
            c["on_time"] += int(wo.completed_at <= wo.due_date)
        elif wo.due_date < now:
            c["overdue_open"] += 1
    rows = [[cat, v["due"], v["completed"], v["on_time"], v["overdue_open"],
             f"{(v['completed'] / v['due']):.0%}" if v["due"] else "-"] for cat, v in sorted(by_cat.items())]
    due = sum(v["due"] for v in by_cat.values())
    done = sum(v["completed"] for v in by_cat.values())
    on_time = sum(v["on_time"] for v in by_cat.values())
    return {
        "title": f"Preventive maintenance compliance {start} to {end}",
        "columns": ["Category", "Due", "Completed", "On time", "Overdue (open)", "Completion rate"],
        "rows": rows, "empty": due == 0,
        "summary": {"Tasks due": due, "Completed": done, "Completed on time": on_time,
                    "Completion rate": f"{done / due:.0%}" if due else "-",
                    "On-time rate": f"{on_time / due:.0%}" if due else "-"},
    }


def sla_report(start, end, category=None):
    s, e = _period(start, end)
    now = utcnow()
    q = WorkOrder.query.join(Asset).filter(WorkOrder.created_at.between(s, e), WorkOrder.sla_resolution_hours.isnot(None))
    if category:
        q = q.filter(Asset.category == category)
    by_p = defaultdict(lambda: {"count": 0, "resolved": 0, "within": 0, "breached": 0, "hours": []})
    for wo in q:
        b = by_p[wo.priority]
        b["count"] += 1
        deadline = wo.created_at + timedelta(hours=wo.sla_resolution_hours)
        if wo.completed_at:
            b["resolved"] += 1
            b["hours"].append(_hours(wo.completed_at - wo.created_at))
            if wo.completed_at <= deadline:
                b["within"] += 1
            else:
                b["breached"] += 1
        elif now > deadline:
            b["breached"] += 1
    order = ["critical", "high", "medium", "low"]
    rows = [[p, v["count"], v["resolved"], v["within"], v["breached"],
             round(sum(v["hours"]) / len(v["hours"]), 1) if v["hours"] else "-"]
            for p, v in sorted(by_p.items(), key=lambda kv: order.index(kv[0]))]
    total = sum(v["count"] for v in by_p.values())
    within = sum(v["within"] for v in by_p.values())
    return {
        "title": f"SLA compliance {start} to {end}",
        "columns": ["Priority", "Work orders", "Resolved", "Within SLA", "Breached", "Avg resolution (h)"],
        "rows": rows, "empty": total == 0,
        "summary": {"Work orders": total, "Within SLA": within,
                    "Breaches": sum(v["breached"] for v in by_p.values()),
                    "SLA attainment": f"{within / total:.0%}" if total else "-"},
    }


def downtime_report(start, end, category=None, asset_id=None):
    s, e = _period(start, end)
    period_hours = _hours(e - s)
    q = WorkOrder.query.join(Asset).filter(WorkOrder.downtime_start.isnot(None), WorkOrder.downtime_start.between(s, e))
    if category:
        q = q.filter(Asset.category == category)
    if asset_id:
        q = q.filter(WorkOrder.asset_id == asset_id)
    per = defaultdict(lambda: {"failures": 0, "repairs": [], "planned": 0.0, "ongoing": 0, "causes": defaultdict(int)})
    assets = {}
    for wo in q:
        assets[wo.asset_id] = wo.asset
        a = per[wo.asset_id]
        if wo.downtime_end is None:
            a["ongoing"] += 1  # excluded from MTTR rather than counted as zero (UC-18 2a)
            continue
        hrs = _hours(wo.downtime_end - wo.downtime_start)
        if wo.downtime_planned:
            a["planned"] += hrs
        else:
            a["failures"] += 1
            a["repairs"].append(hrs)
            a["causes"][wo.downtime_cause or "unspecified"] += 1
    rows = []
    for aid, v in sorted(per.items(), key=lambda kv: -sum(kv[1]["repairs"])):
        unplanned = sum(v["repairs"])
        mttr = round(unplanned / len(v["repairs"]), 2) if v["repairs"] else "-"
        mtbf = round((period_hours - unplanned) / v["failures"], 1) if v["failures"] else "-"
        top_cause = max(v["causes"], key=v["causes"].get) if v["causes"] else "-"
        rows.append([assets[aid].asset_tag, assets[aid].category, v["failures"], round(unplanned, 2), mttr, mtbf,
                     round(v["planned"], 2), v["ongoing"], top_cause])
    all_repairs = [h for v in per.values() for h in v["repairs"]]
    return {
        "title": f"Downtime analysis {start} to {end}",
        "columns": ["Asset", "Category", "Unplanned failures", "Unplanned downtime (h)", "MTTR (h)", "MTBF (h)",
                    "Planned downtime (h)", "Ongoing incidents", "Top cause"],
        "rows": rows, "empty": not per,
        "summary": {"Unplanned downtime (h)": round(sum(all_repairs), 2), "Failures": len(all_repairs),
                    "Overall MTTR (h)": round(sum(all_repairs) / len(all_repairs), 2) if all_repairs else "-",
                    "Planned downtime (h)": round(sum(v["planned"] for v in per.values()), 2),
                    "Ongoing incidents": sum(v["ongoing"] for v in per.values())},
    }


def asset_history_report(asset):
    wos = WorkOrder.query.filter_by(asset_id=asset.asset_id).order_by(WorkOrder.due_date.desc()).all()
    rows = [[w.due_date.strftime("%Y-%m-%d"), w.type, w.priority, w.title, w.status,
             w.completed_at.strftime("%Y-%m-%d %H:%M") if w.completed_at else "-",
             w.assignee.full_name if w.assignee else (w.vendor.company_name if w.vendor else "-"),
             w.root_cause or ""] for w in wos]
    return {"title": f"Maintenance history: {asset.asset_tag} {asset.name}",
            "columns": ["Due", "Type", "Priority", "Title", "Status", "Completed", "Assignee", "Root cause"],
            "rows": rows, "empty": not rows, "summary": {"Work orders": len(rows)}}


# ---------- export ----------

def to_xlsx(report):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Report"
    ws.append([report["title"]])
    for k, v in report["summary"].items():
        ws.append([k, v])
    ws.append([])
    ws.append(report["columns"])
    for row in report["rows"]:
        ws.append([_safe_cell(c) for c in row])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _safe_cell(value):
    # Prevent spreadsheet formula injection from user-entered text.
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@") and not value.replace(".", "", 1).lstrip("-").isdigit():
        return "'" + value
    return value


def to_pdf(report):
    from fpdf import FPDF
    pdf = FPDF(orientation="L", format="A4")
    pdf.set_auto_page_break(True, margin=12)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 9, _latin(report["title"]), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", size=9)
    pdf.cell(0, 6, f"Generated {utcnow():%Y-%m-%d %H:%M} UTC", new_x="LMARGIN", new_y="NEXT")
    for k, v in report["summary"].items():
        pdf.cell(0, 6, _latin(f"{k}: {v}"), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)
    with pdf.table(text_align="LEFT", line_height=5) as table:
        table.row([_latin(c) for c in report["columns"]])
        for r in report["rows"]:
            table.row([_latin(str(c))[:80] for c in r])
    return bytes(pdf.output())


def _latin(text):
    return str(text).encode("latin-1", "replace").decode("latin-1")

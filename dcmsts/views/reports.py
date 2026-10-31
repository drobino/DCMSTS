from datetime import date, timedelta

from flask import Blueprint, Response, abort, flash, render_template, request

from ..models import Asset, db
from ..security import audit, require
from ..services import reports as rpt

bp = Blueprint("reports", __name__, url_prefix="/reports")
TYPES = {"compliance": rpt.compliance_report, "sla": rpt.sla_report, "downtime": rpt.downtime_report}


def _build():
    kind = request.args.get("type", "compliance")
    if kind == "asset":
        asset = db.session.get(Asset, request.args.get("asset_id", "")) or abort(404)
        return kind, rpt.asset_history_report(asset)
    if kind not in TYPES:
        abort(400)
    try:
        end = date.fromisoformat(request.args.get("end") or date.today().isoformat())
        start = date.fromisoformat(request.args.get("start") or (end - timedelta(days=90)).isoformat())
    except ValueError:
        abort(400)
    if start > end:
        abort(400)
    return kind, TYPES[kind](start, end, request.args.get("category") or None)


@bp.get("/")
@require("reports.view")
def index():
    report = None
    if request.args.get("type"):
        _, report = _build()
        if report["empty"]:
            flash("No maintenance data in the selected range; no report generated.", "info")
            report = None
    categories = [c for (c,) in db.session.query(Asset.category).distinct().order_by(Asset.category)]
    return render_template("reports/index.html", report=report, categories=categories, args=request.args)


@bp.get("/export.<fmt>")
@require("reports.view")
def export(fmt):
    kind, report = _build()
    if report["empty"]:
        abort(404)
    audit("export", "REPORT", after={"type": kind, "format": fmt, "args": dict(request.args)})
    db.session.commit()
    name = f"dcmsts-{kind}-{date.today()}"
    if fmt == "xlsx":
        return Response(rpt.to_xlsx(report),
                        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f"attachment; filename={name}.xlsx"})
    if fmt == "pdf":
        return Response(rpt.to_pdf(report), mimetype="application/pdf",
                        headers={"Content-Disposition": f"attachment; filename={name}.pdf"})
    abort(404)

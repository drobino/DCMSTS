from datetime import date

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from ..models import Role, User, Vendor, WorkOrder, db
from ..security import audit, hash_password, password_problems, require, snapshot
from ..services.workorders import workload_score
from . import date_field, field

bp = Blueprint("people", __name__, url_prefix="/people")
VENDOR_FIELDS = ["company_name", "contact_email", "service_type", "skills", "contract_start", "contract_end"]


def _performance(user_id):
    done = WorkOrder.query.filter(WorkOrder.assigned_to == user_id, WorkOrder.completed_at.isnot(None)).all()
    hours = [(w.completed_at - w.created_at).total_seconds() / 3600 for w in done]
    on_time = sum(1 for w in done if w.completed_at <= w.due_date)
    return {"completed": len(done), "on_time": on_time,
            "avg_hours": round(sum(hours) / len(hours), 1) if hours else None}


@bp.get("/technicians")
@require("people.manage")
def technicians():
    techs = User.query.join(Role).filter(Role.role_name == "Technician").order_by(User.full_name).all()
    rows = [{"user": t, "workload": workload_score(t.user_id), "perf": _performance(t.user_id)} for t in techs]
    return render_template("people/technicians.html", rows=rows, today=date.today())


@bp.route("/technicians/new", methods=["GET", "POST"])
@require("technicians.create")
def new_technician():
    """Managers and administrators add technician accounts. The role is fixed to Technician, so this route
    cannot create managers or administrators (that stays under Admin -> Users, administrators only)."""
    if request.method == "POST":
        try:
            email = field("email", 200, True).lower()
            if "@" not in email:
                raise ValueError("Email is invalid.")
            if User.query.filter_by(email=email).first():
                raise ValueError("Email already in use.")
            password = request.form.get("password", "")
            if problems := password_problems(password):
                raise ValueError("Password needs " + ", ".join(problems) + ".")
            role = Role.query.filter_by(role_name="Technician").one()
            t = User(full_name=field("full_name", 150, True), email=email, phone=field("phone", 20),
                     password_hash=hash_password(password), role_id=role.role_id, skills=field("skills", 300) or "")
            db.session.add(t)
            db.session.flush()
            audit("create", "USER", t.user_id,
                  after={"email": email, "role": "Technician", "skills": t.skills, "via": "people"})
            db.session.commit()
            flash(f"Technician {t.full_name} added. Share the initial password with them separately.", "info")
            return redirect(url_for("people.technician", user_id=t.user_id))
        except ValueError as e:
            db.session.rollback()
            flash(str(e), "error")
    return render_template("people/technician_form.html", form=request.form)


@bp.route("/technicians/<user_id>", methods=["GET", "POST"])
@require("people.manage")
def technician(user_id):
    t = db.session.get(User, user_id) or abort(404)
    if t.role_name != "Technician":
        abort(404)
    if request.method == "POST":
        try:
            certs = []
            for line in (request.form.get("certifications") or "").splitlines():
                if not line.strip():
                    continue
                name, _, exp = line.partition("|")
                exp = exp.strip()
                if exp:
                    date.fromisoformat(exp)
                certs.append({"name": name.strip()[:80], "expires": exp or None})
            before = {"skills": t.skills, "certifications": t.certifications}
            t.skills = field("skills", 300) or ""
            t.certifications = certs
            audit("update", "USER", t.user_id, before=before, after={"skills": t.skills, "certifications": certs})
            db.session.commit()
            flash("Profile saved.", "info")
            return redirect(url_for("people.technicians"))
        except ValueError:
            flash("Certification lines must be 'Name | YYYY-MM-DD' (date optional).", "error")
    recent = (WorkOrder.query.filter_by(assigned_to=t.user_id).order_by(WorkOrder.due_date.desc()).limit(20).all())
    return render_template("people/technician.html", t=t, perf=_performance(t.user_id), recent=recent)


@bp.get("/vendors")
@require("people.manage")
def vendors():
    return render_template("people/vendors.html", vendors=Vendor.query.order_by(Vendor.company_name).all(),
                           today=date.today())


@bp.route("/vendors/new", methods=["GET", "POST"], defaults={"vendor_id": None})
@bp.route("/vendors/<vendor_id>", methods=["GET", "POST"])
@require("people.manage")
def vendor(vendor_id):
    v = (db.session.get(Vendor, vendor_id) or abort(404)) if vendor_id else Vendor()
    if request.method == "POST":
        before = snapshot(v, VENDOR_FIELDS) if vendor_id else None
        try:
            v.company_name = field("company_name", 150, True)
            v.contact_email = field("contact_email", 200, True)
            if "@" not in v.contact_email:
                raise ValueError("Contact email is invalid.")
            v.service_type = field("service_type", 100)
            v.skills = field("skills", 300) or ""
            v.contract_start, v.contract_end = date_field("contract_start"), date_field("contract_end")
            if v.contract_start and v.contract_end and v.contract_end < v.contract_start:
                raise ValueError("Contract end must be after start.")
            if not vendor_id:
                db.session.add(v)
            db.session.flush()
            audit("update" if vendor_id else "create", "VENDOR", v.vendor_id, before=before,
                  after=snapshot(v, VENDOR_FIELDS))
            db.session.commit()
            flash("Vendor saved.", "info")
            return redirect(url_for("people.vendors"))
        except ValueError as e:
            db.session.rollback()
            flash(str(e), "error")
            if vendor_id:
                v = db.session.get(Vendor, vendor_id)
    return render_template("people/vendor_form.html", v=v)

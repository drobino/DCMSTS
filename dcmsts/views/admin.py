from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from ..models import (DEFAULT_SETTINGS, PRIORITIES, USER_STATUSES, AuditLog, Notification, Role, Setting, SlaPolicy,
                      User, Vendor, db, utcnow)
from ..security import (audit, hash_password, invalidate_sessions, password_problems, require, verify_audit_chain)
from . import choice_field, field, number_field

bp = Blueprint("admin", __name__, url_prefix="/admin")


def _active_admins():
    return User.query.join(Role).filter(Role.role_name == "Administrator", User.status == "active").count()


@bp.get("/users")
@require("users.manage")
def users():
    return render_template("admin/users.html", users=User.query.order_by(User.full_name).all(),
                           roles=Role.query.order_by(Role.role_name).all(), now=utcnow())


@bp.route("/users/new", methods=["GET", "POST"])
@require("users.manage")
def new_user():
    roles, vendors = Role.query.order_by(Role.role_name).all(), Vendor.query.order_by(Vendor.company_name).all()
    if request.method == "POST":
        try:
            email = field("email", 200, True).lower()
            if "@" not in email:
                raise ValueError("Email is invalid.")
            if User.query.filter_by(email=email).first():
                raise ValueError("Email already in use.")
            role = db.session.get(Role, request.form.get("role_id")) or abort(400)
            password = request.form.get("password", "")
            if problems := password_problems(password):
                raise ValueError("Password needs " + ", ".join(problems) + ".")
            vendor_id = request.form.get("vendor_id") or None
            if role.role_name == "Vendor" and not (vendor_id and db.session.get(Vendor, vendor_id)):
                raise ValueError("Vendor users must be linked to a vendor organisation.")
            u = User(full_name=field("full_name", 150, True), email=email, phone=field("phone", 20),
                     password_hash=hash_password(password), role_id=role.role_id,
                     vendor_id=vendor_id if role.role_name == "Vendor" else None,
                     skills=field("skills", 300) or "")
            db.session.add(u)
            db.session.flush()
            audit("create", "USER", u.user_id, after={"email": email, "role": role.role_name})
            db.session.commit()
            flash("User created.", "info")
            return redirect(url_for("admin.users"))
        except ValueError as e:
            db.session.rollback()
            flash(str(e), "error")
    return render_template("admin/user_form.html", roles=roles, vendors=vendors, form=request.form)


@bp.post("/users/<user_id>")
@require("users.manage")
def update_user(user_id):
    u = db.session.get(User, user_id) or abort(404)
    role = db.session.get(Role, request.form.get("role_id")) or abort(400)
    status = request.form.get("status")
    if status not in USER_STATUSES:
        abort(400)
    was_admin_active = u.role_name == "Administrator" and u.status == "active"
    loses_admin = was_admin_active and (role.role_name != "Administrator" or status != "active")
    if loses_admin and _active_admins() <= 1:
        flash("Cannot remove the last active administrator.", "error")
        return redirect(url_for("admin.users"))
    if role.role_name == "Vendor" and not u.vendor_id:
        flash("Link a vendor before assigning the Vendor role (recreate the user with a vendor).", "error")
        return redirect(url_for("admin.users"))
    before = {"role": u.role_name, "status": u.status}
    if role.role_id != u.role_id or status != u.status:
        u.role_id, u.status = role.role_id, status
        invalidate_sessions(u)  # privileges changed: force re-authentication (UC-19)
        db.session.flush()
        db.session.refresh(u)
        audit("permission_change", "USER", u.user_id, before=before, after={"role": role.role_name, "status": status})
        db.session.commit()
        flash("User updated; existing sessions were ended.", "info")
    return redirect(url_for("admin.users"))


@bp.post("/users/<user_id>/reset-mfa")
@require("users.manage")
def reset_mfa(user_id):
    u = db.session.get(User, user_id) or abort(404)
    u.mfa_enabled, u.mfa_secret = False, None
    invalidate_sessions(u)
    audit("mfa_reset", "USER", u.user_id)
    db.session.commit()
    flash("MFA reset; the user will enrol again at next login.", "info")
    return redirect(url_for("admin.users"))


@bp.post("/users/<user_id>/reset-password")
@require("users.manage")
def reset_password(user_id):
    u = db.session.get(User, user_id) or abort(404)
    password = request.form.get("password", "")
    if problems := password_problems(password):
        flash("Password needs " + ", ".join(problems) + ".", "error")
        return redirect(url_for("admin.users"))
    u.password_hash = hash_password(password)
    u.failed_logins, u.locked_until = 0, None
    invalidate_sessions(u)
    audit("password_reset", "USER", u.user_id)
    db.session.commit()
    flash("Password reset and sessions ended.", "info")
    return redirect(url_for("admin.users"))


@bp.route("/settings", methods=["GET", "POST"])
@require("settings.manage")
def settings():
    from ..models import get_setting
    if request.method == "POST":
        try:
            new = {
                "risk_threshold": number_field("risk_threshold", True, 0.01),
                "review_threshold": number_field("review_threshold", True, 0),
                "reminder_hours": number_field("reminder_hours", True, 1),
                "max_reschedules": number_field("max_reschedules", True, 0, int),
                "telemetry_max_invalid_ratio": number_field("telemetry_max_invalid_ratio", True, 0),
                "schedule_horizon_days": number_field("schedule_horizon_days", True, 7, int),
                "evidence_required_categories": [c.strip() for c in (request.form.get("evidence_required_categories") or "").split(",") if c.strip()],
            }
            if new["risk_threshold"] > 1 or new["review_threshold"] > new["risk_threshold"]:
                raise ValueError("Thresholds must satisfy 0 <= review <= risk <= 1.")
            if new["telemetry_max_invalid_ratio"] > 1 or new["schedule_horizon_days"] > 730:
                raise ValueError("Invalid ratio (0..1) or horizon (7..730 days).")
            before = {k: get_setting(k) for k in new}
            for k, v in new.items():
                row = db.session.get(Setting, k) or Setting(key=k)
                row.value = v
                db.session.add(row)
            audit("update", "SETTINGS", before=before, after=new)
            db.session.commit()
            flash("Settings saved.", "info")
            return redirect(url_for("admin.settings"))
        except ValueError as e:
            db.session.rollback()
            flash(str(e), "error")
    return render_template("admin/settings.html", s={k: get_setting(k) for k in DEFAULT_SETTINGS})


@bp.route("/sla", methods=["GET", "POST"])
@require("sla.manage")
def sla():
    managers = User.query.join(Role).filter(Role.role_name.in_(("Manager", "Administrator")),
                                            User.status == "active").order_by(User.full_name).all()
    if request.method == "POST":
        try:
            priority = choice_field("priority", PRIORITIES)
            category = field("category", 80)
            response = number_field("response_hours", True)
            resolution = number_field("resolution_hours", True)
            escalation = number_field("escalation_hours", True)
            if min(response, resolution, escalation) <= 0:
                raise ValueError("Thresholds must be greater than zero.")
            valid_ids = {m.user_id for m in managers}
            chain = [uid for uid in request.form.getlist("chain") if uid in valid_ids]
            vendor_id = request.form.get("final_vendor_id") or None
            if vendor_id and not db.session.get(Vendor, vendor_id):
                raise ValueError("Unknown vendor.")
            p = SlaPolicy.query.filter_by(priority=priority, category=category).first() or SlaPolicy(
                priority=priority, category=category)
            before = {"response": p.response_hours, "resolution": p.resolution_hours} if p.policy_id else None
            p.response_hours, p.resolution_hours, p.escalation_hours = response, resolution, escalation
            p.escalation_chain, p.final_vendor_id = chain, vendor_id
            db.session.add(p)
            db.session.flush()
            audit("update" if before else "create", "SLA_POLICY", p.policy_id, before=before,
                  after={"priority": priority, "category": category, "response": response, "resolution": resolution,
                         "escalation": escalation, "chain": chain, "final_vendor": vendor_id})
            db.session.commit()
            flash("SLA policy saved. It applies to work orders created from now on.", "info")
            return redirect(url_for("admin.sla"))
        except ValueError as e:
            db.session.rollback()
            flash(str(e), "error")
    policies = SlaPolicy.query.order_by(SlaPolicy.category, SlaPolicy.priority).all()
    names = {m.user_id: m.full_name for m in User.query.all()}
    return render_template("admin/sla.html", policies=policies, managers=managers, names=names,
                           vendors=Vendor.query.order_by(Vendor.company_name).all(), priorities=PRIORITIES)


@bp.get("/audit")
@require("audit.view")
def audit_log():
    q = AuditLog.query
    if request.args.get("action"):
        q = q.filter(AuditLog.action == request.args["action"][:50])
    if request.args.get("entity"):
        q = q.filter(AuditLog.entity_type == request.args["entity"][:50])
    rows = q.order_by(AuditLog.audit_id.desc()).limit(300).all()
    ok, bad = verify_audit_chain()
    names = {u.user_id: u.email for u in User.query.all()}
    return render_template("admin/audit.html", rows=rows, chain_ok=ok, bad=bad, names=names)


@bp.get("/notifications")
@require("audit.view")
def notifications():
    rows = Notification.query.order_by(Notification.created_at.desc()).limit(300).all()
    names = {u.user_id: u.email for u in User.query.all()}
    return render_template("admin/notifications.html", rows=rows, names=names)

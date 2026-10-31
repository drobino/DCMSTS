import hashlib
import json
import re
from datetime import timedelta
from functools import wraps

import pyotp
from flask import abort, current_app, g, has_request_context, request, session
from werkzeug.security import check_password_hash, generate_password_hash

from .models import AuditLog, Role, User, db, utcnow

PERMISSIONS = {
    "Administrator": [
        "users.manage", "settings.manage", "audit.view", "assets.view", "assets.manage",
        "schedules.manage", "workorders.view_all", "workorders.create", "workorders.assign",
        "workorders.update_own", "people.manage", "technicians.create", "predict.view", "predict.review", "predict.train",
        "telemetry.ingest", "telemetry.log", "reports.view", "sla.manage", "calendar.view",
    ],
    "Manager": [
        "assets.view", "assets.manage", "schedules.manage", "workorders.view_all", "workorders.create",
        "workorders.assign", "workorders.update_own", "people.manage", "technicians.create", "predict.view",
        "predict.review", "predict.train", "telemetry.ingest", "telemetry.log", "reports.view", "sla.manage", "calendar.view",
    ],
    "Technician": ["assets.view", "workorders.create", "workorders.update_own", "telemetry.log", "calendar.view"],
    "Vendor": ["workorders.update_own", "calendar.view"],
}
MFA_REQUIRED_ROLES = {"Administrator", "Manager"}


def ensure_roles():
    for name, perms in PERMISSIONS.items():
        role = Role.query.filter_by(role_name=name).first()
        if role is None:
            db.session.add(Role(role_name=name, permissions=perms))
        else:
            role.permissions = perms
    db.session.commit()


def role_by_name(name):
    return Role.query.filter_by(role_name=name).one()


# ---------- passwords ----------

def password_problems(password):
    problems = []
    if len(password) < 12:
        problems.append("at least 12 characters")
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        problems.append("both letters and digits")
    return problems


def hash_password(password):
    return generate_password_hash(password)  # scrypt with per-hash salt


def verify_password(user, password):
    return check_password_hash(user.password_hash, password)


# ---------- audit ----------

def _canonical(entry):
    payload = {
        "user_id": entry.user_id, "entity_type": entry.entity_type, "entity_id": entry.entity_id,
        "action": entry.action, "before": entry.before_value, "after": entry.after_value,
        "ip": entry.ip_address, "ts": entry.timestamp.isoformat(), "prev": entry.prev_hash,
    }
    return json.dumps(payload, sort_keys=True, default=str)


def audit(action, entity_type, entity_id=None, before=None, after=None, user_id=None):
    """Append a hash-chained audit entry. Committed with the caller's transaction."""
    if user_id is None and has_request_context() and getattr(g, "user", None) is not None:
        user_id = g.user.user_id
    last = AuditLog.query.order_by(AuditLog.audit_id.desc()).first()
    entry = AuditLog(
        user_id=user_id, entity_type=entity_type, entity_id=entity_id, action=action,
        before_value=before, after_value=after, timestamp=utcnow(),
        prev_hash=last.hash if last else "0" * 64,
    )
    if has_request_context():
        entry.ip_address = request.remote_addr
        entry.user_agent = (request.user_agent.string or "")[:200]
    entry.hash = hashlib.sha256(_canonical(entry).encode()).hexdigest()
    db.session.add(entry)
    db.session.flush()
    return entry


def verify_audit_chain():
    """Return (ok, first_bad_audit_id)."""
    prev = "0" * 64
    for entry in AuditLog.query.order_by(AuditLog.audit_id):
        if entry.prev_hash != prev or hashlib.sha256(_canonical(entry).encode()).hexdigest() != entry.hash:
            return False, entry.audit_id
        prev = entry.hash
    return True, None


def snapshot(obj, fields):
    out = {}
    for f in fields:
        v = getattr(obj, f)
        out[f] = v.isoformat() if hasattr(v, "isoformat") else (str(v) if v is not None and not isinstance(v, (int, float, bool, str, list, dict)) else v)
    return out


# ---------- authentication ----------

def attempt_login(email, password):
    """Return (user, error). Applies lockout and audits every outcome."""
    cfg = current_app.config
    user = User.query.filter(db.func.lower(User.email) == email.strip().lower()).first()
    now = utcnow()
    if user is None:
        audit("login_failed", "USER", after={"email": email[:200], "reason": "unknown"})
        db.session.commit()
        return None, "Invalid email or password."
    if user.locked_until and user.locked_until > now:
        audit("login_failed", "USER", user.user_id, after={"reason": "locked"}, user_id=user.user_id)
        db.session.commit()
        return None, "Account temporarily locked. Try again later."
    if not verify_password(user, password):
        user.failed_logins += 1
        reason = "bad_password"
        if user.failed_logins >= cfg["LOGIN_MAX_FAILURES"]:
            user.locked_until = now + timedelta(minutes=cfg["LOCKOUT_MINUTES"])
            user.failed_logins = 0
            reason = "locked_out"
            from .services.notify import notify_role
            notify_role("Administrator", "security_alert", "Account locked",
                        f"Account {user.email} was locked after repeated failed logins.")
        audit("login_failed", "USER", user.user_id, after={"reason": reason}, user_id=user.user_id)
        db.session.commit()
        return None, "Invalid email or password."
    if user.status != "active":
        audit("login_failed", "USER", user.user_id, after={"reason": user.status}, user_id=user.user_id)
        db.session.commit()
        return None, "Account is not active."
    user.failed_logins = 0
    user.locked_until = None
    db.session.commit()
    return user, None


def mfa_required(user):
    return user.role_name in MFA_REQUIRED_ROLES


def verify_totp(secret, code):
    return bool(secret) and pyotp.TOTP(secret).verify((code or "").strip(), valid_window=1)


def start_session(user):
    session.clear()
    session["uid"] = user.user_id
    session["ver"] = user.session_version
    session["seen"] = utcnow().timestamp()
    session.permanent = True
    audit("login", "USER", user.user_id, user_id=user.user_id)
    db.session.commit()


def load_current_user():
    g.user = None
    uid = session.get("uid")
    if not uid:
        return
    idle = current_app.config["IDLE_TIMEOUT_MINUTES"] * 60
    now = utcnow().timestamp()
    user = db.session.get(User, uid)
    if (user is None or user.status != "active" or session.get("ver") != user.session_version
            or now - session.get("seen", 0) > idle):
        session.clear()
        g.session_expired = True
        return
    session["seen"] = now
    g.user = user


def invalidate_sessions(user):
    user.session_version += 1


# ---------- authorization ----------

def deny(reason="forbidden"):
    audit("access_denied", "REQUEST", after={"path": request.path, "method": request.method, "reason": reason})
    db.session.commit()
    abort(403)


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if g.user is None:
            from flask import redirect, url_for
            return redirect(url_for("auth.login", next=request.full_path if request.method == "GET" else None,
                                    expired=1 if g.get("session_expired") else None))
        return view(*args, **kwargs)
    return wrapper


def require(permission):
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapper(*args, **kwargs):
            if not g.user.can(permission):
                deny(f"missing {permission}")
            return view(*args, **kwargs)
        return wrapper
    return decorator


def can_access_work_order(user, wo):
    if user.can("workorders.view_all"):
        return True
    if user.role_name == "Vendor":
        return wo.vendor_id is not None and wo.vendor_id == user.vendor_id
    return wo.assigned_to == user.user_id

import time
from collections import defaultdict, deque

import pyotp
from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, session, url_for

from ..models import User, db, utcnow
from ..security import (attempt_login, audit, hash_password, invalidate_sessions, login_required, mfa_required,
                        password_problems, start_session, verify_password, verify_totp)
from . import safe_next

bp = Blueprint("auth", __name__)

# Per-process, per-IP rate limit for authentication endpoints (account lockout is the persistent control).
_attempts = defaultdict(deque)
RATE_LIMIT, RATE_WINDOW = 20, 300


def _rate_limited():
    if current_app.config.get("TESTING_DISABLE_RATE_LIMIT"):
        return False
    now = time.monotonic()
    q = _attempts[request.remote_addr]
    while q and now - q[0] > RATE_WINDOW:
        q.popleft()
    q.append(now)
    return len(q) > RATE_LIMIT


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("dashboard.index"))
    if request.method == "POST":
        if _rate_limited():
            abort(429)
        user, error = attempt_login(request.form.get("email", ""), request.form.get("password", ""))
        if error:
            flash(error, "error")
            return render_template("auth/login.html"), 401
        nxt = safe_next(request.args.get("next"))
        if mfa_required(user) or user.mfa_enabled:
            session.clear()
            session["pending_uid"] = user.user_id
            session["pending_at"] = utcnow().timestamp()
            session["next"] = nxt
            return redirect(url_for("auth.mfa_setup" if not user.mfa_enabled else "auth.mfa"))
        start_session(user)
        return redirect(nxt or url_for("dashboard.index"))
    if request.args.get("expired"):
        flash("Your session expired. Please sign in again.", "info")
    return render_template("auth/login.html")


def _pending_user():
    uid, at = session.get("pending_uid"), session.get("pending_at", 0)
    if not uid or utcnow().timestamp() - at > 300:
        session.clear()
        return None
    return db.session.get(User, uid)


@bp.route("/mfa", methods=["GET", "POST"])
def mfa():
    user = _pending_user()
    if user is None or not user.mfa_enabled:
        return redirect(url_for("auth.login"))
    if request.method == "POST":
        if _rate_limited():
            abort(429)
        if verify_totp(user.mfa_secret, request.form.get("code")):
            nxt = session.get("next")
            start_session(user)
            return redirect(nxt or url_for("dashboard.index"))
        audit("mfa_failed", "USER", user.user_id, user_id=user.user_id)
        db.session.commit()
        flash("Invalid code.", "error")
    return render_template("auth/mfa.html")


@bp.route("/mfa/setup", methods=["GET", "POST"])
def mfa_setup():
    user = _pending_user()
    if user is None or user.mfa_enabled:
        return redirect(url_for("auth.login"))
    secret = session.get("mfa_secret") or pyotp.random_base32()
    session["mfa_secret"] = secret
    if request.method == "POST":
        if verify_totp(secret, request.form.get("code")):
            user.mfa_secret, user.mfa_enabled = secret, True
            audit("mfa_enrolled", "USER", user.user_id, user_id=user.user_id)
            db.session.commit()
            nxt = session.get("next")
            start_session(user)
            flash("Multi-factor authentication enabled.", "info")
            return redirect(nxt or url_for("dashboard.index"))
        flash("Invalid code. Check your authenticator app's clock.", "error")
    uri = pyotp.TOTP(secret).provisioning_uri(name=user.email, issuer_name="DCMSTS")
    return render_template("auth/mfa_setup.html", secret=secret, uri=uri)


@bp.post("/logout")
def logout():
    if g.user:
        invalidate_sessions(g.user)
        audit("logout", "USER", g.user.user_id)
        db.session.commit()
    session.clear()
    return redirect(url_for("auth.login"))


@bp.route("/password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        current, new = request.form.get("current", ""), request.form.get("new", "")
        if not verify_password(g.user, current):
            flash("Current password is incorrect.", "error")
        elif problems := password_problems(new):
            flash("New password needs " + ", ".join(problems) + ".", "error")
        elif new != request.form.get("confirm"):
            flash("Passwords do not match.", "error")
        else:
            g.user.password_hash = hash_password(new)
            invalidate_sessions(g.user)
            audit("password_change", "USER", g.user.user_id)
            db.session.commit()
            session.clear()
            flash("Password changed. Please sign in again.", "info")
            return redirect(url_for("auth.login"))
    return render_template("auth/password.html")

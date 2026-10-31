import json
import logging
import os
from datetime import date

import click
from flask import Flask, g, render_template
from flask_wtf.csrf import CSRFError, CSRFProtect

from .config import Config
from .models import db, install_db_guards
from .security import load_current_user

csrf = CSRFProtect()

CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
       "object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")


def create_app(overrides=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(Config)
    if overrides:
        app.config.update(overrides)
    os.makedirs(app.instance_path, exist_ok=True)
    if app.config["SQLALCHEMY_DATABASE_URI"] == "sqlite:///dcmsts.db":
        app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + os.path.join(app.instance_path, "dcmsts.db")
    app.config["UPLOAD_DIR"] = app.config["UPLOAD_DIR"] or os.path.join(app.instance_path, "uploads")
    app.config["BACKUP_DIR"] = app.config["BACKUP_DIR"] or os.path.join(app.instance_path, "backups")
    if not app.config["SECRET_KEY"]:
        raise RuntimeError("DCMSTS_SECRET_KEY must be set (see .env.example).")
    os.makedirs(app.config["UPLOAD_DIR"], exist_ok=True)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    db.init_app(app)
    csrf.init_app(app)

    from .views import register
    register(app)

    @app.before_request
    def _load_user():
        load_current_user()

    @app.after_request
    def _security_headers(resp):
        resp.headers["Content-Security-Policy"] = CSP
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Permissions-Policy"] = "camera=(self), geolocation=()"
        if app.config["SESSION_COOKIE_SECURE"]:
            resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        if g.get("user") is not None:
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.errorhandler(403)
    def _forbidden(_e):
        return render_template("error.html", code=403, message="You do not have permission to do that."), 403

    @app.errorhandler(404)
    def _not_found(_e):
        return render_template("error.html", code=404, message="Not found."), 404

    @app.errorhandler(413)
    def _too_large(_e):
        return render_template("error.html", code=413, message="File too large (max 10 MB)."), 413

    @app.errorhandler(CSRFError)
    def _csrf(_e):
        return render_template("error.html", code=400, message="Form expired or invalid. Reload and try again."), 400

    @app.errorhandler(500)
    def _server_error(_e):
        db.session.rollback()
        return render_template("error.html", code=500, message="Unexpected error. It has been logged."), 500

    @app.get("/healthz")
    def healthz():
        db.session.execute(db.text("SELECT 1"))
        return {"status": "ok"}

    register_cli(app)
    return app


def register_cli(app):
    from .models import User
    from .security import audit, ensure_roles, hash_password, password_problems, role_by_name, verify_audit_chain

    @app.cli.command("init-db")
    def init_db():
        """Create tables, audit guards and roles (idempotent)."""
        db.create_all()
        install_db_guards()
        ensure_roles()
        click.echo("Database initialised.")

    @app.cli.command("create-admin")
    @click.option("--email", required=True)
    @click.option("--name", required=True)
    @click.password_option()
    def create_admin(email, name, password):
        """Create an Administrator account (MFA enrolment happens at first login)."""
        problems = password_problems(password)
        if problems:
            raise click.ClickException("Password needs " + ", ".join(problems))
        if User.query.filter_by(email=email.lower()).first():
            raise click.ClickException("Email already exists.")
        u = User(full_name=name, email=email.lower(), password_hash=hash_password(password),
                 role_id=role_by_name("Administrator").role_id)
        db.session.add(u)
        db.session.flush()
        audit("create", "USER", u.user_id, after={"email": u.email, "role": "Administrator", "via": "cli"}, user_id=None)
        db.session.commit()
        click.echo(f"Administrator {email} created.")

    @app.cli.command("run-jobs")
    def run_jobs():
        """Schedule generation, reminders, overdue escalation, notification dispatch. Run every 15 min."""
        from .services.jobs import run_all
        click.echo(json.dumps(run_all()))

    @app.cli.command("score-assets")
    def score_assets_cmd():
        """Compute failure-risk predictions for all active assets. Run daily."""
        from .services.predictive import score_assets
        preds = score_assets()
        click.echo(f"Scored {len(preds)} assets.")

    @app.cli.command("verify-audit")
    def verify_audit():
        ok, bad = verify_audit_chain()
        if not ok:
            raise click.ClickException(f"Audit chain broken at entry {bad}.")
        click.echo("Audit chain intact.")

    @app.cli.command("backup")
    def backup_cmd():
        from .services.backup import create_backup, sqlite_path
        path = create_backup(sqlite_path(app.config["SQLALCHEMY_DATABASE_URI"]), app.config["UPLOAD_DIR"],
                             app.config["BACKUP_DIR"], app.config["BACKUP_RETENTION"])
        click.echo(f"Backup written and verified: {path}")

    @app.cli.command("verify-backup")
    @click.argument("path")
    def verify_backup_cmd(path):
        from .services.backup import verify_backup
        ok, detail = verify_backup(path)
        if not ok:
            raise click.ClickException(detail)
        click.echo(f"OK: {detail}")

    @app.cli.command("restore")
    @click.argument("path")
    @click.option("--target-db", required=True, help="Database file to write (use a staging path first).")
    @click.option("--target-uploads", required=True)
    def restore_cmd(path, target_db, target_uploads):
        """Restore a verified backup. Stop the application before restoring over production."""
        from .services.backup import restore_backup
        click.echo(restore_backup(path, target_db, target_uploads))

    @app.cli.command("seed-demo")
    @click.option("--password", default="DemoPass12345", show_default=True)
    def seed_demo_cmd(password):
        """Load a synthetic demo facility with 18 months of history. Never run against production."""
        from .seed import seed_demo
        click.echo(seed_demo(password, date.today()))

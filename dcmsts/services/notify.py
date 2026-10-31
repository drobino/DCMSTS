import logging
import smtplib
from datetime import timedelta
from email.message import EmailMessage

from flask import current_app

from ..models import Notification, Role, User, db, utcnow

log = logging.getLogger("dcmsts.notify")
MAX_ATTEMPTS = 5


def notify(user, message_type, subject, body, work_order=None):
    """Queue a notification; delivery happens in dispatch_pending (run by the job runner)."""
    if user is None or user.status != "active":
        return None
    n = Notification(user_id=user.user_id, message_type=message_type, subject=subject[:200], body=body,
                     work_order_id=work_order.work_order_id if work_order else None, channel="email")
    db.session.add(n)
    return n


def notify_role(role_name, message_type, subject, body, work_order=None):
    users = User.query.join(Role).filter(Role.role_name == role_name, User.status == "active").all()
    return [notify(u, message_type, subject, body, work_order) for u in users]


def _send_email(to_addr, subject, body):
    cfg = current_app.config
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = cfg["SMTP_FROM"], to_addr, subject
    msg.set_content(body)
    with smtplib.SMTP(cfg["SMTP_HOST"], cfg["SMTP_PORT"], timeout=10) as smtp:
        smtp.starttls()
        if cfg["SMTP_USER"]:
            smtp.login(cfg["SMTP_USER"], cfg["SMTP_PASSWORD"])
        smtp.send_message(msg)


def dispatch_pending(now=None, sender=None):
    """Deliver queued notifications with exponential backoff. Returns (sent, failed)."""
    from ..security import audit

    now = now or utcnow()
    if sender is None:
        if current_app.config["SMTP_HOST"]:
            sender = _send_email
        else:
            sender = lambda to, subject, body: log.info("notification to=%s subject=%s", to, subject)
    sent = failed = 0
    due = Notification.query.filter(Notification.status == "queued", Notification.next_attempt_at <= now).all()
    for n in due:
        user = db.session.get(User, n.user_id)
        try:
            sender(user.email, n.subject, n.body)
            n.status, n.sent_at = "sent", now
            sent += 1
        except Exception as exc:  # delivery errors are recorded, never swallowed silently
            n.attempts += 1
            n.last_error = str(exc)[:300]
            if n.attempts >= MAX_ATTEMPTS:
                n.status = "failed"
                failed += 1
                audit("delivery_failed", "NOTIFICATION", n.notif_id, after={"error": n.last_error}, user_id=None)
            else:
                n.next_attempt_at = now + timedelta(minutes=2 ** n.attempts)
    db.session.commit()
    return sent, failed

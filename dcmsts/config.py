import os
from datetime import timedelta


class Config:
    SECRET_KEY = os.environ.get("DCMSTS_SECRET_KEY")
    SQLALCHEMY_DATABASE_URI = os.environ.get("DCMSTS_DATABASE_URL", "sqlite:///dcmsts.db")
    UPLOAD_DIR = os.environ.get("DCMSTS_UPLOAD_DIR")  # defaults to <instance>/uploads
    BACKUP_DIR = os.environ.get("DCMSTS_BACKUP_DIR")  # defaults to <instance>/backups
    BACKUP_RETENTION = int(os.environ.get("DCMSTS_BACKUP_RETENTION", "14"))
    MAX_CONTENT_LENGTH = 10 * 1024 * 1024
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = os.environ.get("DCMSTS_COOKIE_SECURE", "1") == "1"
    PERMANENT_SESSION_LIFETIME = timedelta(hours=12)
    IDLE_TIMEOUT_MINUTES = int(os.environ.get("DCMSTS_IDLE_TIMEOUT_MINUTES", "30"))
    WTF_CSRF_TIME_LIMIT = None  # token bound to the session; lets queued offline forms resubmit
    SMTP_HOST = os.environ.get("DCMSTS_SMTP_HOST")
    SMTP_PORT = int(os.environ.get("DCMSTS_SMTP_PORT", "587"))
    SMTP_USER = os.environ.get("DCMSTS_SMTP_USER")
    SMTP_PASSWORD = os.environ.get("DCMSTS_SMTP_PASSWORD")
    SMTP_FROM = os.environ.get("DCMSTS_SMTP_FROM", "dcmsts@localhost")
    LOGIN_MAX_FAILURES = 5
    LOCKOUT_MINUTES = 15

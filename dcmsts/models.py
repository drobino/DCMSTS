import sqlite3
import uuid
from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import CheckConstraint, Index, event, text
from sqlalchemy.engine import Engine

db = SQLAlchemy()


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_id():
    return str(uuid.uuid4())


def _in(column, values):
    quoted = ",".join(f"'{v}'" for v in values)
    return CheckConstraint(f"{column} IN ({quoted})", name=f"ck_{column}")


ROLES = ("Administrator", "Manager", "Technician", "Vendor")
USER_STATUSES = ("active", "suspended", "deactivated")
LIFECYCLE_STATUSES = ("active", "under_repair", "retired")
WO_TYPES = ("preventive", "corrective")
PRIORITIES = ("low", "medium", "high", "critical")
WO_STATUSES = ("unassigned", "assigned", "in_progress", "blocked", "completed")
DECISIONS = ("pending", "approved", "adjusted", "dismissed", "superseded")
READING_SOURCES = ("manual", "csv_import", "api")
METRICS = ("temperature", "load_pct", "runtime_hours", "vibration")


class Role(db.Model):
    role_id = db.Column(db.String(36), primary_key=True, default=new_id)
    role_name = db.Column(db.String(50), nullable=False, unique=True)
    permissions = db.Column(db.JSON, nullable=False, default=list)


class Vendor(db.Model):
    vendor_id = db.Column(db.String(36), primary_key=True, default=new_id)
    company_name = db.Column(db.String(150), nullable=False)
    contact_email = db.Column(db.String(200), nullable=False)
    service_type = db.Column(db.String(100))
    skills = db.Column(db.String(300), default="")
    contract_start = db.Column(db.Date)
    contract_end = db.Column(db.Date)

    def contract_valid(self, on_date):
        if not self.contract_end:
            return False
        return (self.contract_start is None or self.contract_start <= on_date) and on_date <= self.contract_end


class User(db.Model):
    __tablename__ = "user_account"
    __table_args__ = (_in("status", USER_STATUSES),)
    user_id = db.Column(db.String(36), primary_key=True, default=new_id)
    full_name = db.Column(db.String(150), nullable=False)
    email = db.Column(db.String(200), nullable=False, unique=True)
    phone = db.Column(db.String(20))
    password_hash = db.Column(db.String(255), nullable=False)
    role_id = db.Column(db.String(36), db.ForeignKey("role.role_id", ondelete="RESTRICT"), nullable=False)
    mfa_enabled = db.Column(db.Boolean, nullable=False, default=False)
    mfa_secret = db.Column(db.String(64))
    status = db.Column(db.String(20), nullable=False, default="active")
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    failed_logins = db.Column(db.Integer, nullable=False, default=0)
    locked_until = db.Column(db.DateTime)
    session_version = db.Column(db.Integer, nullable=False, default=1)
    vendor_id = db.Column(db.String(36), db.ForeignKey("vendor.vendor_id"))
    skills = db.Column(db.String(300), default="")
    # [{"name": "...", "expires": "YYYY-MM-DD" | null}]
    certifications = db.Column(db.JSON, nullable=False, default=list)

    role = db.relationship("Role")
    vendor = db.relationship("Vendor")

    @property
    def role_name(self):
        return self.role.role_name

    def can(self, permission):
        return permission in (self.role.permissions or [])


class Asset(db.Model):
    __table_args__ = (_in("lifecycle_status", LIFECYCLE_STATUSES),)
    asset_id = db.Column(db.String(36), primary_key=True, default=new_id)
    asset_tag = db.Column(db.String(50), nullable=False, unique=True)
    name = db.Column(db.String(150), nullable=False)
    category = db.Column(db.String(80), nullable=False)
    location = db.Column(db.String(150))
    manufacturer = db.Column(db.String(100))
    install_date = db.Column(db.Date)
    warranty_expiry = db.Column(db.Date)
    lifecycle_status = db.Column(db.String(20), nullable=False, default="active")


class SlaPolicy(db.Model):
    policy_id = db.Column(db.String(36), primary_key=True, default=new_id)
    priority = db.Column(db.String(10), nullable=False)
    category = db.Column(db.String(80))  # null = facility-wide default for the priority
    response_hours = db.Column(db.Float, nullable=False)
    resolution_hours = db.Column(db.Float, nullable=False)
    escalation_hours = db.Column(db.Float, nullable=False, default=4)
    escalation_chain = db.Column(db.JSON, nullable=False, default=list)  # ordered user_ids
    final_vendor_id = db.Column(db.String(36), db.ForeignKey("vendor.vendor_id"))


class MaintSchedule(db.Model):
    schedule_id = db.Column(db.String(36), primary_key=True, default=new_id)
    asset_id = db.Column(db.String(36), db.ForeignKey("asset.asset_id", ondelete="RESTRICT"), nullable=False)
    title = db.Column(db.String(150), nullable=False)
    recurrence_rule = db.Column(db.String(200), nullable=False)
    checklist_template = db.Column(db.JSON, nullable=False)
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date)
    next_due_date = db.Column(db.Date, nullable=False)
    priority = db.Column(db.String(10), nullable=False, default="medium")
    required_skill = db.Column(db.String(80))
    estimated_minutes = db.Column(db.Integer)
    compliance_ref = db.Column(db.String(150))
    active = db.Column(db.Boolean, nullable=False, default=True)

    asset = db.relationship("Asset")


class WorkOrder(db.Model):
    __table_args__ = (
        _in("type", WO_TYPES),
        _in("priority", PRIORITIES),
        _in("status", WO_STATUSES),
        Index("ix_wo_asset_status_due", "asset_id", "status", "due_date"),
        Index("ix_wo_assignee_status", "assigned_to", "status"),
    )
    work_order_id = db.Column(db.String(36), primary_key=True, default=new_id)
    schedule_id = db.Column(db.String(36), db.ForeignKey("maint_schedule.schedule_id", ondelete="SET NULL"))
    asset_id = db.Column(db.String(36), db.ForeignKey("asset.asset_id", ondelete="RESTRICT"), nullable=False)
    assigned_to = db.Column(db.String(36), db.ForeignKey("user_account.user_id", ondelete="SET NULL"))
    vendor_id = db.Column(db.String(36), db.ForeignKey("vendor.vendor_id"))
    prediction_id = db.Column(db.String(36), db.ForeignKey("prediction.prediction_id"))
    type = db.Column(db.String(12), nullable=False)
    priority = db.Column(db.String(10), nullable=False, default="medium")
    status = db.Column(db.String(12), nullable=False, default="unassigned")
    is_overdue = db.Column(db.Boolean, nullable=False, default=False)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    checklist = db.Column(db.JSON, nullable=False, default=list)  # [{"item": str, "done": bool}]
    required_skill = db.Column(db.String(80))
    due_date = db.Column(db.DateTime, nullable=False)
    sla_resolution_hours = db.Column(db.Float)  # snapshot at creation (UC-14 business rule)
    sla_escalation_hours = db.Column(db.Float)
    sla_chain = db.Column(db.JSON, nullable=False, default=list)
    sla_final_vendor_id = db.Column(db.String(36))
    escalation_level = db.Column(db.Integer, nullable=False, default=0)
    last_escalated_at = db.Column(db.DateTime)
    reminder_sent = db.Column(db.Boolean, nullable=False, default=False)
    reschedule_count = db.Column(db.Integer, nullable=False, default=0)
    blocked_reason = db.Column(db.Text)
    time_spent_minutes = db.Column(db.Integer, nullable=False, default=0)
    root_cause = db.Column(db.Text)
    downtime_start = db.Column(db.DateTime)
    downtime_end = db.Column(db.DateTime)
    downtime_cause = db.Column(db.String(150))
    downtime_planned = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    created_by = db.Column(db.String(36), db.ForeignKey("user_account.user_id"))
    completed_at = db.Column(db.DateTime)

    asset = db.relationship("Asset")
    assignee = db.relationship("User", foreign_keys=[assigned_to])
    vendor = db.relationship("Vendor")
    schedule = db.relationship("MaintSchedule")
    logs = db.relationship("TaskLog", order_by="TaskLog.timestamp", cascade="all, delete-orphan")
    parts = db.relationship("PartsUsed", cascade="all, delete-orphan")
    evidence = db.relationship("Evidence", cascade="all, delete-orphan")


class TaskLog(db.Model):
    log_id = db.Column(db.String(36), primary_key=True, default=new_id)
    work_order_id = db.Column(db.String(36), db.ForeignKey("work_order.work_order_id", ondelete="CASCADE"), nullable=False)
    user_id = db.Column(db.String(36), db.ForeignKey("user_account.user_id", ondelete="SET NULL"))
    action = db.Column(db.String(100), nullable=False)
    notes = db.Column(db.Text)
    timestamp = db.Column(db.DateTime, nullable=False, default=utcnow)

    user = db.relationship("User")


class PartsUsed(db.Model):
    part_use_id = db.Column(db.String(36), primary_key=True, default=new_id)
    work_order_id = db.Column(db.String(36), db.ForeignKey("work_order.work_order_id", ondelete="CASCADE"), nullable=False)
    part_name = db.Column(db.String(150), nullable=False)
    quantity = db.Column(db.Numeric(10, 2), nullable=False)
    cost = db.Column(db.Numeric(10, 2))
    logged_at = db.Column(db.DateTime, nullable=False, default=utcnow)


class Evidence(db.Model):
    evidence_id = db.Column(db.String(36), primary_key=True, default=new_id)
    work_order_id = db.Column(db.String(36), db.ForeignKey("work_order.work_order_id", ondelete="CASCADE"), nullable=False)
    file_url = db.Column(db.String(500), nullable=False)  # storage key under UPLOAD_DIR
    original_name = db.Column(db.String(200))
    sha256 = db.Column(db.String(64), nullable=False)
    type = db.Column(db.String(50), nullable=False)
    uploaded_by = db.Column(db.String(36), db.ForeignKey("user_account.user_id"))
    uploaded_at = db.Column(db.DateTime, nullable=False, default=utcnow)


class Notification(db.Model):
    notif_id = db.Column(db.String(36), primary_key=True, default=new_id)
    work_order_id = db.Column(db.String(36), db.ForeignKey("work_order.work_order_id", ondelete="CASCADE"))
    user_id = db.Column(db.String(36), db.ForeignKey("user_account.user_id"), nullable=False)
    channel = db.Column(db.String(10), nullable=False, default="email")
    message_type = db.Column(db.String(50), nullable=False)
    subject = db.Column(db.String(200), nullable=False)
    body = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(10), nullable=False, default="queued")  # queued/sent/failed
    attempts = db.Column(db.Integer, nullable=False, default=0)
    next_attempt_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    last_error = db.Column(db.String(300))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    sent_at = db.Column(db.DateTime)


class AuditLog(db.Model):
    __table_args__ = (
        Index("ix_audit_user_ts", "user_id", "timestamp"),
        Index("ix_audit_entity_ts", "entity_type", "timestamp"),
    )
    audit_id = db.Column(db.Integer, primary_key=True, autoincrement=True)  # sequence defines chain order
    user_id = db.Column(db.String(36))  # not an FK: entries must outlive any account
    entity_type = db.Column(db.String(50), nullable=False)
    entity_id = db.Column(db.String(36))
    action = db.Column(db.String(50), nullable=False)
    before_value = db.Column(db.JSON)
    after_value = db.Column(db.JSON)
    ip_address = db.Column(db.String(45))
    user_agent = db.Column(db.String(200))
    timestamp = db.Column(db.DateTime, nullable=False, default=utcnow)
    prev_hash = db.Column(db.String(64), nullable=False)
    hash = db.Column(db.String(64), nullable=False)


class IngestBatch(db.Model):
    batch_id = db.Column(db.String(36), primary_key=True, default=new_id)
    source = db.Column(db.String(20), nullable=False)
    filename = db.Column(db.String(200))
    loaded = db.Column(db.Integer, nullable=False, default=0)
    rejected = db.Column(db.Integer, nullable=False, default=0)
    errors = db.Column(db.JSON, nullable=False, default=list)
    status = db.Column(db.String(20), nullable=False)  # loaded / halted / rolled_back
    created_by = db.Column(db.String(36), db.ForeignKey("user_account.user_id"))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


class SensorReading(db.Model):
    __table_args__ = (
        _in("source", READING_SOURCES),
        Index("ix_reading_asset_time", "asset_id", "recorded_at"),
    )
    reading_id = db.Column(db.String(36), primary_key=True, default=new_id)
    asset_id = db.Column(db.String(36), db.ForeignKey("asset.asset_id", ondelete="CASCADE"), nullable=False)
    metric_name = db.Column(db.String(50), nullable=False)
    metric_value = db.Column(db.Numeric(12, 4), nullable=False)
    source = db.Column(db.String(12), nullable=False)
    recorded_at = db.Column(db.DateTime, nullable=False)
    batch_id = db.Column(db.String(36), db.ForeignKey("ingest_batch.batch_id"))


class ModelVersion(db.Model):
    version = db.Column(db.String(30), primary_key=True)
    status = db.Column(db.String(12), nullable=False)  # candidate / production / retired / discarded
    params = db.Column(db.JSON, nullable=False)  # features, means, scales, coefficients, intercept
    metrics = db.Column(db.JSON, nullable=False, default=dict)
    trained_by = db.Column(db.String(36))
    trained_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    promoted_at = db.Column(db.DateTime)


class Prediction(db.Model):
    __table_args__ = (
        _in("reviewer_decision", DECISIONS),
        CheckConstraint("risk_score >= 0 AND risk_score <= 1", name="ck_risk_range"),
        Index("ix_prediction_asset_time", "asset_id", "created_at"),
        Index("ix_prediction_pending", "reviewer_decision"),
    )
    prediction_id = db.Column(db.String(36), primary_key=True, default=new_id)
    asset_id = db.Column(db.String(36), db.ForeignKey("asset.asset_id", ondelete="RESTRICT"), nullable=False)
    model_version = db.Column(db.String(30), nullable=False)
    risk_score = db.Column(db.Numeric(5, 4), nullable=False)
    contributing_factors = db.Column(db.JSON, nullable=False)
    low_confidence = db.Column(db.Boolean, nullable=False, default=False)
    recommended_window_start = db.Column(db.Date)
    recommended_window_end = db.Column(db.Date)
    reviewer_decision = db.Column(db.String(12), nullable=False, default="pending")
    reviewer_id = db.Column(db.String(36), db.ForeignKey("user_account.user_id"))
    review_note = db.Column(db.Text)
    adjusted_window_start = db.Column(db.Date)
    adjusted_window_end = db.Column(db.Date)
    reviewed_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    asset = db.relationship("Asset")


class Setting(db.Model):
    key = db.Column(db.String(60), primary_key=True)
    value = db.Column(db.JSON, nullable=False)


DEFAULT_SETTINGS = {
    "risk_threshold": 0.6,
    "review_threshold": 0.3,
    "reminder_hours": 24,
    "max_reschedules": 2,
    "evidence_required_categories": ["UPS", "Generator", "Fire Suppression"],
    "telemetry_max_invalid_ratio": 0.2,
    "schedule_horizon_days": 90,
}


def get_setting(key):
    row = db.session.get(Setting, key)
    return row.value if row else DEFAULT_SETTINGS[key]


AUDIT_SQLITE_GUARDS = [
    "CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log "
    "BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log "
    "BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END",
]


def install_db_guards():
    if db.engine.dialect.name == "sqlite":
        for stmt in AUDIT_SQLITE_GUARDS:
            db.session.execute(text(stmt))
        db.session.commit()


@event.listens_for(Engine, "connect")
def _sqlite_foreign_keys(dbapi_conn, _record):
    if isinstance(dbapi_conn, sqlite3.Connection):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

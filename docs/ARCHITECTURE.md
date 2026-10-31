# Architecture

## Shape

A modular monolith: one Python/Flask process serving server-rendered, mobile-responsive pages, with a
relational database and a cron-driven CLI for periodic work.

```
Browser (manager / technician / vendor)
   │  HTTPS (reverse proxy, TLS 1.2+)
   ▼
Flask app (gunicorn workers)
   views/      HTTP + validation + authorization (thin)
   services/   business rules: scheduling, workorders, jobs, notify, predictive, ingest, reports, backup
   security.py RBAC, auth, MFA, sessions, audit chain
   models.py   SQLAlchemy ORM
   │
   ├── SQLite (default) / PostgreSQL ── all tables incl. sensor_reading, prediction, audit_log
   └── uploads/ (evidence files)        backups/ (verified zips)

cron ─► flask run-jobs      (every 15 min: generate PM instances, reminders, overdue + escalation, email dispatch)
     ─► flask score-assets  (daily: risk scoring + predictive alerts)
     ─► flask backup        (nightly, verified, retention)
```

## Data flow highlights

- **Schedules → work orders:** `MaintSchedule` holds an RFC 5545 RRULE subset (`FREQ`, `INTERVAL`). Instances are
  generated idempotently up to the configured horizon and stamped with the SLA policy active at creation.
- **Assignment:** candidates are filtered by skill (expired certifications revoke the matching skill), ranked by
  open workload weighted by priority, and annotated with same-day conflicts.
- **Escalation:** level 0 notifies managers when a task becomes overdue. Each later level notifies the next user in the
  snapshotted chain after `escalation_hours`. An optional final vendor tier drafts a vendor work order for a
  manager to assign.
- **Predictive:** features are computed per asset at time *t* using only data at or before *t*: age, days since last
  completed PM, corrective counts over 90/365 days, overdue PMs, 30-day average temperature and load, runtime,
  warranty status. The model is a standardized logistic regression whose parameters are stored as JSON on
  `model_version`, so scoring loads no pickled code, and each score splits into per-feature contributions that
  become the plain-language factors. `rules-v1` uses the same form with hand-set weights as the interim model.
- **Audit:** every entry stores `prev_hash` and `hash = sha256(canonical entry + prev_hash)`. SQLite triggers reject
  UPDATE/DELETE on `audit_log`, and `flask verify-audit` and the audit screen re-verify the chain.

## Key decisions

| Decision | Alternatives | Reason | Consequence |
|---|---|---|---|
| Single Python/Flask monolith | Node/NestJS API + separate FastAPI scoring service (spec §11 suggestion) | One runtime, and scikit-learn lives in-process; fits a one-trimester team and a limited hosting budget | Scoring scales with the web app; can be split out later behind `services/predictive.py` |
| Server-rendered responsive UI + offline form queue | React web app + React Native/Flutter mobile app | Far less code and no build chain; works on any phone browser | No native camera/offline database; photo upload needs connectivity |
| SQLite default, PostgreSQL via URL | PostgreSQL + TimescaleDB + Redis from day one | Zero-ops pilot; the ORM keeps the schema portable; the narrow `sensor_reading` table can move to a time-series store later | Single-writer database; use PostgreSQL for multi-host or high write volume |
| Cron + CLI jobs | Redis/Celery queues | Workload is periodic and small | Jobs run at most every cron interval (15 min) |
| JSON-serialized linear model | Pickled sklearn models / MLflow | Safe to load, inherently explainable, trivially versioned in the DB | Limited to linear models; adequate for Phase 1 data volumes |
| Hash-chained audit log + DB triggers | External WORM log store | Tamper-evidence without new infrastructure | A DB superuser can still drop triggers, but verification exposes the change; ship logs off-host for stronger guarantees |

## Security boundaries

- Every view is wrapped in `require(permission)`. Object-level checks (`can_access_work_order`, executor checks)
  stop technicians and vendors reaching other users' work. Denials are audited and return 403.
- Sessions are signed cookies (HttpOnly, SameSite=Lax, Secure), carrying a per-user `session_version` that is
  incremented on logout, password change, role or status change, and MFA reset. They have a 30-minute idle timeout.
- CSRF protection on all POSTs. The strict CSP has no inline scripts or styles.
- Evidence uploads: extension allow-list, magic-byte check, random storage names, SHA-256 recorded, served only
  through an authorized route.

## Known limitations

- Data at rest is not encrypted by the application. Use encrypted volumes (LUKS/cloud disk encryption) or managed
  PostgreSQL with encryption, and an encrypted backup target, to meet the AES-256 requirement. TOTP secrets are stored
  in the database, so protect it accordingly.
- The per-IP login rate limit is in-memory per worker process. Account lockout is the persistent control; add
  proxy-level rate limiting (nginx `limit_req`) in production.
- Concurrent audit writes on PostgreSQL could fork the chain under heavy parallel load. Verification would flag it;
  serialize with an advisory lock if this matters.
- Schema changes currently use `create_all` (new tables only). Adopt Alembic before the first schema change in
  production.

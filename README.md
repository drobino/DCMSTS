# DCMSTS — Intelligent Data Center Maintenance Scheduling and Tracking System

A web application for managing data center maintenance: asset registry, recurring preventive-maintenance
schedules, corrective/incident work orders, technician and vendor assignment, SLA reminders and escalation,
downtime analysis (MTTR/MTBF), PDF/Excel reports, and predictive failure-risk scoring with a manager review
queue. Access is role-based with MFA, every action goes to a tamper-evident audit log, and backups are verified.

More detail:
- [`docs/REQUIREMENTS.md`](docs/REQUIREMENTS.md): scope, acceptance criteria, test traceability, known gaps
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md): architecture, data flow, design decisions

---

## Quick install (Ubuntu / Debian server)

One command sets up everything: system packages, a service user, the Python environment, configuration,
database, first administrator, gunicorn (systemd), nginx with HTTPS, and the cron jobs.

```bash
cd /path/to/Robert                 # the project folder
sudo ./install.sh                  # clean install → https://<server-ip>/
```

It asks for the administrator's email and password. Press Enter at the password prompt to generate one; a
generated password is saved to `/root/<name>-admin-credentials.txt`, readable by root only. Sign in, enrol MFA,
and change it. Useful options (`./install.sh --help` lists all of them):

| Option | Purpose |
|---|---|
| `--name NAME` | Instance name for the service, user, folders and nginx site (default `dcmsts`). Run several instances side by side with different names and ports |
| `--http-port` / `--https-port` / `--app-port` | Ports (defaults 80 / 443 / 8000). The installer refuses ports already in use |
| `--server-name HOST` | Hostname or IP for the URL and certificate (default: the machine's first IP) |
| `--cert FILE --key FILE` | Use a real certificate instead of the generated self-signed one |
| `--admin-email`, `--admin-name`, `ADMIN_PASSWORD=…` | Unattended administrator creation |
| `--demo` | Also load the demo facility (testing only) |
| `-y` | No questions; defaults plus a generated admin password |

**Clean install vs. demo data:** without `--demo` you get an empty system containing only the administrator
account. Demo data is never loaded unless you ask for it. After a clean install, sign in as the administrator and set up:

1. **Users** under **Admin → Users**: managers, technicians and vendor accounts
2. **SLA policies** under **Admin → SLA & escalation**
3. **Assets** under **Assets → Register asset**, or **Import CSV** (format in `docs/samples/assets.csv`)
4. **Recurring preventive maintenance** under **Schedules**
5. **Vendors** under **People → Vendors**, if you use contractors

Risk scoring works immediately with the built-in rule-based model. Once maintenance history has built up, train a
proper model under **Risk → Models** (see section 8).

**After installing, manage an instance with:**

```bash
systemctl status <name>          # or: restart / stop
journalctl -u <name> -f          # application logs
sudo nano /opt/<name>/.env       # settings such as SMTP email; then: systemctl restart <name>
```

Example of a second, empty instance next to an existing one:
`sudo ./install.sh --name dcmsts-clean --http-port 8080 --https-port 8443 --app-port 8001`

**Upgrading:** update the code and run `sudo ./install.sh --name <name>` again. The installer reuses the instance's
ports, configuration, database, uploads and backups, and only refreshes code, dependencies and service files.

If the CPU lacks x86-64-v2 support (some VMs, such as the Proxmox `kvm64` CPU type), the installer automatically
installs a compatible numpy build. Setting the VM CPU type to `host` lets you use the pinned version instead.

The manual steps below describe the same setup in detail.

---

## 1. Prerequisites

### System packages

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.12 (tested on 3.12.3) | Comes with Ubuntu 24.04 and derivatives |
| python3-venv | matching Python | Creates the isolated environment |
| python3-pip | any recent | Installs Python packages |
| Git | optional | Source control |
| An authenticator app | — | Google Authenticator, Microsoft Authenticator, 1Password, etc. Admin and Manager accounts must enrol in TOTP MFA |

You don't need a separate database server, Redis, Node.js or Docker. The default database is SQLite, which is
built into Python.

**Ubuntu / Debian / Zorin / Mint:**
```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip
python3 --version        # should print 3.12.x
```

**Fedora / RHEL:**
```bash
sudo dnf install -y python3.12 python3-pip
```

**macOS (Homebrew):**
```bash
brew install python@3.12
```

**Windows:** install Python 3.12 from python.org (tick "Add python.exe to PATH"). For production use, run under
WSL2 (Ubuntu). gunicorn and cron are Linux tools.

### Python packages

These are pinned in `requirements.txt` and installed in step 2:

| Package | Purpose |
|---|---|
| Flask, Werkzeug, Jinja2, click, itsdangerous, blinker, MarkupSafe | Web framework, templating, CLI, signed sessions |
| Flask-SQLAlchemy, SQLAlchemy | Database ORM |
| Flask-WTF, WTForms | CSRF protection |
| PyOTP | TOTP multi-factor authentication |
| scikit-learn, numpy, scipy, joblib, threadpoolctl | Predictive maintenance model training |
| openpyxl, et_xmlfile | Excel report export |
| fpdf2, pillow, fonttools, defusedxml | PDF report export |
| gunicorn | Production WSGI server |
| pytest, pluggy, iniconfig, Pygments | Test suite |

**Optional:** to use PostgreSQL instead of SQLite, also install a driver: `pip install "psycopg[binary]"`.

---

## 2. Install

```bash
cd /path/to/Robert                      # the project folder containing this README

python3 -m venv .venv                   # create an isolated environment
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

On Windows (PowerShell), use `.venv\Scripts\pip` and `.venv\Scripts\flask` in place of `.venv/bin/...`.

---

## 3. Configure

All configuration comes from environment variables. Copy the example file and edit it:

```bash
cp .env.example .env
python3 -c "import secrets; print(secrets.token_hex(32))"   # paste the output as DCMSTS_SECRET_KEY in .env
```

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `DCMSTS_SECRET_KEY` | **yes** | — | Signs sessions. The app will not start without it. Keep it secret |
| `DCMSTS_DATABASE_URL` | no | `sqlite:///instance/dcmsts.db` | e.g. `sqlite:////var/lib/dcmsts/dcmsts.db` or `postgresql+psycopg://user:pass@host/dcmsts` |
| `DCMSTS_UPLOAD_DIR` | no | `instance/uploads` | Evidence photos and PDFs |
| `DCMSTS_BACKUP_DIR` | no | `instance/backups` | Backup zips |
| `DCMSTS_BACKUP_RETENTION` | no | `14` | Number of backups to keep |
| `DCMSTS_COOKIE_SECURE` | no | `1` | **Set to `0` for local http development**; keep `1` behind HTTPS |
| `DCMSTS_IDLE_TIMEOUT_MINUTES` | no | `30` | Session inactivity timeout |
| `DCMSTS_SMTP_HOST` / `_PORT` / `_USER` / `_PASSWORD` / `_FROM` | no | unset | Email delivery. If unset, notifications are logged and shown in each user's in-app **Inbox** |

Load the variables into your shell. Do this in every new terminal before running `flask` commands:

```bash
set -a; . ./.env; set +a
export FLASK_APP=dcmsts
```

---

## 4. Initialise the database

```bash
.venv/bin/flask init-db          # creates tables, audit-log guards and the four roles (safe to re-run)
```

Then choose one of the following.

### Option A: try it with demo data (development only)

```bash
export DCMSTS_COOKIE_SECURE=0                # local http
.venv/bin/flask seed-demo                    # 20 assets, 18 months of synthetic history, demo users
.venv/bin/flask score-assets                 # compute initial risk scores
```

Demo accounts all use the password **`DemoPass12345`**:

| Email | Role |
|---|---|
| `admin@dcmsts.local` | Administrator (MFA enrolment at first sign-in) |
| `manager@dcmsts.local` | Manager (MFA enrolment at first sign-in) |
| `esi@dcmsts.local`, `yaw@dcmsts.local`, `akosua@dcmsts.local`, `kwame@dcmsts.local` | Technicians |
| `vendor@coolair.example` | Vendor |

Never run `seed-demo` against a production database. It refuses to run if any assets already exist.

### Option B: start clean (real use)

```bash
.venv/bin/flask create-admin --email you@example.org --name "Your Name"
# prompts for a password: at least 12 characters, letters and digits
```

Sign in as that administrator, enrol MFA, then create users under **Admin → Users**. Add SLA policies under
**Admin → SLA & escalation**, and add assets under **Assets** (or bulk-import using `docs/samples/assets.csv`).

---

## 5. Run

### Development server

```bash
.venv/bin/flask run --debug
```

Open http://127.0.0.1:5000. Remember `DCMSTS_COOKIE_SECURE=0` when using plain http, otherwise the login cookie
is not sent and you will keep returning to the login page.

### Production server

```bash
.venv/bin/gunicorn -w 3 -b 127.0.0.1:8000 wsgi:app
```

Put gunicorn behind a reverse proxy (nginx or Caddy) that terminates HTTPS (TLS 1.2+), and keep
`DCMSTS_COOKIE_SECURE=1`. `wsgi.py` trusts one proxy hop for `X-Forwarded-For/-Proto/-Host`, so the audit log and
login rate limiting see real client IPs. The proxy must forward the host *with* its port (nginx `$http_host`, not
`$host`), otherwise logins on a non-standard port fail the CSRF check. `install.sh` sets all of this up for you. Health check endpoint: `GET /healthz` returns `{"status": "ok"}`.

### Background jobs (required for reminders, escalation, scoring and backups)

The app has no built-in scheduler; cron runs these commands. Run `crontab -e` as the app user and add the
following, adjusting paths and making sure the same environment variables are loaded:

```cron
*/15 * * * *  cd /opt/dcmsts && set -a && . ./.env && .venv/bin/flask --app dcmsts run-jobs
30 2 * * *    cd /opt/dcmsts && set -a && . ./.env && .venv/bin/flask --app dcmsts score-assets
0 3 * * *     cd /opt/dcmsts && set -a && . ./.env && .venv/bin/flask --app dcmsts backup
0 4 * * 0     cd /opt/dcmsts && set -a && . ./.env && .venv/bin/flask --app dcmsts verify-audit
```

| Command | What it does |
|---|---|
| `flask run-jobs` | Generates upcoming PM tasks, sends due-date reminders, marks overdue tasks, escalates, dispatches emails |
| `flask score-assets` | Scores failure risk for every active asset and alerts managers about high-risk assets |
| `flask backup` | Writes a verified backup zip (database + uploads + checksums) and prunes old ones |
| `flask verify-audit` | Checks that no audit log entry has been altered or removed |

During development you can run any of these by hand at any time.

---

## 6. Run the tests

```bash
.venv/bin/pytest -q
```

Expected output: `64 passed`. The tests use temporary databases and do not touch your data.

---

## 7. Backup and restore

```bash
.venv/bin/flask backup                                   # create and verify a backup
.venv/bin/flask verify-backup instance/backups/dcmsts-<timestamp>.zip
.venv/bin/flask restore instance/backups/dcmsts-<timestamp>.zip \
    --target-db /tmp/staging/dcmsts.db --target-uploads /tmp/staging/uploads
```

Practise restores into a staging location first. To restore production: stop the app, restore over the configured
database and upload paths, run `flask verify-audit`, then start the app. Copy backups off the server (e.g. with
`rsync`) to protect against losing the host. The built-in backup supports SQLite only; for PostgreSQL use
`pg_dump` plus a copy of the uploads folder.

---

## 8. Using the predictive maintenance module

1. **Telemetry:** technicians log readings on asset or work-order pages, or managers import a CSV under
   **Risk → Telemetry import** (format in `docs/samples/telemetry.csv`). Invalid rows are quarantined, and any
   batch can be rolled back.
2. **Scoring:** `flask score-assets` or **Risk → Run scoring now**. Until a trained model is promoted, the
   interim rule-based model `rules-v1` is used.
3. **Review:** managers approve, adjust or dismiss recommendations in **Risk → Review queue**. Approving creates
   a work order; nothing is created automatically.
4. **Training:** **Risk → Models → Train new candidate**, check the precision and recall figures, then promote
   manually. Promoting an older version rolls back to it.

---

## 9. Troubleshooting

| Problem | Fix |
|---|---|
| `RuntimeError: DCMSTS_SECRET_KEY must be set` | Load `.env` (`set -a; . ./.env; set +a`) or export the variable |
| Login succeeds but returns to the login page | You're on http with secure cookies; set `DCMSTS_COOKIE_SECURE=0` for local development |
| `Error: Could not locate a Flask application` | `export FLASK_APP=dcmsts`, or pass `--app dcmsts`, and run from the project folder |
| `No module named venv` / `ensurepip is not available` | `sudo apt install python3-venv` |
| MFA code rejected | Make sure your phone's clock is set automatically; codes are time-based |
| Account locked | Wait 15 minutes, or an administrator resets the password under **Admin → Users** |
| Form says "expired or invalid" | Reload the page and submit again (CSRF token refresh) |
| `install.sh` stops with an error | Read the message: it names the failing step. Fix the cause and re-run; the installer is safe to repeat |
| `install.sh` says a port is in use | Another program or DCMSTS instance has it. Choose others with `--http-port`, `--https-port`, `--app-port` |
| Browser warns "connection is not private" | Expected with the generated self-signed certificate. Continue once, or install a real certificate with `--cert` / `--key` |
| Lost the generated admin password | `sudo cat /root/<name>-admin-credentials.txt` (delete the file once you have changed the password) |
| Login form gives "Bad Request" behind your own proxy | Forward `Host`/`X-Forwarded-Host` including the port (`$http_host`) |
| Service not running | `systemctl status <name>` and `journalctl -u <name> -n 50` |
| No emails arrive | Set the `DCMSTS_SMTP_*` variables and make sure cron runs `run-jobs`; check **Admin → Notification log** |

---

## 10. Project layout

```
dcmsts/
  __init__.py        app factory, security headers, CLI commands
  config.py          environment configuration
  models.py          database models
  security.py        roles, permissions, login, lockout, MFA, audit chain
  seed.py            synthetic demo data
  services/          business logic: scheduling, workorders, jobs, notify, predictive, ingest, reports, backup
  views/             web routes
  templates/         server-rendered pages; base.html holds the sidebar layout, _icons.html the inline SVG icons
  static/            style.css (design system, light/dark themes), theme.js (theme switch, saved per browser),
                     app.js (queues form posts while offline)
tests/               pytest suite
docs/                requirements, architecture, sample CSVs
requirements.txt     pinned Python dependencies
.env.example         configuration template
install.sh           one-shot installer / upgrader for Ubuntu and Debian servers
wsgi.py              production entry point (gunicorn wsgi:app) with reverse-proxy header handling
```

### Interface

The UI uses a "Charcoal + Electric Blue" palette: light `#F4F7FB` / `#2563EB`, dark `#151B26` / `#3B82F6`. It follows
the operating system's light or dark setting, and each user can override it with the sun/moon button in the sidebar.
All colours are CSS variables at the top of `dcmsts/static/style.css`. The app's Content Security Policy forbids
inline scripts and styles, so keep new styling in `style.css` and new scripts in `static/`.

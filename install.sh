#!/usr/bin/env bash
# DCMSTS one-shot installer for Ubuntu / Debian.
#
#   sudo ./install.sh                         # clean install: /opt/dcmsts, https on 443
#   sudo ./install.sh --name dcmsts-clean --http-port 8080 --https-port 8443 --app-port 8001
#   sudo ./install.sh --demo                  # also load the synthetic demo facility (testing only)
#
# What it does: installs system packages, creates a service user, copies the code, builds a Python venv,
# writes the config (.env with a fresh secret), initialises the database, creates the first administrator,
# and sets up gunicorn (systemd), nginx with HTTPS (self-signed unless you pass --cert/--key) and the cron jobs.
#
# Re-running it with the same --name upgrades the code and dependencies in place and restarts the service.
# Configuration, database, uploads and backups are kept.
set -Eeuo pipefail

# ----------------------------------------------------------------------------------------------- defaults
NAME="dcmsts"
INSTALL_DIR=""
HTTP_PORT=80
HTTPS_PORT=443
APP_PORT=8000
SERVER_NAME=""
ADMIN_EMAIL=""
ADMIN_NAME="Administrator"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-}"
CERT_FILE=""
KEY_FILE=""
DEMO=0
WORKERS=3
ASSUME_YES=0
PORTS_GIVEN=0

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ----------------------------------------------------------------------------------------------- helpers
if [[ -t 1 ]]; then B=$'\e[1m'; G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; C=$'\e[36m'; N=$'\e[0m'; else B= G= Y= R= C= N=; fi
STEP=0
step() { STEP=$((STEP + 1)); printf '\n%s[%d] %s%s\n' "$B$C" "$STEP" "$*" "$N"; }
ok()   { printf '    %s✔%s %s\n' "$G" "$N" "$*"; }
warn() { printf '    %s!%s %s\n' "$Y" "$N" "$*"; }
die()  { printf '\n%sError:%s %s\n' "$R$B" "$N" "$*" >&2; exit 1; }
trap 'die "installation failed at line $LINENO (command: $BASH_COMMAND). Fix the problem and re-run; the installer is safe to repeat."' ERR

usage() {
  cat <<EOF
Usage: sudo ./install.sh [options]

Instance
  --name NAME            Instance name; used for the service, user, directories and nginx site (default: dcmsts)
  --dir PATH             Install directory (default: /opt/NAME)
  --demo                 Load synthetic demo data and demo users (never for production)

Network
  --server-name HOST     Hostname or IP users will type in the browser (default: this machine's first IP)
  --http-port N          Port that redirects to HTTPS (default: 80)
  --https-port N         HTTPS port (default: 443)
  --app-port N           Internal gunicorn port on 127.0.0.1 (default: 8000)
  --cert FILE --key FILE Use your own TLS certificate instead of a self-signed one
  --workers N            gunicorn worker processes (default: 3)

First administrator (skipped on upgrade if users already exist)
  --admin-email EMAIL    Default: admin@SERVER-NAME-or-localhost; prompted when run interactively
  --admin-name NAME      Default: Administrator
  ADMIN_PASSWORD=...     Environment variable; if unset you are prompted, or a strong one is generated

  -y, --yes              Do not ask questions; use defaults and generate the admin password
  -h, --help             Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name) NAME="$2"; shift 2 ;;
    --dir) INSTALL_DIR="$2"; shift 2 ;;
    --server-name) SERVER_NAME="$2"; shift 2 ;;
    --http-port) HTTP_PORT="$2"; PORTS_GIVEN=1; shift 2 ;;
    --https-port) HTTPS_PORT="$2"; PORTS_GIVEN=1; shift 2 ;;
    --app-port) APP_PORT="$2"; PORTS_GIVEN=1; shift 2 ;;
    --admin-email) ADMIN_EMAIL="$2"; shift 2 ;;
    --admin-name) ADMIN_NAME="$2"; shift 2 ;;
    --cert) CERT_FILE="$2"; shift 2 ;;
    --key) KEY_FILE="$2"; shift 2 ;;
    --workers) WORKERS="$2"; shift 2 ;;
    --demo) DEMO=1; shift ;;
    -y|--yes) ASSUME_YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage; die "unknown option: $1" ;;
  esac
done

[[ "$NAME" =~ ^[a-z][a-z0-9-]{1,30}$ ]] || die "--name must be lowercase letters, digits and dashes (e.g. dcmsts-clean)."
for p in "$HTTP_PORT" "$HTTPS_PORT" "$APP_PORT" "$WORKERS"; do [[ "$p" =~ ^[0-9]+$ ]] || die "ports and --workers must be numbers."; done
[[ -n "$CERT_FILE" && -z "$KEY_FILE" || -z "$CERT_FILE" && -n "$KEY_FILE" ]] && die "--cert and --key must be given together."

INSTALL_DIR="${INSTALL_DIR:-/opt/$NAME}"
APP_USER="$NAME"
DATA_DIR="/var/lib/$NAME"
BACKUP_DIR="/var/backups/$NAME"
SSL_DIR="/etc/ssl/$NAME"
SITE="/etc/nginx/sites-available/$NAME"
UNIT="/etc/systemd/system/$NAME.service"
CRED_FILE="/root/$NAME-admin-credentials.txt"
INTERACTIVE=0; [[ -t 0 && $ASSUME_YES -eq 0 ]] && INTERACTIVE=1

run_as_app() {  # run a command as the service user with the instance's environment loaded
  runuser -u "$APP_USER" -- bash -c "cd '$INSTALL_DIR' && set -a && . ./.env && set +a && $*"
}
quiet() { grep -v -e 'Tip:' -e ' INFO ' | sed 's/^/    /' || true; }
port_owner() { ss -Htlnp "sport = :$1" 2>/dev/null | head -1; }

# ----------------------------------------------------------------------------------------------- 1. checks
step "Checking the system"
[[ $EUID -eq 0 ]] || die "run as root: sudo ./install.sh"
[[ -f "$SRC_DIR/requirements.txt" && -d "$SRC_DIR/dcmsts" ]] || die "run install.sh from the DCMSTS project folder."
command -v apt-get >/dev/null || die "this installer supports apt-based systems (Ubuntu/Debian). See README.md for manual steps."
ok "$(. /etc/os-release && echo "$PRETTY_NAME")"

UPGRADE=0; [[ -f "$INSTALL_DIR/.env" ]] && UPGRADE=1
if [[ $UPGRADE -eq 1 ]]; then
  ok "existing instance found in $INSTALL_DIR: upgrading in place (config and data are kept)"
  # keep the ports the instance was installed with unless overridden explicitly
  if [[ $PORTS_GIVEN -eq 0 && -f "$SITE" && -f "$UNIT" ]]; then
    HTTP_PORT=$(grep -oP '^\s*listen \K\d+(?=;)' "$SITE" | head -1)
    HTTPS_PORT=$(grep -oP '^\s*listen \K\d+(?= ssl)' "$SITE" | head -1)
    APP_PORT=$(grep -oP '127\.0\.0\.1:\K\d+' "$UNIT" | head -1)
  fi
else
  ok "new instance '$NAME' → $INSTALL_DIR"
fi

# A port is free if nothing listens on it, or if this same instance already owns it (upgrade).
for pair in "http:$HTTP_PORT" "https:$HTTPS_PORT" "app:$APP_PORT"; do
  label=${pair%%:*}; port=${pair#*:}
  owner=$(port_owner "$port")
  [[ -z "$owner" ]] && continue
  if [[ $label == app ]]; then
    grep -qs "127.0.0.1:$port " "$UNIT" && continue
  elif echo "$owner" | grep -q nginx; then
    # nginx listens there: OK unless another enabled site claims the same port
    others=$(grep -lsE "^\s*listen (\[::\]:)?$port( |;)" /etc/nginx/sites-enabled/* | grep -v "/$NAME\$" || true)
    [[ -z "$others" ]] && continue
    die "$label port $port is already used by nginx site(s): $others. Choose another with --$label-port."
  fi
  die "$label port $port is in use ($(echo "$owner" | grep -oP 'users:\(\("\K[^"]+' || echo 'another process')). Choose another with --$label-port."
done
ok "ports: HTTP $HTTP_PORT → HTTPS $HTTPS_PORT, gunicorn 127.0.0.1:$APP_PORT"

if [[ -z "$SERVER_NAME" ]]; then
  SERVER_NAME=$(hostname -I 2>/dev/null | awk '{print $1}')
  SERVER_NAME=${SERVER_NAME:-localhost}
fi
ok "server name: $SERVER_NAME"

# ----------------------------------------------------------------------------------------------- 2. packages
step "Installing system packages"
PKGS=(python3 python3-venv python3-pip nginx openssl curl rsync cron)
MISSING=()
for p in "${PKGS[@]}"; do dpkg -s "$p" >/dev/null 2>&1 || MISSING+=("$p"); done
if [[ ${#MISSING[@]} -gt 0 ]]; then
  export DEBIAN_FRONTEND=noninteractive NEEDRESTART_SUSPEND=1
  apt-get update -qq
  apt-get install -y -qq "${MISSING[@]}" >/dev/null
  ok "installed: ${MISSING[*]}"
else
  ok "all present"
fi
PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || die "Python 3.11+ required (found $PYV)."
ok "Python $PYV"

# ----------------------------------------------------------------------------------------------- 3. user + code
step "Creating service user and copying the application"
if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin "$APP_USER"
  ok "created system user '$APP_USER'"
else
  ok "user '$APP_USER' exists"
fi
if [[ -d "$INSTALL_DIR" && -n "$(ls -A "$INSTALL_DIR" 2>/dev/null)" && ! -f "$INSTALL_DIR/.env" && ! -d "$INSTALL_DIR/dcmsts" ]]; then
  die "$INSTALL_DIR exists and is not a DCMSTS install; refusing to overwrite it. Choose another --dir."
fi
mkdir -p "$INSTALL_DIR" "$DATA_DIR/uploads" "$BACKUP_DIR"
if [[ "$(realpath "$SRC_DIR")" != "$(realpath "$INSTALL_DIR")" ]]; then
  rsync -a --delete \
    --exclude .git --exclude .venv --exclude instance --exclude .env --exclude __pycache__ --exclude .pytest_cache \
    "$SRC_DIR/" "$INSTALL_DIR/"
  ok "code copied to $INSTALL_DIR"
else
  ok "installing in place from $INSTALL_DIR"
fi
mkdir -p "$INSTALL_DIR/instance"
chown -R root:root "$INSTALL_DIR"
chown "$APP_USER:$APP_USER" "$INSTALL_DIR/instance"
chown -R "$APP_USER:$APP_USER" "$DATA_DIR" "$BACKUP_DIR"
chmod 750 "$DATA_DIR" "$BACKUP_DIR"

# ----------------------------------------------------------------------------------------------- 4. venv
step "Building the Python environment (this can take a few minutes the first time)"
[[ -x "$INSTALL_DIR/.venv/bin/python" ]] || python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install -q --disable-pip-version-check --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install -q --disable-pip-version-check -r "$INSTALL_DIR/requirements.txt"
# Recent numpy wheels need an x86-64-v2 CPU. Some VMs expose an older CPU model (e.g. Proxmox "kvm64");
# fall back to the newest numpy that still runs there.
if ! "$INSTALL_DIR/.venv/bin/python" -c 'import numpy, sklearn.linear_model' >/dev/null 2>&1; then
  warn "this CPU cannot run the pinned numpy build; installing a compatible numpy (<2.4)"
  warn "tip: set the VM CPU type to 'host' or 'x86-64-v2' to use the pinned version"
  "$INSTALL_DIR/.venv/bin/pip" install -q --disable-pip-version-check "numpy<2.4"
  "$INSTALL_DIR/.venv/bin/python" -c 'import numpy, sklearn.linear_model' || die "numpy/scikit-learn still fail to import."
fi
"$INSTALL_DIR/.venv/bin/pip" check >/dev/null || warn "pip check reported dependency conflicts"
ok "dependencies installed ($("$INSTALL_DIR/.venv/bin/python" -c 'import numpy, sklearn; print("numpy", numpy.__version__ + ",", "scikit-learn", sklearn.__version__)' 2>/dev/null))"

# ----------------------------------------------------------------------------------------------- 5. config
step "Writing configuration"
if [[ ! -f "$INSTALL_DIR/.env" ]]; then
  SECRET=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
  cat > "$INSTALL_DIR/.env" <<EOF
# Generated by install.sh on $(date -u +%Y-%m-%dT%H:%MZ). Restart after editing: systemctl restart $NAME
DCMSTS_SECRET_KEY=$SECRET
DCMSTS_DATABASE_URL=sqlite:///$DATA_DIR/$NAME.db
DCMSTS_UPLOAD_DIR=$DATA_DIR/uploads
DCMSTS_BACKUP_DIR=$BACKUP_DIR
DCMSTS_BACKUP_RETENTION=14
DCMSTS_COOKIE_SECURE=1
DCMSTS_IDLE_TIMEOUT_MINUTES=30
# Email: when DCMSTS_SMTP_HOST is empty, notifications appear in each user's in-app Inbox.
DCMSTS_SMTP_HOST=
DCMSTS_SMTP_PORT=587
DCMSTS_SMTP_USER=
DCMSTS_SMTP_PASSWORD=
DCMSTS_SMTP_FROM=dcmsts@$SERVER_NAME
FLASK_APP=dcmsts
EOF
  ok "created $INSTALL_DIR/.env with a new secret key"
else
  ok "kept existing $INSTALL_DIR/.env"
fi
chown root:"$APP_USER" "$INSTALL_DIR/.env"; chmod 640 "$INSTALL_DIR/.env"

# ----------------------------------------------------------------------------------------------- 6. database
step "Initialising the database"
run_as_app ".venv/bin/flask --app dcmsts init-db" 2>&1 | quiet
USER_COUNT=$(run_as_app ".venv/bin/python -c \"from dcmsts import create_app; from dcmsts.models import User; app = create_app(); app.app_context().push(); print(User.query.count())\"" 2>/dev/null | tail -1)
[[ "$USER_COUNT" =~ ^[0-9]+$ ]] || die "could not query the database. Logs: journalctl -u $NAME"
ok "database ready ($USER_COUNT existing users)"

ADMIN_CREATED=0
if [[ "$USER_COUNT" == "0" ]]; then
  step "Creating the first administrator"
  if [[ -z "$ADMIN_EMAIL" ]]; then
    DEFAULT_EMAIL="admin@${SERVER_NAME}"
    [[ "$SERVER_NAME" =~ ^[0-9.]+$ || "$SERVER_NAME" == localhost ]] && DEFAULT_EMAIL="admin@dcmsts.local"
    if [[ $INTERACTIVE -eq 1 ]]; then
      read -rp "    Administrator email [$DEFAULT_EMAIL]: " ADMIN_EMAIL
      read -rp "    Administrator full name [$ADMIN_NAME]: " n; ADMIN_NAME=${n:-$ADMIN_NAME}
    fi
    ADMIN_EMAIL=${ADMIN_EMAIL:-$DEFAULT_EMAIL}
  fi
  GENERATED=0
  if [[ -z "$ADMIN_PASSWORD" && $INTERACTIVE -eq 1 ]]; then
    while :; do
      read -rsp "    Password (12+ characters, letters and digits; Enter = generate one): " ADMIN_PASSWORD; echo
      [[ -z "$ADMIN_PASSWORD" ]] && break
      read -rsp "    Repeat password: " again; echo
      [[ "$ADMIN_PASSWORD" == "$again" ]] && break
      warn "passwords did not match, try again"
    done
  fi
  if [[ -z "$ADMIN_PASSWORD" ]]; then
    ADMIN_PASSWORD="$(python3 -c 'import secrets, string; a = string.ascii_letters + string.digits; print("".join(secrets.choice(a) for _ in range(14)) + secrets.choice(string.digits) + secrets.choice(string.ascii_letters))')"
    GENERATED=1
  fi
  # Run the CLI in-process and pass the password on stdin, so it never appears on a command line.
  OUT=$(printf '%s' "$ADMIN_PASSWORD" | runuser -u "$APP_USER" -- bash -c "cd '$INSTALL_DIR' && set -a && . ./.env && set +a && \
    .venv/bin/python -c 'import sys
from dcmsts import create_app
app = create_app()
r = app.test_cli_runner().invoke(args=[\"create-admin\", \"--email\", sys.argv[1], \"--name\", sys.argv[2], \"--password\", sys.stdin.read()])
print(r.output.strip()); sys.exit(r.exit_code)' \"\$0\" \"\$1\"" "$ADMIN_EMAIL" "$ADMIN_NAME" 2>/dev/null) || die "could not create the administrator: $OUT"
  ok "$OUT"
  ADMIN_CREATED=1
  if [[ $GENERATED -eq 1 ]]; then
    umask 077
    printf 'DCMSTS (%s) first administrator\nURL:      https://%s%s/\nEmail:    %s\nPassword: %s\n\nSign in, enrol MFA, then change this password (click your name > key icon) and delete this file.\n' \
      "$NAME" "$SERVER_NAME" "$([[ $HTTPS_PORT -eq 443 ]] || echo ":$HTTPS_PORT")" "$ADMIN_EMAIL" "$ADMIN_PASSWORD" > "$CRED_FILE"
    ok "generated password saved to $CRED_FILE (readable by root only)"
  fi
  ADMIN_PASSWORD=""
else
  ok "users already exist; skipping administrator creation"
fi

if [[ $DEMO -eq 1 ]]; then
  step "Loading demo data"
  ASSETS=$(run_as_app ".venv/bin/python -c \"from dcmsts import create_app; from dcmsts.models import Asset; app = create_app(); app.app_context().push(); print(Asset.query.count())\"" 2>/dev/null | tail -1)
  if [[ "$ASSETS" == "0" ]]; then
    run_as_app ".venv/bin/flask --app dcmsts seed-demo" 2>&1 | quiet
    run_as_app ".venv/bin/flask --app dcmsts score-assets" 2>&1 | quiet
  else
    warn "assets already exist; demo data not loaded"
  fi
fi

# ----------------------------------------------------------------------------------------------- 7. service
step "Starting the application service"
cat > "$UNIT" <<EOF
[Unit]
Description=DCMSTS ($NAME) - Data Center Maintenance Scheduling and Tracking
After=network.target

[Service]
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$INSTALL_DIR/.env
ExecStart=$INSTALL_DIR/.venv/bin/gunicorn -w $WORKERS -b 127.0.0.1:$APP_PORT --access-logfile - wsgi:app
Restart=on-failure
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable "$NAME" >/dev/null 2>&1
systemctl restart "$NAME"
ok "systemd service '$NAME' running on 127.0.0.1:$APP_PORT"

# ----------------------------------------------------------------------------------------------- 8. nginx + TLS
step "Configuring HTTPS (nginx)"
if [[ -n "$CERT_FILE" ]]; then
  [[ -r "$CERT_FILE" && -r "$KEY_FILE" ]] || die "cannot read --cert/--key."
  SSL_CERT="$CERT_FILE"; SSL_KEY="$KEY_FILE"
  ok "using supplied certificate"
else
  mkdir -p "$SSL_DIR"
  SSL_CERT="$SSL_DIR/cert.pem"; SSL_KEY="$SSL_DIR/key.pem"
  if [[ ! -f "$SSL_CERT" ]] || ! openssl x509 -in "$SSL_CERT" -noout -ext subjectAltName 2>/dev/null | grep -q "$SERVER_NAME"; then
    if [[ "$SERVER_NAME" =~ ^[0-9.]+$ ]]; then SAN="IP:$SERVER_NAME,DNS:localhost"; else SAN="DNS:$SERVER_NAME,DNS:localhost"; fi
    openssl req -x509 -nodes -newkey rsa:2048 -days 825 -subj "/CN=$SERVER_NAME" -addext "subjectAltName=$SAN" \
      -keyout "$SSL_KEY" -out "$SSL_CERT" 2>/dev/null
    chmod 600 "$SSL_KEY"
    ok "generated a self-signed certificate for $SERVER_NAME (browsers will show a one-time warning)"
  else
    ok "kept existing certificate"
  fi
fi

HTTPS_SUFFIX=$([[ $HTTPS_PORT -eq 443 ]] && echo "" || echo ":$HTTPS_PORT")
cat > "$SITE" <<EOF
# Generated by install.sh for DCMSTS instance '$NAME'.
limit_req_zone \$binary_remote_addr zone=${NAME//-/_}_login:10m rate=10r/m;

server {
    listen $HTTP_PORT;
    listen [::]:$HTTP_PORT;
    server_name _;
    return 301 https://\$host$HTTPS_SUFFIX\$request_uri;
}

server {
    listen $HTTPS_PORT ssl;
    listen [::]:$HTTPS_PORT ssl;
    server_name _;

    ssl_certificate     $SSL_CERT;
    ssl_certificate_key $SSL_KEY;
    ssl_protocols TLSv1.2 TLSv1.3;
    client_max_body_size 12m;

    # plain-http requests sent to the https port: bounce them to https
    error_page 497 =301 https://\$host$HTTPS_SUFFIX\$request_uri;

    location = /login {
        limit_req zone=${NAME//-/_}_login burst=10 nodelay;
        proxy_pass http://127.0.0.1:$APP_PORT;
        include /etc/nginx/proxy_params;
        proxy_set_header X-Forwarded-Host \$http_host;
    }
    location / {
        proxy_pass http://127.0.0.1:$APP_PORT;
        include /etc/nginx/proxy_params;
        proxy_set_header X-Forwarded-Host \$http_host;
        proxy_read_timeout 120s;
    }
}
EOF
ln -sf "$SITE" "/etc/nginx/sites-enabled/$NAME"
# the stock "Welcome to nginx" site would collide with port 80
if [[ $HTTP_PORT -eq 80 && -L /etc/nginx/sites-enabled/default ]]; then rm -f /etc/nginx/sites-enabled/default; ok "disabled nginx default site"; fi
nginx -t >/dev/null 2>&1 || { nginx -t; die "nginx configuration test failed."; }
systemctl enable nginx >/dev/null 2>&1
systemctl reload-or-restart nginx
ok "nginx serving https://$SERVER_NAME$HTTPS_SUFFIX/"
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow "$HTTP_PORT/tcp" >/dev/null; ufw allow "$HTTPS_PORT/tcp" >/dev/null; ok "opened ports $HTTP_PORT and $HTTPS_PORT in ufw"
fi

# ----------------------------------------------------------------------------------------------- 9. cron
step "Scheduling background jobs"
PREFIX="cd $INSTALL_DIR && set -a && . ./.env && set +a && .venv/bin/flask --app dcmsts"
LOG="$DATA_DIR/cron.log"
crontab -u "$APP_USER" - <<EOF
# DCMSTS ($NAME) background jobs - managed by install.sh
*/15 * * * *  $PREFIX run-jobs     >> $LOG 2>&1
30 2 * * *    $PREFIX score-assets >> $LOG 2>&1
0 3 * * *     $PREFIX backup       >> $LOG 2>&1
0 4 * * 0     $PREFIX verify-audit >> $LOG 2>&1
EOF
systemctl enable --now cron >/dev/null 2>&1 || true
ok "reminders/escalation every 15 min, risk scoring 02:30, backup 03:00, audit check Sundays 04:00"

# ----------------------------------------------------------------------------------------------- 10. verify
step "Verifying"
for i in $(seq 1 20); do
  if curl -fsk "https://127.0.0.1:$HTTPS_PORT/healthz" 2>/dev/null | grep -q ok; then HEALTHY=1; break; fi
  sleep 1
done
[[ "${HEALTHY:-0}" -eq 1 ]] || die "health check failed. Logs: journalctl -u $NAME -n 50"
ok "health check passed"
run_as_app ".venv/bin/flask --app dcmsts verify-audit" 2>&1 | quiet

URL="https://$SERVER_NAME$HTTPS_SUFFIX/"
cat <<EOF

${G}${B}DCMSTS is ready.${N}

  Open:            ${B}$URL${N}
EOF
if [[ $ADMIN_CREATED -eq 1 ]]; then
  echo "  Administrator:   $ADMIN_EMAIL"
  if [[ -f "$CRED_FILE" ]]; then echo "  Password:        sudo cat $CRED_FILE"; else echo "  Password:        the one you entered"; fi
  echo "  First sign-in:   you will be asked to enrol an authenticator app (MFA)."
fi
[[ $DEMO -eq 1 ]] && echo "  Demo users:      admin@dcmsts.local, manager@…, esi@… (password DemoPass12345)"
cat <<EOF

  Service:         systemctl status|restart $NAME      logs: journalctl -u $NAME -f
  Config:          $INSTALL_DIR/.env
  Data / backups:  $DATA_DIR   /   $BACKUP_DIR
  Upgrade:         git pull (or copy new code) and run this installer again with --name $NAME
EOF

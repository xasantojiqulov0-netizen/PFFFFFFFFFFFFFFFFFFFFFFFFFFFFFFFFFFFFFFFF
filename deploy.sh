#!/usr/bin/env bash
# Cross-server deploy script (Linux)
# Usage: sudo ./deploy.sh /path/to/app

set -euo pipefail
APP_DIR=${1:-$(pwd)}
PYTHON=${PYTHON:-python3}
VENV_DIR=${APP_DIR}/.venv
SERVICE_NAME=${SERVICE_NAME:-kino-bot}
USER=${USER:-$(whoami)}
PORT=${PORT:-8000}
WEBHOOK_PATH=${WEBHOOK_PATH:-/webhook/$BOT_TOKEN}

echo "Deploying app to ${APP_DIR} as user ${USER}"

# Ensure app dir exists
if [ ! -d "${APP_DIR}" ]; then
  echo "App directory does not exist: ${APP_DIR}" >&2
  exit 1
fi

cd "${APP_DIR}"

# Create virtualenv
if [ ! -d "${VENV_DIR}" ]; then
  echo "Creating virtualenv..."
  ${PYTHON} -m venv "${VENV_DIR}"
fi

# Activate and install
. "${VENV_DIR}/bin/activate"
pip install --upgrade pip
if [ -f requirements.txt ]; then
  pip install -r requirements.txt
fi

# Create systemd service
if [ -w /etc/systemd/system ]; then
  echo "Creating systemd service ${SERVICE_NAME}.service"
  cat > /etc/systemd/system/${SERVICE_NAME}.service <<EOF
[Unit]
Description=Kino Telegram Bot
After=network.target

[Service]
Type=simple
User=${USER}
WorkingDirectory=${APP_DIR}
Environment="PATH=${VENV_DIR}/bin"
Environment="PORT=${PORT}"
ExecStart=${VENV_DIR}/bin/python ${APP_DIR}/main.py
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable ${SERVICE_NAME}.service
  systemctl restart ${SERVICE_NAME}.service
  echo "Service started: systemctl status ${SERVICE_NAME}.service"
else
  echo "No permission to write systemd service. Please create a service manually using the example in DEPLOY.md"
fi

echo "Deployment finished."

#!/usr/bin/env bash
#
# install.sh — Interactive installer for IP Access Monitor
#
# This script:
#   1. Checks prerequisites (Python 3, pip, systemd)
#   2. Prompts for Telegram bot token & chat ID (with test message)
#   3. Prompts for monitoring configuration
#   4. Writes .env and creates ips.txt if missing
#   5. Installs Python dependencies
#   6. Creates, enables & starts a systemd service
#
set -euo pipefail

# ------------------------------------------------------------------ #
#  Colours                                                             #
# ------------------------------------------------------------------ #
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ------------------------------------------------------------------ #
#  1. Prerequisites                                                    #
# ------------------------------------------------------------------ #
info "Checking prerequisites..."

if ! command -v python3 &>/dev/null; then
    error "python3 is not installed. Please install Python 3.8+ first."
    exit 1
fi

if ! command -v pip3 &>/dev/null && ! python3 -m pip --version &>/dev/null 2>&1; then
    error "pip is not installed. Please install pip first."
    exit 1
fi

if ! command -v systemctl &>/dev/null; then
    warn "systemctl not found — systemd service setup will be skipped."
    HAS_SYSTEMD=false
else
    HAS_SYSTEMD=true
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
ok "Python $PYTHON_VERSION detected"

# ------------------------------------------------------------------ #
#  2. Prompt for configuration                                         #
# ------------------------------------------------------------------ #
echo ""
echo "=========================================="
echo "  IP Access Monitor — Setup"
echo "=========================================="
echo ""

# Telegram Bot Token
while true; do
    read -rp "Enter your Telegram Bot Token: " BOT_TOKEN
    if [[ -n "$BOT_TOKEN" ]]; then
        break
    fi
    warn "Bot token cannot be empty."
done

# Telegram Chat ID
while true; do
    read -rp "Enter your Telegram Chat ID: " CHAT_ID
    if [[ -n "$CHAT_ID" ]]; then
        break
    fi
    warn "Chat ID cannot be empty."
done

# Test Telegram connection
info "Sending test message to Telegram..."
TEST_RESPONSE=$(curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
    -H "Content-Type: application/json" \
    -d "{\"chat_id\": \"${CHAT_ID}\", \"text\": \"✅ IP Access Monitor\\nTest message — alerts are configured correctly.\", \"parse_mode\": \"HTML\"}" 2>&1)

if echo "$TEST_RESPONSE" | grep -q '"ok":true'; then
    ok "Test message sent successfully!"
else
    warn "Test message failed. Response: $TEST_RESPONSE"
    read -rp "Continue anyway? (y/N): " CONTINUE
    [[ "$CONTINUE" =~ ^[Yy]$ ]] || exit 1
fi

# Monitoring config with defaults
echo ""
read -rp "Check interval in seconds [120]: " CHECK_INTERVAL
CHECK_INTERVAL=${CHECK_INTERVAL:-120}

read -rp "Seconds to wait before polling results [5]: " RESULT_WAIT
RESULT_WAIT=${RESULT_WAIT:-5}

read -rp "Max non-priority global nodes [20]: " MAX_NODES
MAX_NODES=${MAX_NODES:-20}

read -rp "Alert threshold (0.0–1.0) [0.7]: " ALERT_THRESHOLD
ALERT_THRESHOLD=${ALERT_THRESHOLD:-0.7}

read -rp "Priority countries (comma-separated) [ir,de]: " PRIORITY_COUNTRIES
PRIORITY_COUNTRIES=${PRIORITY_COUNTRIES:-ir,de}

# ------------------------------------------------------------------ #
#  3. Write .env                                                       #
# ------------------------------------------------------------------ #
info "Writing .env..."

cat > .env <<EOF
# Telegram Bot Configuration
TELEGRAM_BOT_TOKEN=${BOT_TOKEN}
TELEGRAM_CHAT_ID=${CHAT_ID}

# Monitoring Configuration
CHECK_INTERVAL=${CHECK_INTERVAL}
RESULT_WAIT=${RESULT_WAIT}
NODE_CACHE_TTL=86400
MAX_NODES=${MAX_NODES}
ALERT_THRESHOLD=${ALERT_THRESHOLD}

# Priority countries — all nodes from these countries are always included
PRIORITY_COUNTRIES=${PRIORITY_COUNTRIES}
EOF

ok ".env created"

# ------------------------------------------------------------------ #
#  4. Create ips.txt if missing                                        #
# ------------------------------------------------------------------ #
if [[ ! -f ips.txt ]]; then
    info "Creating ips.txt template..."
    cat > ips.txt <<'EOF'
# Add one IP or hostname per line (lines starting with # are ignored)
# Examples:
# 1.2.3.4
# example.com
EOF
    ok "ips.txt created — add your IPs to this file"
else
    ok "ips.txt already exists"
fi

# ------------------------------------------------------------------ #
#  5. Install Python dependencies                                      #
# ------------------------------------------------------------------ #
info "Installing Python dependencies..."

# Try normal pip install first, fall back to --break-system-packages for PEP 668
if python3 -m pip install -r requirements.txt --quiet 2>&1; then
    ok "Dependencies installed"
elif python3 -m pip install --break-system-packages -r requirements.txt --quiet 2>&1; then
    ok "Dependencies installed (with --break-system-packages)"
else
    error "Failed to install Python dependencies. Try manually:"
    error "  python3 -m pip install -r requirements.txt"
    error "  or: apt install python3-requests python3-dotenv"
    exit 1
fi

# ------------------------------------------------------------------ #
#  6. Create systemd service                                           #
# ------------------------------------------------------------------ #
if [[ "$HAS_SYSTEMD" == true ]]; then
    SERVICE_NAME="ip-access-monitor"
    SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

    info "Creating systemd service..."

    sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=IP Access Monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${SCRIPT_DIR}
ExecStart=$(which python3) ${SCRIPT_DIR}/monitor.py
Restart=always
RestartSec=30
EnvironmentFile=${SCRIPT_DIR}/.env
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

    ok "Service file created at $SERVICE_FILE"

    # ------------------------------------------------------------------ #
    #  7. Enable & start                                                  #
    # ------------------------------------------------------------------ #
    info "Enabling and starting service..."
    sudo systemctl daemon-reload
    sudo systemctl enable "${SERVICE_NAME}.service"
    sudo systemctl start "${SERVICE_NAME}.service"

    ok "Service enabled and started!"
    echo ""
    info "Service status:"
    sudo systemctl status "${SERVICE_NAME}.service" --no-pager || true
    echo ""
    echo "=========================================="
    echo "  Installation complete!"
    echo "=========================================="
    echo ""
    echo "  Manage the service:"
    echo "    sudo systemctl status  ${SERVICE_NAME}"
    echo "    sudo systemctl stop    ${SERVICE_NAME}"
    echo "    sudo systemctl start   ${SERVICE_NAME}"
    echo "    sudo systemctl restart ${SERVICE_NAME}"
    echo ""
    echo "  View logs:"
    echo "    journalctl -u ${SERVICE_NAME} -f"
    echo ""
    echo "  Edit monitored IPs:"
    echo "    nano ${SCRIPT_DIR}/ips.txt"
    echo ""
else
    warn "systemd not available — run manually with: ./run.sh"
fi

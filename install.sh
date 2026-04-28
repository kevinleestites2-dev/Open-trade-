#!/usr/bin/env bash
# ============================================================================
# Zeus Prime - One-Command Installer
# Usage: curl -fsSL https://raw.githubusercontent.com/kevinleestites2-dev/Open-trade-/main/install.sh | bash
# ============================================================================

set -e

REPO_URL="https://github.com/kevinleestites2-dev/Open-trade-.git"
INSTALL_DIR="$HOME/zeus-prime"
PYTHON_MIN="3.10"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

print_banner() {
    echo -e "${BLUE}"
    echo "╔══════════════════════════════════════════════════════════════╗"
    echo "║              ⚡ ZEUS PRIME INSTALLER ⚡                     ║"
    echo "║     Autonomous Polymarket Trading Bot                       ║"
    echo "╚══════════════════════════════════════════════════════════════╝"
    echo -e "${NC}"
}

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

detect_os() {
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        OS=$ID
    elif [ -d /data/data/com.termux ]; then
        OS="termux"
    else
        OS="unknown"
    fi
    log_info "Detected OS: $OS"
}

install_system_deps() {
    log_info "Installing system dependencies..."

    case $OS in
        ubuntu|debian)
            sudo apt-get update -qq
            sudo apt-get install -y -qq python3 python3-pip python3-venv git curl wget
            ;;
        termux)
            pkg update -y
            pkg install -y python git curl wget
            ;;
        *)
            log_warn "Unknown OS. Attempting generic install..."
            ;;
    esac
}

check_python() {
    log_info "Checking Python version..."

    if command -v python3 &>/dev/null; then
        PY_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
        PY_MAJOR=$(echo $PY_VERSION | cut -d. -f1)
        PY_MINOR=$(echo $PY_VERSION | cut -d. -f2)

        if [ "$PY_MAJOR" -ge 3 ] && [ "$PY_MINOR" -ge 10 ]; then
            log_info "Python $PY_VERSION found"
            return 0
        fi
    fi

    log_warn "Python 3.10+ not found. Installing..."
    case $OS in
        ubuntu|debian)
            sudo apt-get install -y software-properties-common
            sudo add-apt-repository -y ppa:deadsnakes/ppa
            sudo apt-get update -qq
            sudo apt-get install -y python3.11 python3.11-venv python3.11-dev
            sudo update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1
            ;;
        termux)
            pkg install -y python
            ;;
    esac
}

install_ollama() {
    log_info "Checking Ollama installation..."

    if command -v ollama &>/dev/null; then
        log_info "Ollama already installed"
    else
        log_info "Installing Ollama..."
        curl -fsSL https://ollama.ai/install.sh | sh || {
            log_warn "Ollama install failed (optional - bot works without it)"
            return 0
        }
    fi

    # Pull models (non-blocking, optional)
    if command -v ollama &>/dev/null; then
        log_info "Pulling AI models (this may take a while)..."
        ollama pull phi4-mini 2>/dev/null &
        ollama pull qwen2.5-coder:7b 2>/dev/null &
        log_info "Models downloading in background..."
    fi
}

clone_repo() {
    log_info "Cloning Zeus Prime repository..."

    if [ -d "$INSTALL_DIR" ]; then
        log_warn "Directory $INSTALL_DIR already exists. Pulling latest..."
        cd "$INSTALL_DIR"
        git pull origin main || true
    else
        git clone "$REPO_URL" "$INSTALL_DIR"
        cd "$INSTALL_DIR"
    fi
}

setup_python_env() {
    log_info "Setting up Python virtual environment..."

    cd "$INSTALL_DIR"
    python3 -m venv .venv
    source .venv/bin/activate

    log_info "Installing Python dependencies..."
    pip install --upgrade pip -q
    pip install -r requirements.txt -q

    log_info "Dependencies installed successfully"
}

configure_env() {
    log_info "Configuring environment..."

    cd "$INSTALL_DIR"

    if [ -f .env ]; then
        log_warn ".env file already exists. Skipping configuration."
        return 0
    fi

    cp .env.example .env

    echo ""
    echo -e "${YELLOW}═══════════════════════════════════════════════════════════${NC}"
    echo -e "${YELLOW}  CONFIGURATION REQUIRED${NC}"
    echo -e "${YELLOW}═══════════════════════════════════════════════════════════${NC}"
    echo ""

    read -p "Enter your Polygon private key (hex, no 0x prefix): " PRIVATE_KEY
    read -p "Enter your proxy wallet address: " PROXY_WALLET
    read -p "Enter your Polymarket API key: " API_KEY
    read -p "Enter your Polymarket API secret: " API_SECRET
    read -p "Enter your Polymarket API passphrase: " API_PASS
    read -p "Enter your Telegram bot token (or press Enter to skip): " TG_TOKEN
    read -p "Enter your Telegram chat ID (or press Enter to skip): " TG_CHAT
    read -p "Enter initial capital in USDC (default: 1000): " CAPITAL

    CAPITAL=${CAPITAL:-1000}

    sed -i "s/your_private_key_here/$PRIVATE_KEY/" .env
    sed -i "s/your_proxy_wallet_address/$PROXY_WALLET/" .env
    sed -i "s/your_api_key/$API_KEY/" .env
    sed -i "s/your_api_secret/$API_SECRET/" .env
    sed -i "s/your_api_passphrase/$API_PASS/" .env
    sed -i "s/your_telegram_bot_token/$TG_TOKEN/" .env
    sed -i "s/your_telegram_chat_id/$TG_CHAT/" .env
    sed -i "s/INITIAL_CAPITAL=1000/INITIAL_CAPITAL=$CAPITAL/" .env

    log_info "Configuration saved to .env"
}

test_configuration() {
    log_info "Testing configuration..."

    cd "$INSTALL_DIR"
    source .venv/bin/activate

    python3 trade.py --simulate --check-config

    if [ $? -eq 0 ]; then
        log_info "Configuration test PASSED"
    else
        log_error "Configuration test FAILED. Please check your .env file."
        exit 1
    fi
}

setup_autostart() {
    log_info "Setting up auto-start..."

    cd "$INSTALL_DIR"

    # Try PM2 first
    if command -v pm2 &>/dev/null; then
        log_info "Using PM2 for process management..."
        pm2 start "$INSTALL_DIR/.venv/bin/python3" --name zeus-prime -- "$INSTALL_DIR/trade.py"
        pm2 save
        pm2 startup || true
        log_info "PM2 configured. Bot will auto-restart on boot."
        return 0
    fi

    # Try systemd
    if command -v systemctl &>/dev/null && [ "$OS" != "termux" ]; then
        log_info "Using systemd for process management..."

        sudo tee /etc/systemd/system/zeus-prime.service > /dev/null << EOF
[Unit]
Description=Zeus Prime - Autonomous Polymarket Trading Bot
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$INSTALL_DIR
Environment=PATH=$INSTALL_DIR/.venv/bin:/usr/bin:/bin
ExecStart=$INSTALL_DIR/.venv/bin/python3 $INSTALL_DIR/trade.py
Restart=always
RestartSec=10
StandardOutput=append:$INSTALL_DIR/logs/stdout.log
StandardError=append:$INSTALL_DIR/logs/stderr.log

[Install]
WantedBy=multi-user.target
EOF

        sudo systemctl daemon-reload
        sudo systemctl enable zeus-prime
        sudo systemctl start zeus-prime

        log_info "Systemd service configured. Bot will auto-restart on boot."
        return 0
    fi

    # Fallback: nohup
    log_warn "No PM2 or systemd found. Using nohup..."
    nohup "$INSTALL_DIR/.venv/bin/python3" "$INSTALL_DIR/trade.py" \
        > "$INSTALL_DIR/logs/stdout.log" 2>&1 &
    echo $! > "$INSTALL_DIR/.pid"
    log_info "Bot started with PID $(cat $INSTALL_DIR/.pid)"
}

print_success() {
    echo ""
    echo -e "${GREEN}╔══════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║         ⚡ ZEUS PRIME INSTALLED SUCCESSFULLY ⚡              ║${NC}"
    echo -e "${GREEN}╠══════════════════════════════════════════════════════════════╣${NC}"
    echo -e "${GREEN}║                                                              ║${NC}"
    echo -e "${GREEN}║  Location: $INSTALL_DIR${NC}"
    echo -e "${GREEN}║                                                              ║${NC}"
    echo -e "${GREEN}║  Commands:                                                   ║${NC}"
    echo -e "${GREEN}║    Start:   systemctl start zeus-prime                       ║${NC}"
    echo -e "${GREEN}║    Stop:    systemctl stop zeus-prime                        ║${NC}"
    echo -e "${GREEN}║    Status:  systemctl status zeus-prime                      ║${NC}"
    echo -e "${GREEN}║    Logs:    journalctl -u zeus-prime -f                      ║${NC}"
    echo -e "${GREEN}║                                                              ║${NC}"
    echo -e "${GREEN}║  Simulate: python3 trade.py --simulate                       ║${NC}"
    echo -e "${GREEN}║  Config:   nano $INSTALL_DIR/.env${NC}"
    echo -e "${GREEN}║                                                              ║${NC}"
    echo -e "${GREEN}║  Telegram: Send /status to your bot                          ║${NC}"
    echo -e "${GREEN}║                                                              ║${NC}"
    echo -e "${GREEN}╚══════════════════════════════════════════════════════════════╝${NC}"
    echo ""
}

# ============================================================================
# MAIN INSTALLATION FLOW
# ============================================================================

main() {
    print_banner
    detect_os
    install_system_deps
    check_python
    install_ollama
    clone_repo
    setup_python_env
    configure_env
    test_configuration
    setup_autostart
    print_success
}

main "$@"

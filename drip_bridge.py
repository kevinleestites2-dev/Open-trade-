#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════╗
║         DRIP BRIDGE v1.0 — The Capital Conduit               ║
║         Game Fleet → Wallet → OpenTrade Capital Pool         ║
║         MidasPrime monitors. ZeusPrime deploys.              ║
╚══════════════════════════════════════════════════════════════╝

Architecture:
  [Game Fleet] → drip micro-rewards → [PRIME Wallet]
  [PRIME Wallet] → threshold check → [OpenTrade Capital Pool]
  [OpenTrade] → trading profits → [PRIME Wallet]
  [MidasPrime] → monitors everything → signals Forgemaster

Flow:
  1. Game instances auto-play 24/7, accumulate in-game coins
  2. Each game periodically calls /claim → sends MATIC drip to wallet
  3. DripBridge polls wallet balance every POLL_INTERVAL seconds
  4. When balance >= DEPLOY_THRESHOLD → notify ZeusPrime to deploy
  5. ZeusPrime trades with new capital → profits return to wallet
  6. MidasPrime logs everything to war_chest.json
"""

import os
import json
import time
import logging
import requests
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv

# ============================================================================
# BOOTSTRAP
# ============================================================================

BASE_DIR = Path(__file__).parent.resolve()
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)
load_dotenv(BASE_DIR / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [DRIP BRIDGE] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOGS_DIR / "drip_bridge.log"),
    ],
)
log = logging.getLogger("DripBridge")

# ============================================================================
# CONFIG
# ============================================================================

class Config:
    # Wallet
    WALLET_ADDRESS: str   = os.getenv("PROXY_WALLET_ADDRESS", "0x369c2DDDBEb910c48356910069B2903b3Cb4d535")
    POLYGON_RPC:    str   = os.getenv("POLYGON_RPC", "https://polygon-rpc.com")

    # Telegram
    TELEGRAM_TOKEN: str   = os.getenv("TELEGRAM_TOKEN", "")
    TELEGRAM_CHAT:  str   = os.getenv("TELEGRAM_CHAT_ID", "")

    # Bridge thresholds
    POLL_INTERVAL:    int   = 60          # check wallet every 60 seconds
    DEPLOY_THRESHOLD: float = 5.0         # deploy to OpenTrade when >= $5 USDC accumulated
    MIN_RESERVE:      float = 0.5         # always keep $0.50 for gas fees
    COMPOUND_RATE:    float = 0.80        # deploy 80% to trading, keep 20% as reserve

    # War chest
    WAR_CHEST_PATH: str = str(BASE_DIR / "logs" / "war_chest.json")

    # Simulate mode
    SIMULATE: bool = os.getenv("SIMULATE_MODE", "true").lower() == "true"

# ============================================================================
# WAR CHEST LOGGER
# ============================================================================

def load_war_chest() -> dict:
    path = Path(Config.WAR_CHEST_PATH)
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {
        "total_drip_received": 0.0,
        "total_deployed_to_trading": 0.0,
        "total_trading_profits": 0.0,
        "game_instances": 0,
        "last_deploy": None,
        "history": []
    }

def save_war_chest(data: dict):
    with open(Config.WAR_CHEST_PATH, "w") as f:
        json.dump(data, f, indent=2)

def log_event(event_type: str, amount: float, note: str = ""):
    chest = load_war_chest()
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "type": event_type,
        "amount": amount,
        "note": note
    }
    chest["history"].append(entry)

    if event_type == "drip_received":
        chest["total_drip_received"] += amount
    elif event_type == "deployed_to_trading":
        chest["total_deployed_to_trading"] += amount
        chest["last_deploy"] = entry["timestamp"]
    elif event_type == "trading_profit":
        chest["total_trading_profits"] += amount

    save_war_chest(chest)
    log.info(f"[{event_type}] ${amount:.4f} — {note}")

# ============================================================================
# TELEGRAM ALERTS
# ============================================================================

def telegram_alert(msg: str):
    if not Config.TELEGRAM_TOKEN or not Config.TELEGRAM_CHAT:
        log.warning("Telegram not configured — skipping alert")
        return
    try:
        url = f"https://api.telegram.org/bot{Config.TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": Config.TELEGRAM_CHAT, "text": msg}, timeout=10)
    except Exception as e:
        log.error(f"Telegram alert failed: {e}")

# ============================================================================
# WALLET BALANCE CHECK (Polygon MATIC via RPC)
# ============================================================================

def get_wallet_balance_matic() -> float:
    """Query Polygon RPC for MATIC balance of the PRIME wallet."""
    try:
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_getBalance",
            "params": [Config.WALLET_ADDRESS, "latest"],
            "id": 1
        }
        resp = requests.post(Config.POLYGON_RPC, json=payload, timeout=10)
        result = resp.json()
        hex_balance = result["result"]
        wei = int(hex_balance, 16)
        matic = wei / 1e18
        return matic
    except Exception as e:
        log.error(f"Balance check failed: {e}")
        return 0.0

def get_matic_price_usd() -> float:
    """Get current MATIC/USD price from CoinGecko (free, no key)."""
    try:
        url = "https://api.coingecko.com/api/v3/simple/price?ids=matic-network&vs_currencies=usd"
        resp = requests.get(url, timeout=10)
        return resp.json()["matic-network"]["usd"]
    except Exception as e:
        log.warning(f"Price fetch failed: {e} — using $0.80 fallback")
        return 0.80  # fallback price

# ============================================================================
# DEPLOY SIGNAL TO ZEUS PRIME
# ============================================================================

def signal_zeus_prime(deploy_amount_usd: float, matic_balance: float):
    """
    Signal ZeusPrime to deploy capital into trading.
    In production: writes a command file ZeusPrime polls.
    ZeusPrime already checks INITIAL_CAPITAL from .env —
    we update that value dynamically here.
    """
    signal_path = BASE_DIR / "logs" / "deploy_signal.json"
    signal = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": "DEPLOY_CAPITAL",
        "amount_usd": deploy_amount_usd,
        "matic_balance": matic_balance,
        "source": "drip_bridge",
        "status": "pending"
    }
    with open(signal_path, "w") as f:
        json.dump(signal, f, indent=2)

    msg = (
        f"⚡ DRIP BRIDGE → ZEUS PRIME\n"
        f"Capital ready: ${deploy_amount_usd:.2f} USD\n"
        f"MATIC balance: {matic_balance:.4f}\n"
        f"Action: DEPLOY TO TRADING\n"
        f"Source: Game Fleet Drip"
    )
    telegram_alert(msg)
    log.info(f"ZeusPrime deploy signal sent: ${deploy_amount_usd:.2f}")

# ============================================================================
# GAME FLEET REGISTRY
# ============================================================================

def register_game_instance(instance_id: str, url: str):
    """Register a new game instance in the fleet registry."""
    registry_path = BASE_DIR / "logs" / "game_fleet.json"
    registry = {}
    if registry_path.exists():
        with open(registry_path) as f:
            registry = json.load(f)

    registry[instance_id] = {
        "url": url,
        "registered": datetime.now(timezone.utc).isoformat(),
        "status": "active",
        "total_drip": 0.0
    }
    with open(registry_path, "w") as f:
        json.dump(registry, f, indent=2)

    chest = load_war_chest()
    chest["game_instances"] = len(registry)
    save_war_chest(chest)
    log.info(f"Game instance registered: {instance_id} @ {url}")

def get_fleet_size() -> int:
    registry_path = BASE_DIR / "logs" / "game_fleet.json"
    if not registry_path.exists():
        return 0
    with open(registry_path) as f:
        return len(json.load(f))

# ============================================================================
# MAIN BRIDGE LOOP
# ============================================================================

def run_bridge():
    log.info("=" * 60)
    log.info("DRIP BRIDGE v1.0 — ONLINE")
    log.info(f"Wallet: {Config.WALLET_ADDRESS}")
    log.info(f"Deploy threshold: ${Config.DEPLOY_THRESHOLD} USD")
    log.info(f"Poll interval: {Config.POLL_INTERVAL}s")
    log.info(f"Simulate mode: {Config.SIMULATE}")
    log.info("=" * 60)

    telegram_alert(
        "🔱 DRIP BRIDGE ONLINE\n"
        f"Monitoring wallet: {Config.WALLET_ADDRESS[:10]}...\n"
        f"Deploy threshold: ${Config.DEPLOY_THRESHOLD}\n"
        f"Game fleet: {get_fleet_size()} instances\n"
        "Waiting for drip..."
    )

    last_balance = 0.0

    while True:
        try:
            # 1. Check wallet balance
            matic = get_wallet_balance_matic()
            price = get_matic_price_usd()
            usd_value = matic * price

            log.info(f"Wallet: {matic:.4f} MATIC = ${usd_value:.2f} USD | Fleet: {get_fleet_size()} games")

            # 2. Log new drip received
            if matic > last_balance and last_balance > 0:
                drip_amount = (matic - last_balance) * price
                log_event("drip_received", drip_amount, f"{matic - last_balance:.6f} MATIC from game fleet")

            last_balance = matic

            # 3. Check deploy threshold
            deployable = usd_value - Config.MIN_RESERVE
            if deployable >= Config.DEPLOY_THRESHOLD:
                deploy_amount = deployable * Config.COMPOUND_RATE

                if Config.SIMULATE:
                    log.info(f"[SIMULATE] Would deploy ${deploy_amount:.2f} to ZeusPrime")
                else:
                    signal_zeus_prime(deploy_amount, matic)
                    log_event("deployed_to_trading", deploy_amount, f"ZeusPrime capital injection")

            # 4. Fleet growth check
            fleet_size = get_fleet_size()
            if fleet_size == 0:
                log.warning("No game instances registered. Deploy game fleet to start drip.")

        except Exception as e:
            log.error(f"Bridge loop error: {e}")

        time.sleep(Config.POLL_INTERVAL)

# ============================================================================
# STATUS REPORT
# ============================================================================

def print_status():
    chest = load_war_chest()
    matic = get_wallet_balance_matic()
    price = get_matic_price_usd()
    usd = matic * price

    print("\n" + "=" * 60)
    print("DRIP BRIDGE — WAR CHEST STATUS")
    print("=" * 60)
    print(f"Wallet balance:          {matic:.4f} MATIC = ${usd:.2f}")
    print(f"Total drip received:     ${chest['total_drip_received']:.2f}")
    print(f"Total deployed:          ${chest['total_deployed_to_trading']:.2f}")
    print(f"Total trading profits:   ${chest['total_trading_profits']:.2f}")
    print(f"Game fleet size:         {chest['game_instances']} instances")
    print(f"Last deploy:             {chest['last_deploy'] or 'Never'}")
    print("=" * 60)

    # Countdown to targets
    targets = {
        "Nexus (1TB Laptop)": 3000,
        "Citadel (Apartment)": 5000,
        "Steam Machine": 600
    }
    total_earned = chest['total_drip_received'] + chest['total_trading_profits']
    print("\nMIDAS COUNTDOWN:")
    for name, target in targets.items():
        pct = min(100, (total_earned / target) * 100)
        bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
        print(f"  {name}: [{bar}] ${total_earned:.0f}/${target} ({pct:.1f}%)")
    print()

# ============================================================================
# ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        print_status()
    else:
        run_bridge()

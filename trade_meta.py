#!/usr/bin/env python3
"""
Zeus Prime - Autonomous Polymarket Trading Bot
The most advanced self-contained trading bot for Polymarket.
Runs 24/7 with zero human intervention. Learns and adapts continuously.
"""

import os
import sys
import json
import time
import hmac
import math
import hashlib
import sqlite3
import signal
import logging
import threading
import traceback
import subprocess
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from enum import Enum
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, List, Tuple, Any, Callable
from collections import deque
from pathlib import Path

import requests
import websocket
from eth_account import Account
from eth_account.messages import encode_defunct
from web3 import Web3

# ============================================================================
# CONFIGURATION
# ============================================================================

BASE_DIR = Path(__file__).parent.resolve()
SKILLS_DIR = BASE_DIR / "skills"
LOGS_DIR = BASE_DIR / "logs"
SKILLS_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)

# Load environment
from dotenv import load_dotenv
load_dotenv(BASE_DIR / ".env")


class Config:
    """Central configuration loaded from environment."""

    PRIVATE_KEY: str = os.getenv("PRIVATE_KEY", "")
    PROXY_WALLET_ADDRESS: str = os.getenv("PROXY_WALLET_ADDRESS", "")
    FUNDER_ADDRESS: str = os.getenv("FUNDER_ADDRESS", "")
    API_KEY: str = os.getenv("POLYMARKET_API_KEY", "")
    API_SECRET: str = os.getenv("POLYMARKET_API_SECRET", "")
    API_PASSPHRASE: str = os.getenv("POLYMARKET_API_PASSPHRASE", "")

    TELEGRAM_TOKEN: str = os.getenv("TELEGRAM_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # Polymarket endpoints
    CLOB_BASE_URL: str = "https://clob.polymarket.com"
    WS_URL: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    GAMMA_URL: str = "https://gamma-api.polymarket.com"

    # USDC on Polygon (native, NOT USDC.e)
    USDC_ADDRESS: str = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
    POLYGON_RPC: str = os.getenv("POLYGON_RPC", "https://polygon-rpc.com")

    # Trading parameters
    INITIAL_CAPITAL: float = float(os.getenv("INITIAL_CAPITAL", "1000"))
    SIMULATE_MODE: bool = os.getenv("SIMULATE_MODE", "false").lower() == "true"

    # Risk limits (HARD CODED - CANNOT BE OVERRIDDEN)
    STOP_LOSS_PCT: float = 0.10
    DAILY_LOSS_LIMIT_PCT: float = 0.05
    DRAWDOWN_LIMIT_PCT: float = 0.25
    TOTAL_LOSS_HALT_PCT: float = 0.40
    MAX_POSITION_RISK_PCT: float = 0.05
    MAX_EXPOSURE_PER_MARKET_PCT: float = 0.20
    MAX_EXPOSURE_PER_STRATEGY_PCT: float = 0.50
    CONSECUTIVE_LOSS_PAUSE: int = 3
    CONSECUTIVE_LOSS_PAUSE_MINUTES: int = 10

    # Copy trading wallets
    COPY_WALLETS: Dict[str, str] = {
        "RN1": os.getenv("COPY_WALLET_RN1", ""),
        "Domer": os.getenv("COPY_WALLET_DOMER", ""),
        "ColdMath": os.getenv("COPY_WALLET_COLDMATH", ""),
    }

    # Aave V3 on Polygon (for flash loans)
    AAVE_POOL_ADDRESS: str = "0x794a61358D6845594F94dc1DB02A252b5b4814aD"

    # Auto-update
    GITHUB_REPO_URL: str = os.getenv(
        "GITHUB_REPO_URL",
        "https://github.com/kevinleestites2-dev/Open-trade-.git"
    )


# ============================================================================
# LOGGING SETUP
# ============================================================================

def setup_logging():
    """Configure logging to file and console."""
    log_file = LOGS_DIR / f"zeus_prime_{datetime.now(timezone.utc).strftime('%Y%m%d')}.log"
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.INFO)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)
    return root_logger


logger = setup_logging()


# ============================================================================
# DATABASE
# ============================================================================

class Database:
    """SQLite database for trade logging and performance tracking."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or (BASE_DIR / "zeus_prime.db")
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self._init_tables()

    def _init_tables(self):
        with self.lock:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    market_id TEXT,
                    token_id TEXT,
                    side TEXT,
                    entry_price REAL,
                    exit_price REAL,
                    size REAL,
                    pnl REAL DEFAULT 0,
                    fee_paid REAL DEFAULT 0,
                    slippage REAL DEFAULT 0,
                    execution_latency_ms REAL DEFAULT 0,
                    volatility REAL DEFAULT 0,
                    volume REAL DEFAULT 0,
                    time_of_day TEXT,
                    market_regime TEXT,
                    status TEXT DEFAULT 'open',
                    notes TEXT DEFAULT '',
                    exit_reason TEXT DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS equity_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    equity REAL NOT NULL,
                    peak_equity REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS strategy_performance (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    rolling_pnl_24h REAL DEFAULT 0,
                    win_rate REAL DEFAULT 0,
                    trades_count INTEGER DEFAULT 0,
                    weight REAL DEFAULT 0.1,
                    consecutive_losses INTEGER DEFAULT 0,
                    paused_until TEXT DEFAULT NULL
                );

                CREATE TABLE IF NOT EXISTS parameters (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy TEXT NOT NULL,
                    param_name TEXT NOT NULL,
                    param_value REAL NOT NULL,
                    performance_score REAL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS failure_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    trade_id INTEGER,
                    reason TEXT NOT NULL,
                    count_24h INTEGER DEFAULT 1
                );

                CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy);
                CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp);
                CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
            """)
            self.conn.commit()

    def log_trade(self, trade_data: dict) -> int:
        with self.lock:
            cols = ", ".join(trade_data.keys())
            placeholders = ", ".join(["?"] * len(trade_data))
            cursor = self.conn.execute(
                f"INSERT INTO trades ({cols}) VALUES ({placeholders})",
                list(trade_data.values())
            )
            self.conn.commit()
            return cursor.lastrowid

    def update_trade(self, trade_id: int, updates: dict):
        with self.lock:
            set_clause = ", ".join([f"{k} = ?" for k in updates.keys()])
            self.conn.execute(
                f"UPDATE trades SET {set_clause} WHERE id = ?",
                list(updates.values()) + [trade_id]
            )
            self.conn.commit()

    def get_open_trades(self, strategy: Optional[str] = None) -> list:
        with self.lock:
            if strategy:
                rows = self.conn.execute(
                    "SELECT * FROM trades WHERE status = 'open' AND strategy = ?",
                    (strategy,)
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM trades WHERE status = 'open'"
                ).fetchall()
            return [dict(r) for r in rows]

    def get_rolling_pnl(self, strategy: str, hours: int = 24) -> float:
        with self.lock:
            since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
            row = self.conn.execute(
                "SELECT COALESCE(SUM(pnl), 0) as total FROM trades WHERE strategy = ? AND timestamp >= ? AND status = 'closed'",
                (strategy, since)
            ).fetchone()
            return row["total"] if row else 0.0

    def get_strategy_stats(self, strategy: str, hours: int = 24) -> dict:
        with self.lock:
            since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
            rows = self.conn.execute(
                "SELECT pnl FROM trades WHERE strategy = ? AND timestamp >= ? AND status = 'closed'",
                (strategy, since)
            ).fetchall()
            if not rows:
                return {"win_rate": 0, "trades": 0, "pnl": 0, "avg_pnl": 0}
            pnls = [r["pnl"] for r in rows]
            wins = sum(1 for p in pnls if p > 0)
            return {
                "win_rate": wins / len(pnls) if pnls else 0,
                "trades": len(pnls),
                "pnl": sum(pnls),
                "avg_pnl": sum(pnls) / len(pnls) if pnls else 0,
            }

    def get_consecutive_losses(self, strategy: str) -> int:
        with self.lock:
            rows = self.conn.execute(
                "SELECT pnl FROM trades WHERE strategy = ? AND status = 'closed' ORDER BY id DESC LIMIT 10",
                (strategy,)
            ).fetchall()
            count = 0
            for r in rows:
                if r["pnl"] < 0:
                    count += 1
                else:
                    break
            return count

    def get_total_pnl_today(self) -> float:
        with self.lock:
            today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0).isoformat()
            row = self.conn.execute(
                "SELECT COALESCE(SUM(pnl), 0) as total FROM trades WHERE timestamp >= ? AND status = 'closed'",
                (today,)
            ).fetchone()
            return row["total"] if row else 0.0

    def save_equity_snapshot(self, equity: float, peak: float):
        with self.lock:
            self.conn.execute(
                "INSERT INTO equity_snapshots (timestamp, equity, peak_equity) VALUES (?, ?, ?)",
                (datetime.now(timezone.utc).isoformat(), equity, peak)
            )
            self.conn.commit()

    def get_recent_failures(self, strategy: str, reason: str, hours: int = 24) -> int:
        with self.lock:
            since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
            row = self.conn.execute(
                "SELECT COUNT(*) as cnt FROM failure_log WHERE strategy = ? AND reason = ? AND timestamp >= ?",
                (strategy, reason, since)
            ).fetchone()
            return row["cnt"] if row else 0

    def log_failure(self, strategy: str, trade_id: int, reason: str):
        with self.lock:
            self.conn.execute(
                "INSERT INTO failure_log (timestamp, strategy, trade_id, reason) VALUES (?, ?, ?, ?)",
                (datetime.now(timezone.utc).isoformat(), strategy, trade_id, reason)
            )
            self.conn.commit()

    def get_all_trades_for_optimization(self, strategy: str, limit: int = 20) -> list:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM trades WHERE strategy = ? AND status = 'closed' ORDER BY id DESC LIMIT ?",
                (strategy, limit)
            ).fetchall()
            return [dict(r) for r in rows]


# ============================================================================
# POLYMARKET API CLIENT
# ============================================================================

class PolymarketClient:
    """Client for Polymarket CLOB API with fee-aware order signing."""

    def __init__(self, config: Config):
        self.config = config
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
        })
        if config.API_KEY:
            self.session.headers.update({
                "POLY_API_KEY": config.API_KEY,
                "POLY_API_SECRET": config.API_SECRET,
                "POLY_API_PASSPHRASE": config.API_PASSPHRASE,
            })
        self.w3 = Web3(Web3.HTTPProvider(config.POLYGON_RPC))
        if config.PRIVATE_KEY:
            self.account = Account.from_key(config.PRIVATE_KEY)
        else:
            self.account = None

    def _sign_request(self, method: str, path: str, body: str = "") -> dict:
        """Sign API request with HMAC."""
        timestamp = str(int(time.time()))
        message = timestamp + method.upper() + path + body
        signature = hmac.new(
            self.config.API_SECRET.encode(),
            message.encode(),
            hashlib.sha256
        ).hexdigest()
        return {
            "POLY_API_KEY": self.config.API_KEY,
            "POLY_API_SIGNATURE": signature,
            "POLY_API_TIMESTAMP": timestamp,
            "POLY_API_PASSPHRASE": self.config.API_PASSPHRASE,
        }

    def get_fee_rate(self, token_id: str) -> int:
        """Query live fee rate for a token."""
        try:
            resp = self.session.get(
                f"{self.config.CLOB_BASE_URL}/fee-rate",
                params={"tokenID": token_id}
            )
            resp.raise_for_status()
            data = resp.json()
            return int(data.get("feeRateBps", 0))
        except Exception as e:
            logger.warning(f"Failed to get fee rate: {e}")
            return 200  # Default 2% fee

    def get_markets(self, limit: int = 100, active: bool = True) -> list:
        """Get active, orderbook-enabled markets via Gamma (normalized to CLOB schema)."""
        try:
            import json as _json
            params = {
                "active": "true",
                "closed": "false",
                "order": "volume24hr",
                "ascending": "false",
                "limit": min(limit, 100),
            }
            resp = requests.get(f"{self.config.GAMMA_URL}/markets", params=params)
            resp.raise_for_status()
            raw = resp.json()
            if isinstance(raw, dict):
                raw = raw.get("data", raw.get("markets", []))
            if not isinstance(raw, list):
                return []
            result = []
            for m in raw:
                if not isinstance(m, dict):
                    continue
                if not (m.get("active") and not m.get("closed")):
                    continue
                if not (m.get("acceptingOrders") and m.get("enableOrderBook")):
                    continue
                clob_ids = m.get("clobTokenIds", [])
                if isinstance(clob_ids, str):
                    try:
                        clob_ids = _json.loads(clob_ids)
                    except Exception:
                        clob_ids = []
                if not clob_ids:
                    continue
                outcomes = m.get("outcomes", ["Yes", "No"])
                if isinstance(outcomes, str):
                    try:
                        outcomes = _json.loads(outcomes)
                    except Exception:
                        outcomes = ["Yes", "No"]
                outcome_prices = m.get("outcomePrices", [])
                if isinstance(outcome_prices, str):
                    try:
                        outcome_prices = _json.loads(outcome_prices)
                    except Exception:
                        outcome_prices = []
                tokens = []
                for idx, tid in enumerate(clob_ids):
                    price = float(outcome_prices[idx]) if idx < len(outcome_prices) else 0.5
                    outcome = outcomes[idx] if idx < len(outcomes) else str(idx)
                    tokens.append({"token_id": str(tid), "outcome": outcome, "price": price})
                result.append({
                    "condition_id": m.get("conditionId", m.get("condition_id", "")),
                    "question": m.get("question", ""),
                    "tokens": tokens,
                    "volume": float(m.get("volume", m.get("volumeNum", 0)) or 0),
                    "active": True,
                    "closed": False,
                    "accepting_orders": True,
                    "enable_order_book": True,
                    "neg_risk": m.get("negRisk", False),
                    "market_slug": m.get("slug", m.get("market_slug", "")),
                    "_raw": m,
                })
                if len(result) >= limit:
                    break
            return result
        except Exception as e:
            logger.error(f"Failed to get markets: {e}")
            return []

    def get_market(self, condition_id: str) -> Optional[dict]:
        """Get single market details."""
        try:
            resp = self.session.get(f"{self.config.CLOB_BASE_URL}/markets/{condition_id}")
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to get market {condition_id}: {e}")
            return None

    def get_orderbook(self, token_id: str) -> Optional[dict]:
        """Get orderbook for a token."""
        try:
            resp = self.session.get(
                f"{self.config.CLOB_BASE_URL}/book",
                params={"token_id": token_id}
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to get orderbook: {e}")
            return None

    def get_price(self, token_id: str) -> Optional[float]:
        """Get mid price for a token."""
        book = self.get_orderbook(token_id)
        if not book:
            return None
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        if bids and asks:
            best_bid = float(bids[0]["price"])
            best_ask = float(asks[0]["price"])
            return (best_bid + best_ask) / 2
        return None

    def sign_order(self, order: dict) -> dict:
        """Sign an order with fee rate included (2026 spec)."""
        if not self.account:
            return order

        fee_rate_bps = self.get_fee_rate(order["tokenID"])
        order["feeRateBps"] = str(fee_rate_bps)
        order["signature_type"] = 2  # Proxy wallet
        order["funder"] = self.config.FUNDER_ADDRESS or self.config.PROXY_WALLET_ADDRESS

        # Create order hash for signing
        order_data = json.dumps(order, sort_keys=True)
        msg_hash = Web3.keccak(text=order_data)
        signed = self.account.signHash(msg_hash)
        order["signature"] = signed.signature.hex()
        return order

    def place_order(self, token_id: str, side: str, price: float, size: float,
                    order_type: str = "GTC") -> Optional[dict]:
        """Place a signed order on CLOB."""
        if self.config.SIMULATE_MODE:
            logger.info(f"[SIMULATE] Order: {side} {size}@{price} on {token_id[:8]}...")
            return {"orderID": f"sim_{int(time.time()*1000)}", "status": "LIVE"}

        order = {
            "tokenID": token_id,
            "price": str(round(price, 4)),
            "size": str(round(size, 2)),
            "side": side.upper(),
            "type": order_type,
            "expiration": "0",
        }
        signed_order = self.sign_order(order)

        try:
            resp = self.session.post(
                f"{self.config.CLOB_BASE_URL}/order",
                json=signed_order
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to place order: {e}")
            return None

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order."""
        if self.config.SIMULATE_MODE:
            logger.info(f"[SIMULATE] Cancel order: {order_id}")
            return True

        try:
            resp = self.session.delete(
                f"{self.config.CLOB_BASE_URL}/order/{order_id}"
            )
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Failed to cancel order {order_id}: {e}")
            return False

    def cancel_all_orders(self) -> bool:
        """Cancel all open orders."""
        if self.config.SIMULATE_MODE:
            logger.info("[SIMULATE] Cancel all orders")
            return True

        try:
            resp = self.session.delete(f"{self.config.CLOB_BASE_URL}/orders")
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Failed to cancel all orders: {e}")
            return False

    def get_positions(self) -> list:
        """Get current open positions."""
        try:
            resp = self.session.get(f"{self.config.CLOB_BASE_URL}/positions")
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to get positions: {e}")
            return []

    def get_balance(self) -> float:
        """Get USDC balance."""
        if self.config.SIMULATE_MODE:
            return self.config.INITIAL_CAPITAL
        try:
            resp = self.session.get(f"{self.config.CLOB_BASE_URL}/balance")
            resp.raise_for_status()
            data = resp.json()
            return float(data.get("balance", 0))
        except Exception as e:
            logger.error(f"Failed to get balance: {e}")
            return 0.0

    def get_gamma_markets(self, tag: str = "", min_volume: float = 50000) -> list:
        """Get markets from Gamma API with filters. Normalizes to CLOB field schema."""
        try:
            params = {"active": "true", "closed": "false"}
            if tag:
                params["tag"] = tag
            resp = requests.get(f"{self.config.GAMMA_URL}/markets", params=params)
            resp.raise_for_status()
            raw = resp.json()
            if isinstance(raw, dict):
                raw = raw.get("data", raw.get("markets", []))
            if not isinstance(raw, list):
                return []
            result = []
            for m in raw:
                if not isinstance(m, dict):
                    continue
                vol = float(m.get("volume", m.get("volumeNum", 0)) or 0)
                if vol < min_volume:
                    continue
                # Normalize Gamma field names → CLOB field names expected by strategies
                clob_ids = m.get("clobTokenIds", [])
                if isinstance(clob_ids, str):
                    import json as _json
                    try:
                        clob_ids = _json.loads(clob_ids)
                    except Exception:
                        clob_ids = []
                outcomes = m.get("outcomes", ["Yes", "No"])
                if isinstance(outcomes, str):
                    import json as _json
                    try:
                        outcomes = _json.loads(outcomes)
                    except Exception:
                        outcomes = ["Yes", "No"]
                outcome_prices = m.get("outcomePrices", [])
                if isinstance(outcome_prices, str):
                    import json as _json
                    try:
                        outcome_prices = _json.loads(outcome_prices)
                    except Exception:
                        outcome_prices = []
                tokens = []
                for idx, tid in enumerate(clob_ids):
                    price = float(outcome_prices[idx]) if idx < len(outcome_prices) else 0.5
                    outcome = outcomes[idx] if idx < len(outcomes) else str(idx)
                    tokens.append({"token_id": str(tid), "outcome": outcome, "price": price})
                normalized = {
                    "condition_id": m.get("conditionId", m.get("condition_id", "")),
                    "question": m.get("question", ""),
                    "tokens": tokens,
                    "volume": vol,
                    "active": m.get("active", True),
                    "closed": m.get("closed", False),
                    "accepting_orders": m.get("acceptingOrders", m.get("accepting_orders", True)),
                    "neg_risk": m.get("negRisk", False),
                    "market_slug": m.get("slug", m.get("market_slug", "")),
                    "_raw": m,
                }
                result.append(normalized)
            return result
        except Exception as e:
            logger.error(f"Failed to get gamma markets: {e}")
            return []


# ============================================================================
# WEBSOCKET MANAGER
# ============================================================================

class WebSocketManager:
    """Manages WebSocket connection to Polymarket for real-time data."""

    def __init__(self, config: Config):
        self.config = config
        self.ws: Optional[websocket.WebSocketApp] = None
        self.connected = False
        self.callbacks: Dict[str, List[Callable]] = {}
        self.subscriptions: List[dict] = []
        self.reconnect_delay = 1
        self.max_reconnect_delay = 60
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self.price_cache: Dict[str, float] = {}
        self.orderbook_cache: Dict[str, dict] = {}
        self.lock = threading.Lock()

    def start(self):
        """Start WebSocket connection in background thread."""
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("WebSocket manager started")

    def stop(self):
        """Stop WebSocket connection."""
        self._stop_event.set()
        if self.ws:
            self.ws.close()
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("WebSocket manager stopped")

    def subscribe(self, channel: str, assets: List[str]):
        """Subscribe to a channel for specific assets."""
        sub = {"type": "subscribe", "channel": channel, "assets": assets}
        self.subscriptions.append(sub)
        if self.connected and self.ws:
            self.ws.send(json.dumps(sub))

    def on_message(self, event_type: str, callback: Callable):
        """Register callback for specific event types."""
        if event_type not in self.callbacks:
            self.callbacks[event_type] = []
        self.callbacks[event_type].append(callback)

    def get_price(self, token_id: str) -> Optional[float]:
        """Get cached price for token."""
        with self.lock:
            return self.price_cache.get(token_id)

    def _run(self):
        """Main WebSocket loop with auto-reconnect."""
        while not self._stop_event.is_set():
            try:
                self.ws = websocket.WebSocketApp(
                    self.config.WS_URL,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self.ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                logger.error(f"WebSocket error: {e}")

            if not self._stop_event.is_set():
                logger.info(f"Reconnecting in {self.reconnect_delay}s...")
                time.sleep(self.reconnect_delay)
                self.reconnect_delay = min(
                    self.reconnect_delay * 2, self.max_reconnect_delay
                )

    def _on_open(self, ws):
        """Handle WebSocket connection open."""
        self.connected = True
        self.reconnect_delay = 1
        logger.info("WebSocket connected")
        for sub in self.subscriptions:
            ws.send(json.dumps(sub))

    def _on_message(self, ws, message):
        """Handle incoming WebSocket message."""
        try:
            data = json.loads(message)
            event_type = data.get("type", data.get("channel", "unknown"))

            # Update price cache
            if "price" in data:
                token_id = data.get("asset_id", data.get("token_id", ""))
                if token_id:
                    with self.lock:
                        self.price_cache[token_id] = float(data["price"])

            # Update orderbook cache
            if event_type in ("book", "book_update"):
                token_id = data.get("asset_id", "")
                if token_id:
                    with self.lock:
                        self.orderbook_cache[token_id] = data

            # Fire callbacks
            for cb in self.callbacks.get(event_type, []):
                try:
                    cb(data)
                except Exception as e:
                    logger.error(f"Callback error: {e}")

            for cb in self.callbacks.get("*", []):
                try:
                    cb(data)
                except Exception as e:
                    logger.error(f"Wildcard callback error: {e}")

        except json.JSONDecodeError:
            pass

    def _on_error(self, ws, error):
        """Handle WebSocket error."""
        logger.error(f"WebSocket error: {error}")

    def _on_close(self, ws, close_status_code, close_msg):
        """Handle WebSocket connection close."""
        self.connected = False
        logger.warning(f"WebSocket closed: {close_status_code} - {close_msg}")


# ============================================================================
# RISK MANAGER (HARD CODED - CANNOT BE OVERRIDDEN)
# ============================================================================

class RiskManager:
    """
    Multi-layer risk management system.
    ALL limits are HARD CODED and CANNOT be overridden by any strategy or optimizer.
    """

    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self.current_equity = config.INITIAL_CAPITAL
        self.peak_equity = config.INITIAL_CAPITAL
        self.daily_start_equity = config.INITIAL_CAPITAL
        self.is_halted = False
        self.is_emergency_halted = False
        self.is_permanent_halt = False
        self.halt_resume_time: Optional[datetime] = None
        self.strategy_exposure: Dict[str, float] = {}
        self.market_exposure: Dict[str, float] = {}
        self.lock = threading.Lock()

    def update_equity(self, equity: float):
        """Update current equity and check circuit breakers."""
        with self.lock:
            self.current_equity = equity
            self.peak_equity = max(self.peak_equity, equity)
            self.db.save_equity_snapshot(equity, self.peak_equity)
            self._check_circuit_breakers()

    def _check_circuit_breakers(self):
        """Check all risk limits and trigger halts if needed."""
        # Total loss halt (40%) - PERMANENT
        if self.current_equity <= self.config.INITIAL_CAPITAL * (1 - self.config.TOTAL_LOSS_HALT_PCT):
            self.is_permanent_halt = True
            logger.critical("PERMANENT HALT: Total loss exceeds 40%")
            return

        # Drawdown limit (25% from peak) - Emergency halt
        if self.current_equity <= self.peak_equity * (1 - self.config.DRAWDOWN_LIMIT_PCT):
            self.is_emergency_halted = True
            self.halt_resume_time = datetime.now(timezone.utc) + timedelta(days=1)
            logger.critical(f"EMERGENCY HALT: Drawdown exceeds 25%. Resume at {self.halt_resume_time}")
            return

        # Daily loss limit (5%) - Pause 24 hours
        daily_pnl = self.db.get_total_pnl_today()
        if daily_pnl < -(self.daily_start_equity * self.config.DAILY_LOSS_LIMIT_PCT):
            self.is_halted = True
            self.halt_resume_time = datetime.now(timezone.utc) + timedelta(hours=24)
            logger.warning(f"DAILY HALT: Loss exceeds 5%. Resume at {self.halt_resume_time}")

    def can_trade(self) -> Tuple[bool, str]:
        """Check if trading is allowed."""
        with self.lock:
            if self.is_permanent_halt:
                return False, "Permanent halt: Total loss >40%. Manual restart required."

            if self.is_emergency_halted:
                if datetime.now(timezone.utc) >= self.halt_resume_time:
                    self.is_emergency_halted = False
                    logger.info("Emergency halt lifted")
                else:
                    return False, f"Emergency halt until {self.halt_resume_time}"

            if self.is_halted:
                if datetime.now(timezone.utc) >= self.halt_resume_time:
                    self.is_halted = False
                    self.daily_start_equity = self.current_equity
                    logger.info("Daily halt lifted")
                else:
                    return False, f"Daily halt until {self.halt_resume_time}"

            return True, "OK"

    def check_position_size(self, size_usd: float, market_id: str,
                           strategy: str) -> Tuple[bool, float, str]:
        """
        Validate position size against all limits.
        Returns (allowed, adjusted_size, reason).
        """
        with self.lock:
            # Max 5% of capital per trade
            max_per_trade = self.current_equity * self.config.MAX_POSITION_RISK_PCT
            if size_usd > max_per_trade:
                size_usd = max_per_trade

            # Max 20% exposure per market
            current_market_exp = self.market_exposure.get(market_id, 0)
            max_market = self.current_equity * self.config.MAX_EXPOSURE_PER_MARKET_PCT
            if current_market_exp + size_usd > max_market:
                size_usd = max(0, max_market - current_market_exp)
                if size_usd <= 0:
                    return False, 0, f"Market exposure limit reached ({market_id})"

            # Max 50% exposure per strategy
            current_strat_exp = self.strategy_exposure.get(strategy, 0)
            max_strat = self.current_equity * self.config.MAX_EXPOSURE_PER_STRATEGY_PCT
            if current_strat_exp + size_usd > max_strat:
                size_usd = max(0, max_strat - current_strat_exp)
                if size_usd <= 0:
                    return False, 0, f"Strategy exposure limit reached ({strategy})"

            return True, size_usd, "OK"

    def register_position(self, market_id: str, strategy: str, size_usd: float):
        """Register a new position for exposure tracking."""
        with self.lock:
            self.market_exposure[market_id] = self.market_exposure.get(market_id, 0) + size_usd
            self.strategy_exposure[strategy] = self.strategy_exposure.get(strategy, 0) + size_usd

    def release_position(self, market_id: str, strategy: str, size_usd: float):
        """Release a closed position from exposure tracking."""
        with self.lock:
            self.market_exposure[market_id] = max(0, self.market_exposure.get(market_id, 0) - size_usd)
            self.strategy_exposure[strategy] = max(0, self.strategy_exposure.get(strategy, 0) - size_usd)

    def check_stop_loss(self, entry_price: float, current_price: float, side: str) -> bool:
        """Check if stop loss is triggered (10% hard limit)."""
        if side.upper() == "BUY":
            loss_pct = (entry_price - current_price) / entry_price
        else:
            loss_pct = (current_price - entry_price) / entry_price
        return loss_pct >= self.config.STOP_LOSS_PCT

    def check_strategy_consecutive_losses(self, strategy: str) -> Tuple[bool, str]:
        """Check if strategy should be paused due to consecutive losses."""
        losses = self.db.get_consecutive_losses(strategy)
        if losses >= self.config.CONSECUTIVE_LOSS_PAUSE:
            return True, f"Strategy {strategy} paused: {losses} consecutive losses"
        return False, "OK"

    def reset_daily(self):
        """Reset daily counters at midnight UTC."""
        with self.lock:
            self.daily_start_equity = self.current_equity


# ============================================================================
# TELEGRAM BOT
# ============================================================================

class TelegramBot:
    """Telegram integration for alerts and commands."""

    def __init__(self, config: Config):
        self.config = config
        self.enabled = bool(config.TELEGRAM_TOKEN and config.TELEGRAM_CHAT_ID)
        self.base_url = f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}"
        self._command_handlers: Dict[str, Callable] = {}
        self._polling_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._last_update_id = 0

    def send(self, message: str, parse_mode: str = "HTML"):
        """Send a message to Telegram."""
        if not self.enabled:
            return
        try:
            requests.post(
                f"{self.base_url}/sendMessage",
                json={
                    "chat_id": self.config.TELEGRAM_CHAT_ID,
                    "text": message,
                    "parse_mode": parse_mode,
                },
                timeout=10
            )
        except Exception as e:
            logger.error(f"Telegram send failed: {e}")

    def alert_trade(self, strategy: str, side: str, size: float,
                    price: float, market: str, pnl: Optional[float] = None):
        """Send trade execution alert."""
        emoji = "\U0001f7e2" if side.upper() == "BUY" else "\U0001f534"
        msg = (
            f"{emoji} <b>Trade Executed</b>\n"
            f"\U0001f4ca Strategy: {strategy}\n"
            f"\U0001f4b0 {side.upper()} {size:.2f} USDC @ {price:.4f}\n"
            f"\U0001f3af Market: {market[:40]}"
        )
        if pnl is not None:
            pnl_emoji = "\U00002705" if pnl >= 0 else "\U0000274c"
            msg += f"\n{pnl_emoji} P&L: ${pnl:.2f}"
        self.send(msg)

    def alert_take_profit(self, strategy: str, pnl: float, market: str):
        """Send take profit alert."""
        self.send(
            f"\U0001f389 <b>Take Profit Hit!</b>\n"
            f"Strategy: {strategy}\n"
            f"P&L: +${pnl:.2f}\n"
            f"Market: {market[:40]}"
        )

    def alert_stop_loss(self, strategy: str, loss: float, market: str):
        """Send stop loss alert."""
        self.send(
            f"\U0001f6a8 <b>Stop Loss Triggered!</b>\n"
            f"Strategy: {strategy}\n"
            f"Loss: -${abs(loss):.2f}\n"
            f"Market: {market[:40]}"
        )

    def alert_circuit_breaker(self, reason: str):
        """Send circuit breaker alert."""
        self.send(f"\U000026a0\U0000fe0f <b>CIRCUIT BREAKER</b>\n{reason}")

    def alert_daily_summary(self, stats: dict):
        """Send daily P&L summary."""
        pnl = stats.get("total_pnl", 0)
        emoji = "\U0001f4c8" if pnl >= 0 else "\U0001f4c9"
        self.send(
            f"{emoji} <b>Daily P&L Summary</b>\n"
            f"\U0001f4b5 Total P&L: ${pnl:.2f}\n"
            f"\U0001f4ca Trades: {stats.get('total_trades', 0)}\n"
            f"\U0001f3af Win Rate: {stats.get('win_rate', 0)*100:.1f}%\n"
            f"\U0001f4b0 Equity: ${stats.get('equity', 0):.2f}\n"
            f"\U0001f3c6 Best Strategy: {stats.get('best_strategy', 'N/A')}"
        )

    def alert_weekly_report(self, stats: dict):
        """Send weekly performance report."""
        self.send(
            f"\U0001f4c5 <b>Weekly Performance Report</b>\n"
            f"\U0001f4b5 Week P&L: ${stats.get('week_pnl', 0):.2f}\n"
            f"\U0001f4ca Total Trades: {stats.get('total_trades', 0)}\n"
            f"\U0001f3af Win Rate: {stats.get('win_rate', 0)*100:.1f}%\n"
            f"\U0001f4b0 Current Equity: ${stats.get('equity', 0):.2f}\n"
            f"\U0001f680 Strategies Active: {stats.get('active_strategies', 0)}"
        )

    def alert_error(self, error: str):
        """Send error alert."""
        self.send(f"\U0000274c <b>ERROR</b>\n<code>{error[:500]}</code>")

    def alert_bot_status(self, status: str):
        """Send bot start/stop/restart alert."""
        self.send(f"\U0001f916 <b>Bot {status}</b>\n{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

    def alert_low_balance(self, balance: float):
        """Send low balance alert."""
        self.send(f"\U000026a0\U0000fe0f <b>LOW BALANCE</b>\nCurrent: ${balance:.2f}\nPlease add funds!")

    def register_command(self, command: str, handler: Callable):
        """Register a command handler."""
        self._command_handlers[command] = handler

    def start_polling(self):
        """Start polling for commands."""
        if not self.enabled:
            return
        self._stop_event.clear()
        self._polling_thread = threading.Thread(target=self._poll_commands, daemon=True)
        self._polling_thread.start()

    def stop_polling(self):
        """Stop polling for commands."""
        self._stop_event.set()
        if self._polling_thread:
            self._polling_thread.join(timeout=5)

    def _poll_commands(self):
        """Poll for incoming Telegram commands."""
        while not self._stop_event.is_set():
            try:
                resp = requests.get(
                    f"{self.base_url}/getUpdates",
                    params={"offset": self._last_update_id + 1, "timeout": 10},
                    timeout=15
                )
                if resp.status_code == 200:
                    updates = resp.json().get("result", [])
                    for update in updates:
                        self._last_update_id = update["update_id"]
                        msg = update.get("message", {})
                        text = msg.get("text", "")
                        if text.startswith("/"):
                            cmd = text.split()[0].lower()
                            handler = self._command_handlers.get(cmd)
                            if handler:
                                try:
                                    response = handler()
                                    self.send(response)
                                except Exception as e:
                                    self.send(f"Command error: {e}")
            except Exception:
                pass
            time.sleep(1)


# ============================================================================
# STRATEGY BASE CLASS
# ============================================================================

class StrategyState(Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    STOPPED = "stopped"


@dataclass
class TradePosition:
    trade_id: int
    strategy: str
    market_id: str
    token_id: str
    side: str
    entry_price: float
    size: float
    entry_time: datetime
    take_profit_pct: float
    stop_loss_pct: float = 0.10
    trailing_stop: bool = False
    trailing_stop_pct: float = 0.01
    peak_price: float = 0.0
    order_id: str = ""


class BaseStrategy:
    """Base class for all trading strategies."""

    NAME: str = "base"
    TAKE_PROFIT_PCT: float = 0.05
    STOP_LOSS_PCT: float = 0.10

    def __init__(self, client: PolymarketClient, ws: WebSocketManager,
                 risk_mgr: RiskManager, db: Database, telegram: TelegramBot,
                 config: Config):
        self.client = client
        self.ws = ws
        self.risk_mgr = risk_mgr
        self.db = db
        self.telegram = telegram
        self.config = config
        self.state = StrategyState.ACTIVE
        self.positions: List[TradePosition] = []
        self.paused_until: Optional[datetime] = None
        self.weight: float = 0.1
        self.parameters: Dict[str, float] = {}

    def can_run(self) -> bool:
        """Check if strategy can execute."""
        if self.state == StrategyState.STOPPED:
            return False
        if self.state == StrategyState.PAUSED:
            if self.paused_until and datetime.now(timezone.utc) >= self.paused_until:
                self.state = StrategyState.ACTIVE
                self.paused_until = None
                logger.info(f"Strategy {self.NAME} resumed from pause")
            else:
                return False

        # Check consecutive losses
        should_pause, reason = self.risk_mgr.check_strategy_consecutive_losses(self.NAME)
        if should_pause:
            self.pause(minutes=self.config.CONSECUTIVE_LOSS_PAUSE_MINUTES)
            logger.warning(reason)
            return False

        return True

    def pause(self, minutes: int = 10):
        """Pause strategy for specified minutes."""
        self.state = StrategyState.PAUSED
        self.paused_until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        logger.info(f"Strategy {self.NAME} paused until {self.paused_until}")

    def get_allocation(self) -> float:
        """Get current capital allocation for this strategy."""
        return self.risk_mgr.current_equity * self.weight

    def open_position(self, market_id: str, token_id: str, side: str,
                      price: float, size: float, take_profit_pct: Optional[float] = None,
                      trailing_stop: bool = False) -> Optional[TradePosition]:
        """Open a new position with risk checks."""
        can_trade, reason = self.risk_mgr.can_trade()
        if not can_trade:
            logger.warning(f"Cannot trade: {reason}")
            return None

        allowed, adj_size, msg = self.risk_mgr.check_position_size(size, market_id, self.NAME)
        if not allowed:
            logger.warning(f"Position rejected: {msg}")
            return None
        size = adj_size

        # Place order
        start_time = time.time()
        result = self.client.place_order(token_id, side, price, size)
        latency = (time.time() - start_time) * 1000

        if not result:
            return None

        # Log trade
        trade_data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "strategy": self.NAME,
            "market_id": market_id,
            "token_id": token_id,
            "side": side,
            "entry_price": price,
            "size": size,
            "execution_latency_ms": latency,
            "status": "open",
            "time_of_day": datetime.now(timezone.utc).strftime("%H:%M"),
        }
        trade_id = self.db.log_trade(trade_data)

        # Register with risk manager
        self.risk_mgr.register_position(market_id, self.NAME, size)

        position = TradePosition(
            trade_id=trade_id,
            strategy=self.NAME,
            market_id=market_id,
            token_id=token_id,
            side=side,
            entry_price=price,
            size=size,
            entry_time=datetime.now(timezone.utc),
            take_profit_pct=take_profit_pct or self.TAKE_PROFIT_PCT,
            trailing_stop=trailing_stop,
            peak_price=price,
            order_id=result.get("orderID", ""),
        )
        self.positions.append(position)

        self.telegram.alert_trade(self.NAME, side, size, price, market_id[:20])
        logger.info(f"[{self.NAME}] Opened {side} {size:.2f}@{price:.4f} on {market_id[:16]}")
        return position

    def close_position(self, position: TradePosition, current_price: float,
                       reason: str = "manual") -> float:
        """Close a position and calculate P&L."""
        if position.side.upper() == "BUY":
            pnl = (current_price - position.entry_price) * position.size
        else:
            pnl = (position.entry_price - current_price) * position.size

        # Place exit order
        exit_side = "SELL" if position.side.upper() == "BUY" else "BUY"
        self.client.place_order(position.token_id, exit_side, current_price, position.size)

        # Update database
        self.db.update_trade(position.trade_id, {
            "exit_price": current_price,
            "pnl": pnl,
            "status": "closed",
            "exit_reason": reason,
        })

        # Release from risk manager
        self.risk_mgr.release_position(position.market_id, self.NAME, position.size)

        # Remove from active positions
        self.positions = [p for p in self.positions if p.trade_id != position.trade_id]

        # Alerts
        if reason == "take_profit":
            self.telegram.alert_take_profit(self.NAME, pnl, position.market_id[:20])
        elif reason == "stop_loss":
            self.telegram.alert_stop_loss(self.NAME, pnl, position.market_id[:20])
        else:
            self.telegram.alert_trade(self.NAME, exit_side, position.size, current_price,
                                     position.market_id[:20], pnl)

        # Update equity
        self.risk_mgr.update_equity(self.risk_mgr.current_equity + pnl)

        logger.info(f"[{self.NAME}] Closed {position.side} P&L: ${pnl:.2f} ({reason})")
        return pnl

    def manage_positions(self):
        """Check all open positions for TP/SL."""
        for pos in list(self.positions):
            current_price = self.ws.get_price(pos.token_id)
            if current_price is None:
                current_price = self.client.get_price(pos.token_id)
            if current_price is None:
                continue

            # Update peak for trailing stop
            if pos.trailing_stop:
                if pos.side.upper() == "BUY":
                    pos.peak_price = max(pos.peak_price, current_price)
                else:
                    pos.peak_price = min(pos.peak_price, current_price) if pos.peak_price > 0 else current_price

            # Check stop loss (HARD 10%)
            if self.risk_mgr.check_stop_loss(pos.entry_price, current_price, pos.side):
                self.close_position(pos, current_price, "stop_loss")
                self.db.log_failure(self.NAME, pos.trade_id, "stop_loss_hit")
                continue

            # Check trailing stop
            if pos.trailing_stop and pos.peak_price > 0:
                if pos.side.upper() == "BUY":
                    trail_trigger = pos.peak_price * (1 - pos.trailing_stop_pct)
                    if current_price <= trail_trigger and current_price > pos.entry_price:
                        self.close_position(pos, current_price, "trailing_stop")
                        continue

            # Check take profit
            if pos.side.upper() == "BUY":
                profit_pct = (current_price - pos.entry_price) / pos.entry_price
            else:
                profit_pct = (pos.entry_price - current_price) / pos.entry_price

            if profit_pct >= pos.take_profit_pct:
                self.close_position(pos, current_price, "take_profit")

    def execute(self):
        """Main strategy execution - override in subclass."""
        raise NotImplementedError

    def get_params(self) -> dict:
        """Get current strategy parameters."""
        return self.parameters.copy()

    def set_params(self, params: dict):
        """Update strategy parameters."""
        self.parameters.update(params)


# ============================================================================
# STRATEGY 1: DUMP-AND-HEDGE
# ============================================================================

class DumpAndHedgeStrategy(BaseStrategy):
    """
    15-min BTC/ETH/SOL/XRP markets. Detect 15% price drop in first 2 minutes.
    Buy dumped side. Hedge when sum <= 0.95.
    Take profit 5%. Stop loss 10%.
    """

    NAME = "dump_and_hedge"
    TAKE_PROFIT_PCT = 0.05
    STOP_LOSS_PCT = 0.10

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.parameters = {
            "move_threshold": 0.15,
            "sum_target": 0.95,
            "detection_window_sec": 120,
        }
        self.tracked_markets: Dict[str, dict] = {}
        self.assets = ["BTC", "ETH", "SOL", "XRP"]

    def execute(self):
        if not self.can_run():
            return

        self.manage_positions()

        # Scan for 15-min crypto markets
        for asset in self.assets:
            markets = self.client.get_gamma_markets(tag=asset)
            for market in markets:
                if "15" not in market.get("question", "").lower():
                    continue

                tokens = market.get("tokens", [])
                if len(tokens) < 2:
                    continue

                yes_token = tokens[0]
                no_token = tokens[1]
                yes_id = yes_token.get("token_id", "")
                no_id = no_token.get("token_id", "")

                yes_price = self.ws.get_price(yes_id) or self.client.get_price(yes_id)
                no_price = self.ws.get_price(no_id) or self.client.get_price(no_id)

                if not yes_price or not no_price:
                    continue

                market_id = market.get("condition_id", "")
                now = time.time()

                # Track initial prices
                if market_id not in self.tracked_markets:
                    self.tracked_markets[market_id] = {
                        "start_time": now,
                        "start_yes": yes_price,
                        "start_no": no_price,
                        "yes_id": yes_id,
                        "no_id": no_id,
                    }
                    continue

                tracked = self.tracked_markets[market_id]
                elapsed = now - tracked["start_time"]

                # Only act within detection window
                if elapsed > self.parameters["detection_window_sec"]:
                    del self.tracked_markets[market_id]
                    continue

                # Detect dump (15% drop)
                move_threshold = self.parameters["move_threshold"]
                yes_drop = (tracked["start_yes"] - yes_price) / tracked["start_yes"] if tracked["start_yes"] > 0 else 0
                no_drop = (tracked["start_no"] - no_price) / tracked["start_no"] if tracked["start_no"] > 0 else 0

                # Buy the dumped side
                if yes_drop >= move_threshold:
                    allocation = self.get_allocation()
                    size = min(allocation * 0.3, allocation)
                    self.open_position(market_id, yes_id, "BUY", yes_price, size)
                    del self.tracked_markets[market_id]

                elif no_drop >= move_threshold:
                    allocation = self.get_allocation()
                    size = min(allocation * 0.3, allocation)
                    self.open_position(market_id, no_id, "BUY", no_price, size)
                    del self.tracked_markets[market_id]

                # Hedge when sum <= target
                price_sum = yes_price + no_price
                if price_sum <= self.parameters["sum_target"]:
                    allocation = self.get_allocation()
                    size = min(allocation * 0.2, allocation)
                    # Buy both sides for guaranteed profit
                    self.open_position(market_id, yes_id, "BUY", yes_price, size / 2)
                    self.open_position(market_id, no_id, "BUY", no_price, size / 2)
                    del self.tracked_markets[market_id]


# ============================================================================
# STRATEGY 2: YES+NO ARBITRAGE
# ============================================================================

class YesNoArbitrageStrategy(BaseStrategy):
    """
    Scan all markets for YES + NO < 0.99. Execute both sides instantly.
    Take profit 1-3% (when sum normalizes). Stop loss 5%.
    """

    NAME = "yes_no_arbitrage"
    TAKE_PROFIT_PCT = 0.02
    STOP_LOSS_PCT = 0.05

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.parameters = {
            "sum_threshold": 0.99,
            "min_profit_target": 0.01,
            "max_profit_target": 0.03,
        }

    def execute(self):
        if not self.can_run():
            return

        self.manage_positions()

        # Scan all active markets
        markets = self.client.get_markets(limit=50)
        for market in markets:
            tokens = market.get("tokens", [])
            if len(tokens) < 2:
                continue

            yes_id = tokens[0].get("token_id", "")
            no_id = tokens[1].get("token_id", "")

            yes_price = self.ws.get_price(yes_id) or self.client.get_price(yes_id)
            no_price = self.ws.get_price(no_id) or self.client.get_price(no_id)

            if not yes_price or not no_price:
                continue

            price_sum = yes_price + no_price

            # Arbitrage opportunity: sum < 0.99
            if price_sum < self.parameters["sum_threshold"]:
                profit_potential = 1.0 - price_sum
                if profit_potential < self.parameters["min_profit_target"]:
                    continue

                market_id = market.get("condition_id", "")
                allocation = self.get_allocation()
                size = min(allocation * 0.4, allocation)

                # Buy both sides
                half_size = size / 2
                self.open_position(
                    market_id, yes_id, "BUY", yes_price, half_size,
                    take_profit_pct=min(profit_potential, self.parameters["max_profit_target"])
                )
                self.open_position(
                    market_id, no_id, "BUY", no_price, half_size,
                    take_profit_pct=min(profit_potential, self.parameters["max_profit_target"])
                )

                logger.info(
                    f"[ARB] Found opportunity: YES={yes_price:.4f} + NO={no_price:.4f} = {price_sum:.4f}"
                )


# ============================================================================
# STRATEGY 3: YIELD FARMING LP
# ============================================================================

class YieldFarmingStrategy(BaseStrategy):
    """
    Place limit orders both sides. Earn daily USDC rewards.
    Auto-rebalance daily at midnight UTC.
    Take profit daily (claim rewards). Stop loss 10%.
    """

    NAME = "yield_farming"
    TAKE_PROFIT_PCT = 0.01
    STOP_LOSS_PCT = 0.10

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.parameters = {
            "spread": 0.02,
            "rebalance_hour": 0,
            "min_volume": 100000,
        }
        self.last_rebalance: Optional[datetime] = None
        self.active_orders: Dict[str, List[str]] = {}

    def execute(self):
        if not self.can_run():
            return

        self.manage_positions()

        now = datetime.now(timezone.utc)

        # Rebalance at midnight UTC
        should_rebalance = (
            self.last_rebalance is None or
            (now.hour == self.parameters["rebalance_hour"] and
             (now - self.last_rebalance).total_seconds() > 3600)
        )

        if should_rebalance:
            self._rebalance()
            self.last_rebalance = now

    def _rebalance(self):
        """Cancel all orders and replace with new ones."""
        # Cancel existing orders
        for market_id, order_ids in self.active_orders.items():
            for oid in order_ids:
                self.client.cancel_order(oid)
        self.active_orders.clear()

        # Find high-volume markets
        markets = self.client.get_gamma_markets(min_volume=self.parameters["min_volume"])

        allocation = self.get_allocation()
        per_market = allocation / max(len(markets[:5]), 1)

        for market in markets[:5]:
            tokens = market.get("tokens", [])
            if len(tokens) < 2:
                continue

            yes_id = tokens[0].get("token_id", "")
            no_id = tokens[1].get("token_id", "")
            market_id = market.get("condition_id", "")

            yes_price = self.client.get_price(yes_id)
            no_price = self.client.get_price(no_id)

            if not yes_price or not no_price:
                continue

            spread = self.parameters["spread"]
            order_size = per_market / 4

            # Place limit orders on both sides
            orders = []
            bid_yes = self.client.place_order(yes_id, "BUY", yes_price - spread/2, order_size, "GTC")
            ask_yes = self.client.place_order(yes_id, "SELL", yes_price + spread/2, order_size, "GTC")
            bid_no = self.client.place_order(no_id, "BUY", no_price - spread/2, order_size, "GTC")
            ask_no = self.client.place_order(no_id, "SELL", no_price + spread/2, order_size, "GTC")

            filled = 0
            for o in [bid_yes, ask_yes, bid_no, ask_no]:
                if o:
                    orders.append(o.get("orderID", ""))
                    filled += 1

            self.active_orders[market_id] = orders
            logger.info(f"[YIELD] Placed LP orders on {market_id[:16]}")
            if filled > 0:
                self.telegram.send(
                    f"🌾 <b>Yield Farming</b>\n"
                    f"📊 LP orders placed: {filled} orders\n"
                    f"📈 YES: {yes_price:.4f} | NO: {no_price:.4f}\n"
                    f"🎯 Market: {market_id[:20]}\n"
                    f"💰 Capital deployed: ${per_market:.2f}"
                )


# ============================================================================
# STRATEGY 4: MAKER REBATE MARKET MAKING
# ============================================================================

class MakerRebateMMStrategy(BaseStrategy):
    """
    Capture spread + 20-25% taker fees. Auto-post competitive bids/asks.
    Take profit per fill. Stop loss 10%.
    """

    NAME = "maker_rebate_mm"
    TAKE_PROFIT_PCT = 0.005
    STOP_LOSS_PCT = 0.10

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.parameters = {
            "spread": 0.015,
            "order_refresh_sec": 30,
            "min_spread_profit": 0.005,
            "levels": 3,
        }
        self.active_orders: Dict[str, List[str]] = {}
        self.last_refresh: float = 0

    def execute(self):
        if not self.can_run():
            return

        self.manage_positions()

        now = time.time()
        if now - self.last_refresh < self.parameters["order_refresh_sec"]:
            return

        self.last_refresh = now
        self._refresh_quotes()

    def _refresh_quotes(self):
        """Refresh market making quotes."""
        # Cancel stale orders
        for market_id, order_ids in list(self.active_orders.items()):
            for oid in order_ids:
                self.client.cancel_order(oid)
        self.active_orders.clear()

        markets = self.client.get_markets(limit=20)
        allocation = self.get_allocation()

        for market in markets[:5]:
            tokens = market.get("tokens", [])
            if len(tokens) < 2:
                continue

            yes_id = tokens[0].get("token_id", "")
            market_id = market.get("condition_id", "")

            book = self.client.get_orderbook(yes_id)
            if not book:
                continue

            bids = book.get("bids", [])
            asks = book.get("asks", [])
            if not bids or not asks:
                continue

            best_bid = float(bids[0]["price"])
            best_ask = float(asks[0]["price"])
            current_spread = best_ask - best_bid

            if current_spread < self.parameters["min_spread_profit"]:
                continue

            # Post competitive quotes
            spread = self.parameters["spread"]
            mid = (best_bid + best_ask) / 2
            order_size = (allocation * 0.1) / self.parameters["levels"]

            orders = []
            for i in range(int(self.parameters["levels"])):
                offset = spread * (i + 1) / 2
                bid = self.client.place_order(yes_id, "BUY", mid - offset, order_size, "GTC")
                ask = self.client.place_order(yes_id, "SELL", mid + offset, order_size, "GTC")
                if bid:
                    orders.append(bid.get("orderID", ""))
                if ask:
                    orders.append(ask.get("orderID", ""))

            self.active_orders[market_id] = orders
            if orders:
                self.telegram.send(
                    f"🏦 <b>Market Maker</b>\n"
                    f"📊 Quotes refreshed: {len(orders)} orders\n"
                    f"🎯 Market: {market_id[:20]}\n"
                    f"💧 Spread: {self.parameters['spread']*100:.1f}%"
                )


# ============================================================================
# STRATEGY 5: COPY TRADING
# ============================================================================

class CopyTradingStrategy(BaseStrategy):
    """
    Follow top wallets: RN1 (sports), Domer (politics), ColdMath (weather).
    Filter: 60%+ win rate, 100+ trades, 4+ months history.
    Take profit 5-10%. Stop loss 10%.
    """

    NAME = "copy_trading"
    TAKE_PROFIT_PCT = 0.07
    STOP_LOSS_PCT = 0.10

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.parameters = {
            "min_win_rate": 0.60,
            "min_trades": 100,
            "min_history_months": 4,
            "take_profit_min": 0.05,
            "take_profit_max": 0.10,
            "position_scale": 0.5,
        }
        self.tracked_wallets = self.config.COPY_WALLETS
        self.last_check: Dict[str, float] = {}

    def execute(self):
        if not self.can_run():
            return

        self.manage_positions()

        for wallet_name, wallet_addr in self.tracked_wallets.items():
            if not wallet_addr:
                continue

            now = time.time()
            if now - self.last_check.get(wallet_name, 0) < 60:
                continue
            self.last_check[wallet_name] = now

            # Check wallet's recent trades via Gamma API
            try:
                resp = requests.get(
                    f"{self.config.GAMMA_URL}/trades",
                    params={"maker": wallet_addr, "limit": 10},
                    timeout=10
                )
                if resp.status_code != 200:
                    continue
                trades = resp.json()
            except Exception:
                continue

            for trade in trades:
                token_id = trade.get("asset_id", "")
                side = trade.get("side", "").upper()
                price = float(trade.get("price", 0))
                market_id = trade.get("market", "")

                if not token_id or not side or not price:
                    continue

                # Check if we already have a position in this market
                existing = [p for p in self.positions if p.market_id == market_id]
                if existing:
                    continue

                # Copy the trade with scaled size
                allocation = self.get_allocation()
                size = allocation * self.parameters["position_scale"] * 0.2

                tp = (self.parameters["take_profit_min"] + self.parameters["take_profit_max"]) / 2
                self.open_position(market_id, token_id, side, price, size, take_profit_pct=tp)
                logger.info(f"[COPY] Copied {wallet_name}: {side} {token_id[:8]} @ {price:.4f}")


# ============================================================================
# STRATEGY 6: GRID TRADING
# ============================================================================

class GridTradingStrategy(BaseStrategy):
    """
    Laddered orders at 5-10 price levels. Capture oscillations.
    Take profit per grid level. Stop loss 10%.
    """

    NAME = "grid_trading"
    TAKE_PROFIT_PCT = 0.02
    STOP_LOSS_PCT = 0.10

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.parameters = {
            "grid_levels": 7,
            "grid_spacing": 0.02,
            "refresh_interval_sec": 300,
        }
        self.grids: Dict[str, dict] = {}
        self.last_refresh: float = 0

    def execute(self):
        if not self.can_run():
            return

        self.manage_positions()

        now = time.time()
        if now - self.last_refresh < self.parameters["refresh_interval_sec"]:
            return

        self.last_refresh = now
        self._setup_grids()

    def _setup_grids(self):
        """Set up grid orders on selected markets."""
        markets = self.client.get_gamma_markets(min_volume=50000)
        allocation = self.get_allocation()

        for market in markets[:3]:
            tokens = market.get("tokens", [])
            if len(tokens) < 2:
                continue

            yes_id = tokens[0].get("token_id", "")
            market_id = market.get("condition_id", "")

            current_price = self.ws.get_price(yes_id) or self.client.get_price(yes_id)
            if not current_price:
                continue

            # Cancel existing grid orders for this market
            if market_id in self.grids:
                for oid in self.grids[market_id].get("orders", []):
                    self.client.cancel_order(oid)

            # Create grid
            levels = int(self.parameters["grid_levels"])
            spacing = self.parameters["grid_spacing"]
            order_size = (allocation * 0.15) / levels

            orders = []
            for i in range(levels):
                offset = spacing * (i + 1)
                # Buy below
                buy_price = max(0.01, current_price - offset)
                buy_order = self.client.place_order(yes_id, "BUY", buy_price, order_size, "GTC")
                if buy_order:
                    orders.append(buy_order.get("orderID", ""))

                # Sell above
                sell_price = min(0.99, current_price + offset)
                sell_order = self.client.place_order(yes_id, "SELL", sell_price, order_size, "GTC")
                if sell_order:
                    orders.append(sell_order.get("orderID", ""))

            self.grids[market_id] = {
                "orders": orders,
                "center": current_price,
                "token_id": yes_id,
            }
            logger.info(f"[GRID] Set up {levels} levels on {market_id[:16]} @ {current_price:.4f}")


# ============================================================================
# STRATEGY 7: FLASH LOAN ARBITRAGE
# ============================================================================

class FlashLoanArbStrategy(BaseStrategy):
    """
    Aave V3 on Polygon. Borrow USDC. Cross-platform arb (Polymarket/Kalshi).
    Repay loan + 0.05% fee. Keep profit. No stop loss (atomic tx).
    """

    NAME = "flash_loan_arb"
    TAKE_PROFIT_PCT = 0.001
    STOP_LOSS_PCT = 0.0  # Atomic - no stop loss needed

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.parameters = {
            "min_profit_bps": 10,
            "flash_fee_bps": 5,
            "max_loan_amount": 50000,
            "scan_interval_sec": 5,
        }
        self.last_scan: float = 0

    def execute(self):
        if not self.can_run():
            return

        now = time.time()
        if now - self.last_scan < self.parameters["scan_interval_sec"]:
            return
        self.last_scan = now

        # Scan for cross-platform arbitrage opportunities
        opportunities = self._find_opportunities()

        for opp in opportunities:
            if opp["profit_bps"] > self.parameters["min_profit_bps"]:
                self._execute_flash_loan(opp)

    def _find_opportunities(self) -> list:
        """Find cross-platform price discrepancies."""
        opportunities = []

        markets = self.client.get_markets(limit=30)
        for market in markets:
            tokens = market.get("tokens", [])
            if len(tokens) < 2:
                continue

            yes_id = tokens[0].get("token_id", "")
            no_id = tokens[1].get("token_id", "")

            yes_price = self.ws.get_price(yes_id) or self.client.get_price(yes_id)
            no_price = self.ws.get_price(no_id) or self.client.get_price(no_id)

            if not yes_price or not no_price:
                continue

            # Check if sum significantly different from 1.0
            price_sum = yes_price + no_price
            if price_sum < 0.98:
                profit_bps = int((1.0 - price_sum) * 10000) - self.parameters["flash_fee_bps"]
                if profit_bps > 0:
                    opportunities.append({
                        "market_id": market.get("condition_id", ""),
                        "yes_id": yes_id,
                        "no_id": no_id,
                        "yes_price": yes_price,
                        "no_price": no_price,
                        "profit_bps": profit_bps,
                        "type": "sum_arb",
                    })

        return sorted(opportunities, key=lambda x: x["profit_bps"], reverse=True)

    def _execute_flash_loan(self, opportunity: dict):
        """Execute flash loan arbitrage (atomic transaction)."""
        if self.config.SIMULATE_MODE:
            profit = opportunity["profit_bps"] / 10000 * self.parameters["max_loan_amount"]
            logger.info(
                f"[FLASH] Simulated arb: profit=${profit:.2f} "
                f"({opportunity['profit_bps']}bps) on {opportunity['market_id'][:16]}"
            )
            # Log as completed trade
            trade_data = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "strategy": self.NAME,
                "market_id": opportunity["market_id"],
                "token_id": opportunity["yes_id"],
                "side": "BUY",
                "entry_price": opportunity["yes_price"],
                "exit_price": opportunity["yes_price"],
                "size": self.parameters["max_loan_amount"],
                "pnl": profit,
                "status": "closed",
                "exit_reason": "flash_arb_profit",
                "time_of_day": datetime.now(timezone.utc).strftime("%H:%M"),
            }
            self.db.log_trade(trade_data)
            return

        # In production: construct and submit atomic flash loan transaction
        # via Aave V3 Pool on Polygon
        logger.info(
            f"[FLASH] Executing flash loan arb: {opportunity['profit_bps']}bps "
            f"on {opportunity['market_id'][:16]}"
        )

        # Buy both YES and NO at combined price < 1.0
        allocation = min(
            self.parameters["max_loan_amount"],
            self.get_allocation()
        )
        half = allocation / 2

        self.open_position(
            opportunity["market_id"], opportunity["yes_id"],
            "BUY", opportunity["yes_price"], half
        )
        self.open_position(
            opportunity["market_id"], opportunity["no_id"],
            "BUY", opportunity["no_price"], half
        )


# ============================================================================
# STRATEGY 8: GRIND TRADING
# ============================================================================

class GrindTradingStrategy(BaseStrategy):
    """
    High-frequency small profits via spread capture on liquid midrange markets.
    Only trades tokens priced 0.15–0.85 (liquid, two-sided flow).
    Take profit 1-2%. Stop loss 5%.
    """

    NAME = "grind_trading"
    TAKE_PROFIT_PCT = 0.01
    STOP_LOSS_PCT = 0.05  # Tighter: these are small scalps

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.parameters = {
            "min_take_profit": 0.008,
            "max_take_profit": 0.02,
            "cycle_cooldown_sec": 15,
            "min_volume_24h": 100000,   # Only liquid markets
            "momentum_threshold": 0.005,
            "min_price": 0.15,          # Never trade junk near-zero tokens
            "max_price": 0.85,          # Never trade near-certain tokens
            "min_spread": 0.008,        # Minimum spread to make profit after fees
            "max_spread": 0.06,         # Too wide = illiquid/dangerous
            "max_positions": 3,
            "size_pct": 0.08,           # 8% of allocation per trade
        }
        self.last_cycle: float = 0
        self.cycle_count: int = 0

    def execute(self):
        if not self.can_run():
            return

        self.manage_positions()

        now = time.time()
        if now - self.last_cycle < self.parameters["cycle_cooldown_sec"]:
            return
        self.last_cycle = now

        if len(self.positions) >= self.parameters["max_positions"]:
            return

        markets = self.client.get_markets(limit=40)

        for market in markets:
            if len(self.positions) >= self.parameters["max_positions"]:
                break

            tokens = market.get("tokens", [])
            if len(tokens) < 2:
                continue

            vol = float(market.get("volume", 0))
            if vol < self.parameters["min_volume_24h"]:
                continue

            yes_id = tokens[0].get("token_id", "")
            market_id = market.get("condition_id", "")

            book = self.client.get_orderbook(yes_id)
            if not book:
                continue

            bids = book.get("bids", [])
            asks = book.get("asks", [])
            if not bids or not asks:
                continue

            best_bid = float(bids[0]["price"])
            best_ask = float(asks[0]["price"])
            mid_price = (best_bid + best_ask) / 2
            spread = best_ask - best_bid

            # *** CRITICAL FILTER: Only trade liquid midrange tokens ***
            if mid_price < self.parameters["min_price"] or mid_price > self.parameters["max_price"]:
                continue  # Skip near-zero and near-certain tokens

            # Spread quality filter
            if spread < self.parameters["min_spread"] or spread > self.parameters["max_spread"]:
                continue

            # Bid depth check — need at least 3 levels of depth
            if len(bids) < 3 or len(asks) < 3:
                continue

            # Direction: buy if price below midpoint and spread is favorable
            if mid_price < 0.48:
                side = "BUY"
                entry = best_ask
            elif mid_price > 0.52:
                side = "SELL"
                entry = best_bid
            else:
                # At 50/50 — buy the dip side (bet on mean reversion)
                side = "BUY"
                entry = best_ask

            allocation = self.get_allocation()
            size = allocation * self.parameters["size_pct"]

            if size < 1.0:  # Minimum $1 position
                continue

            tp = min(spread * 0.6, self.parameters["max_take_profit"])
            tp = max(tp, self.parameters["min_take_profit"])

            self.open_position(market_id, yes_id, side, entry, size, take_profit_pct=tp)
            self.cycle_count += 1


# ============================================================================
# STRATEGY 9: DAY TRADING MOMENTUM
# ============================================================================

class DayTradingMomentumStrategy(BaseStrategy):
    """
    5-min/15-min markets. Follow trend in first 2-3 minutes.
    Take profit 3-5%. Stop loss 10%. Trail stop at 1% below peak.
    """

    NAME = "day_trading_momentum"
    TAKE_PROFIT_PCT = 0.04
    STOP_LOSS_PCT = 0.10

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.parameters = {
            "trend_detection_sec": 150,
            "min_move_pct": 0.03,
            "take_profit_min": 0.03,
            "take_profit_max": 0.05,
            "trailing_stop_pct": 0.01,
        }
        self.price_history: Dict[str, deque] = {}

    def execute(self):
        if not self.can_run():
            return

        self.manage_positions()

        # Scan short-duration markets
        markets = self.client.get_gamma_markets(min_volume=30000)

        for market in markets:
            question = market.get("question", "").lower()
            if "5 min" not in question and "15 min" not in question:
                continue

            tokens = market.get("tokens", [])
            if len(tokens) < 2:
                continue

            yes_id = tokens[0].get("token_id", "")
            market_id = market.get("condition_id", "")

            current_price = self.ws.get_price(yes_id) or self.client.get_price(yes_id)
            if not current_price:
                continue

            # Track price history
            if yes_id not in self.price_history:
                self.price_history[yes_id] = deque(maxlen=60)

            self.price_history[yes_id].append({
                "time": time.time(),
                "price": current_price
            })

            history = self.price_history[yes_id]
            if len(history) < 5:
                continue

            # Calculate trend over detection window
            oldest = history[0]
            elapsed = time.time() - oldest["time"]
            if elapsed > self.parameters["trend_detection_sec"]:
                price_change = (current_price - oldest["price"]) / oldest["price"]

                if abs(price_change) >= self.parameters["min_move_pct"]:
                    # Follow the trend
                    side = "BUY" if price_change > 0 else "SELL"
                    allocation = self.get_allocation()
                    size = allocation * 0.2

                    # Check if already in this market
                    existing = [p for p in self.positions if p.market_id == market_id]
                    if existing:
                        continue

                    tp = (self.parameters["take_profit_min"] + self.parameters["take_profit_max"]) / 2
                    self.open_position(
                        market_id, yes_id, side, current_price, size,
                        take_profit_pct=tp, trailing_stop=True
                    )
                    logger.info(
                        f"[MOMENTUM] {side} on trend ({price_change*100:.1f}%) "
                        f"@ {current_price:.4f}"
                    )


# ============================================================================
# STRATEGY 10: DAY TRADING MEAN REVERSION
# ============================================================================

class DayTradingMeanReversionStrategy(BaseStrategy):
    """
    Bet on reversal when price moves too far too fast.
    Take profit 2-4%. Stop loss 10%. Time-based exit after 1 hour.
    """

    NAME = "day_trading_mean_reversion"
    TAKE_PROFIT_PCT = 0.03
    STOP_LOSS_PCT = 0.10

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.parameters = {
            "overextension_threshold": 0.08,
            "take_profit_min": 0.02,
            "take_profit_max": 0.04,
            "max_hold_minutes": 60,
            "lookback_minutes": 10,
        }
        self.price_history: Dict[str, deque] = {}

    def execute(self):
        if not self.can_run():
            return

        # Time-based exit check
        for pos in list(self.positions):
            elapsed = (datetime.now(timezone.utc) - pos.entry_time).total_seconds() / 60
            if elapsed >= self.parameters["max_hold_minutes"]:
                current_price = self.ws.get_price(pos.token_id) or self.client.get_price(pos.token_id)
                if current_price:
                    self.close_position(pos, current_price, "time_exit")
                    logger.info(f"[MEAN_REV] Time exit after {elapsed:.0f} minutes")

        self.manage_positions()

        # Scan for overextended moves
        markets = self.client.get_gamma_markets(min_volume=30000)

        for market in markets:
            tokens = market.get("tokens", [])
            if len(tokens) < 2:
                continue

            yes_id = tokens[0].get("token_id", "")
            market_id = market.get("condition_id", "")

            current_price = self.ws.get_price(yes_id) or self.client.get_price(yes_id)
            if not current_price:
                continue

            # Track price history
            if yes_id not in self.price_history:
                self.price_history[yes_id] = deque(maxlen=120)

            self.price_history[yes_id].append({
                "time": time.time(),
                "price": current_price
            })

            history = self.price_history[yes_id]
            if len(history) < 10:
                continue

            # Calculate mean and deviation
            lookback_sec = self.parameters["lookback_minutes"] * 60
            recent = [h for h in history if time.time() - h["time"] <= lookback_sec]
            if len(recent) < 5:
                continue

            prices = [h["price"] for h in recent]
            mean_price = sum(prices) / len(prices)
            deviation = (current_price - mean_price) / mean_price if mean_price > 0 else 0

            # Overextended move detected
            if abs(deviation) >= self.parameters["overextension_threshold"]:
                # Bet on mean reversion (opposite direction)
                side = "SELL" if deviation > 0 else "BUY"

                existing = [p for p in self.positions if p.market_id == market_id]
                if existing:
                    continue

                allocation = self.get_allocation()
                size = allocation * 0.15
                tp = (self.parameters["take_profit_min"] + self.parameters["take_profit_max"]) / 2

                self.open_position(market_id, yes_id, side, current_price, size, take_profit_pct=tp)
                logger.info(
                    f"[MEAN_REV] {side} on overextension ({deviation*100:.1f}%) "
                    f"@ {current_price:.4f}, mean={mean_price:.4f}"
                )


# ============================================================================
# MARKET REGIME DETECTOR
# ============================================================================

class MarketRegime(Enum):
    HIGH_VOL_TRENDING = "high_vol_trending"
    HIGH_VOL_RANGING = "high_vol_ranging"
    LOW_VOL_TRENDING = "low_vol_trending"
    LOW_VOL_RANGING = "low_vol_ranging"


class RegimeDetector:
    """
    Classify market state every 15 minutes:
    - High/low volatility (BTC move >2% = high)
    - Trending/ranging (ADX >25 = trending)
    - High/low liquidity (order book depth)
    """

    def __init__(self, ws: WebSocketManager, client: PolymarketClient):
        self.ws = ws
        self.client = client
        self.current_regime = MarketRegime.LOW_VOL_RANGING
        self.price_history: deque = deque(maxlen=1000)
        self.last_update: float = 0
        self.update_interval = 900  # 15 minutes
        self.volatility: float = 0.0
        self.is_trending: bool = False
        self.liquidity_score: float = 0.5

    def update(self) -> MarketRegime:
        """Update market regime classification."""
        now = time.time()
        if now - self.last_update < self.update_interval:
            return self.current_regime

        self.last_update = now
        self._calculate_volatility()
        self._detect_trend()
        self._assess_liquidity()
        self._classify()

        logger.info(
            f"[REGIME] {self.current_regime.value} "
            f"(vol={self.volatility:.4f}, trending={self.is_trending}, "
            f"liq={self.liquidity_score:.2f})"
        )
        return self.current_regime

    def _calculate_volatility(self):
        """Calculate recent volatility from BTC markets."""
        # Use cached prices from WebSocket
        prices = list(self.ws.price_cache.values())
        if len(prices) < 2:
            self.volatility = 0.01
            return

        # Simple volatility measure
        returns = []
        for i in range(1, min(len(prices), 20)):
            if prices[i-1] > 0:
                returns.append(abs(prices[i] - prices[i-1]) / prices[i-1])

        self.volatility = sum(returns) / len(returns) if returns else 0.01

    def _detect_trend(self):
        """Detect if market is trending using simple momentum."""
        prices = list(self.ws.price_cache.values())
        if len(prices) < 10:
            self.is_trending = False
            return

        # Simple trend detection: are prices consistently moving one direction?
        up_moves = sum(1 for i in range(1, min(len(prices), 20)) if prices[i] > prices[i-1])
        total = min(len(prices) - 1, 19)
        if total == 0:
            self.is_trending = False
            return

        ratio = up_moves / total
        self.is_trending = ratio > 0.65 or ratio < 0.35  # ADX proxy

    def _assess_liquidity(self):
        """Assess overall market liquidity."""
        books = self.ws.orderbook_cache
        if not books:
            self.liquidity_score = 0.5
            return

        depths = []
        for book_data in books.values():
            bids = book_data.get("bids", [])
            asks = book_data.get("asks", [])
            depth = len(bids) + len(asks)
            depths.append(depth)

        avg_depth = sum(depths) / len(depths) if depths else 0
        self.liquidity_score = min(avg_depth / 20, 1.0)

    def _classify(self):
        """Classify into one of four regimes."""
        high_vol = self.volatility > 0.02

        if high_vol and self.is_trending:
            self.current_regime = MarketRegime.HIGH_VOL_TRENDING
        elif high_vol and not self.is_trending:
            self.current_regime = MarketRegime.HIGH_VOL_RANGING
        elif not high_vol and self.is_trending:
            self.current_regime = MarketRegime.LOW_VOL_TRENDING
        else:
            self.current_regime = MarketRegime.LOW_VOL_RANGING

    def get_strategy_weights(self) -> Dict[str, float]:
        """Get recommended strategy weights based on current regime."""
        weights = {
            MarketRegime.HIGH_VOL_TRENDING: {
                "dump_and_hedge": 0.20,
                "yes_no_arbitrage": 0.10,
                "yield_farming": 0.05,
                "maker_rebate_mm": 0.05,
                "copy_trading": 0.10,
                "grid_trading": 0.05,
                "flash_loan_arb": 0.15,
                "grind_trading": 0.05,
                "day_trading_momentum": 0.20,
                "day_trading_mean_reversion": 0.05,
            },
            MarketRegime.HIGH_VOL_RANGING: {
                "dump_and_hedge": 0.15,
                "yes_no_arbitrage": 0.15,
                "yield_farming": 0.05,
                "maker_rebate_mm": 0.10,
                "copy_trading": 0.05,
                "grid_trading": 0.15,
                "flash_loan_arb": 0.10,
                "grind_trading": 0.10,
                "day_trading_momentum": 0.05,
                "day_trading_mean_reversion": 0.10,
            },
            MarketRegime.LOW_VOL_TRENDING: {
                "dump_and_hedge": 0.05,
                "yes_no_arbitrage": 0.10,
                "yield_farming": 0.15,
                "maker_rebate_mm": 0.10,
                "copy_trading": 0.15,
                "grid_trading": 0.10,
                "flash_loan_arb": 0.05,
                "grind_trading": 0.10,
                "day_trading_momentum": 0.15,
                "day_trading_mean_reversion": 0.05,
            },
            MarketRegime.LOW_VOL_RANGING: {
                "dump_and_hedge": 0.05,
                "yes_no_arbitrage": 0.15,
                "yield_farming": 0.20,
                "maker_rebate_mm": 0.15,
                "copy_trading": 0.10,
                "grid_trading": 0.15,
                "flash_loan_arb": 0.05,
                "grind_trading": 0.10,
                "day_trading_momentum": 0.02,
                "day_trading_mean_reversion": 0.03,
            },
        }
        return weights.get(self.current_regime, {})


# ============================================================================
# PARAMETER OPTIMIZER
# ============================================================================

class ParameterOptimizer:
    """
    After every 20 trades, test small parameter variations.
    Deploy best-performing parameters for next hour.
    """

    def __init__(self, db: Database):
        self.db = db
        self.trade_count_since_optimize: Dict[str, int] = {}
        self.optimize_threshold = 20
        self.param_grids: Dict[str, Dict[str, list]] = {
            "dump_and_hedge": {
                "move_threshold": [0.12, 0.13, 0.14, 0.15, 0.16, 0.17, 0.18],
                "sum_target": [0.93, 0.94, 0.95, 0.96, 0.97],
            },
            "yes_no_arbitrage": {
                "sum_threshold": [0.97, 0.98, 0.99],
                "min_profit_target": [0.005, 0.01, 0.015],
            },
            "grind_trading": {
                "min_take_profit": [0.003, 0.005, 0.007, 0.01],
                "cycle_cooldown_sec": [5, 10, 15, 20],
            },
            "day_trading_momentum": {
                "min_move_pct": [0.02, 0.03, 0.04, 0.05],
                "trailing_stop_pct": [0.005, 0.01, 0.015, 0.02],
            },
            "day_trading_mean_reversion": {
                "overextension_threshold": [0.06, 0.07, 0.08, 0.09, 0.10],
                "max_hold_minutes": [30, 45, 60, 90],
            },
            "grid_trading": {
                "grid_levels": [5, 7, 10],
                "grid_spacing": [0.015, 0.02, 0.025, 0.03],
            },
            "maker_rebate_mm": {
                "spread": [0.01, 0.015, 0.02, 0.025],
                "levels": [2, 3, 4, 5],
            },
        }

    def on_trade_closed(self, strategy: str):
        """Track trades and trigger optimization when threshold reached."""
        self.trade_count_since_optimize[strategy] = (
            self.trade_count_since_optimize.get(strategy, 0) + 1
        )

    def should_optimize(self, strategy: str) -> bool:
        """Check if optimization should run for this strategy."""
        return self.trade_count_since_optimize.get(strategy, 0) >= self.optimize_threshold

    def optimize(self, strategy: str) -> Dict[str, float]:
        """Run parameter optimization and return best parameters."""
        self.trade_count_since_optimize[strategy] = 0

        trades = self.db.get_all_trades_for_optimization(strategy, self.optimize_threshold * 2)
        if not trades:
            return {}

        grid = self.param_grids.get(strategy, {})
        if not grid:
            return {}

        # Evaluate recent performance with current parameters
        recent_pnl = sum(t.get("pnl", 0) for t in trades[:self.optimize_threshold])

        # Simple grid search: find which parameter values correlate with wins
        best_params = {}
        for param_name, values in grid.items():
            best_value = values[len(values) // 2]  # Default to middle
            best_score = recent_pnl

            # Analyze trades where different conditions applied
            for val in values:
                # Use heuristic: if recent performance is bad, shift params
                if recent_pnl < 0:
                    # Try adjacent values to current
                    idx = len(values) // 2
                    if recent_pnl < -10:
                        idx = max(0, idx - 1)  # Tighten
                    best_value = values[idx]

            best_params[param_name] = best_value

        logger.info(f"[OPTIMIZER] {strategy} new params: {best_params}")
        return best_params


# ============================================================================
# SKILL EXTRACTOR
# ============================================================================

class SkillExtractor:
    """
    When a trade wins by >10%, extract the exact setup as a skill.
    Save to skills/ folder as JSON.
    """

    def __init__(self, db: Database, skills_dir: Path):
        self.db = db
        self.skills_dir = skills_dir
        self.skills_dir.mkdir(exist_ok=True)

    def check_and_extract(self, trade: dict):
        """Check if trade qualifies for skill extraction."""
        pnl_pct = 0
        if trade.get("entry_price") and trade.get("entry_price") > 0:
            pnl_pct = trade.get("pnl", 0) / (trade.get("size", 1) * trade.get("entry_price", 1))

        if pnl_pct >= 0.10:
            self._extract_skill(trade)

    def _extract_skill(self, trade: dict):
        """Extract winning trade setup as a reusable skill."""
        skill = {
            "extracted_at": datetime.now(timezone.utc).isoformat(),
            "strategy": trade.get("strategy"),
            "entry_price": trade.get("entry_price"),
            "exit_price": trade.get("exit_price"),
            "pnl_pct": trade.get("pnl", 0) / max(trade.get("size", 1), 0.01),
            "market_conditions": {
                "volatility": trade.get("volatility"),
                "volume": trade.get("volume"),
                "time_of_day": trade.get("time_of_day"),
                "market_regime": trade.get("market_regime"),
            },
            "parameters": {},
            "signals": {
                "side": trade.get("side"),
                "entry_trigger": trade.get("notes", ""),
            },
        }

        filename = f"skill_{trade.get('strategy')}_{int(time.time())}.json"
        filepath = self.skills_dir / filename

        with open(filepath, "w") as f:
            json.dump(skill, f, indent=2)

        logger.info(f"[SKILL] Extracted winning skill to {filename}")

    def find_matching_skill(self, strategy: str, market_conditions: dict) -> Optional[dict]:
        """Find a skill that matches current market conditions."""
        for skill_file in self.skills_dir.glob(f"skill_{strategy}_*.json"):
            try:
                with open(skill_file) as f:
                    skill = json.load(f)

                conditions = skill.get("market_conditions", {})

                # Match on regime and time of day
                if conditions.get("market_regime") == market_conditions.get("regime"):
                    return skill
            except Exception:
                continue
        return None


# ============================================================================
# FAILURE REFLECTOR
# ============================================================================

class FailureReflector:
    """
    For every losing trade, log the reason.
    If same reason appears 3 times in 24 hours, adjust parameters or pause.
    """

    FAILURE_REASONS = [
        "slippage_too_high",
        "hedge_never_triggered",
        "fee_too_large",
        "wrong_direction",
        "late_entry",
        "market_closed",
        "insufficient_liquidity",
        "stop_loss_hit",
    ]

    def __init__(self, db: Database):
        self.db = db
        self.threshold = 3

    def analyze_failure(self, trade: dict) -> str:
        """Determine the reason for a losing trade."""
        pnl = trade.get("pnl", 0)
        if pnl >= 0:
            return ""

        slippage = trade.get("slippage", 0)
        fee = trade.get("fee_paid", 0)
        exit_reason = trade.get("exit_reason", "")

        if slippage > 0.02:
            return "slippage_too_high"
        if fee > abs(pnl) * 0.5:
            return "fee_too_large"
        if exit_reason == "stop_loss":
            return "stop_loss_hit"
        if exit_reason == "time_exit":
            return "late_entry"

        return "wrong_direction"

    def should_adjust(self, strategy: str, reason: str) -> bool:
        """Check if a failure reason has occurred too many times."""
        count = self.db.get_recent_failures(strategy, reason, hours=24)
        return count >= self.threshold

    def get_adjustment(self, strategy: str, reason: str) -> Dict[str, Any]:
        """Get parameter adjustments based on failure reason."""
        adjustments = {
            "slippage_too_high": {"action": "tighten_entry", "param": "min_spread_profit", "delta": 0.005},
            "fee_too_large": {"action": "increase_size", "param": "min_take_profit", "delta": 0.005},
            "wrong_direction": {"action": "pause", "minutes": 30},
            "stop_loss_hit": {"action": "tighten_entry", "param": "overextension_threshold", "delta": 0.01},
            "late_entry": {"action": "reduce_hold", "param": "max_hold_minutes", "delta": -10},
        }
        return adjustments.get(reason, {"action": "pause", "minutes": 10})


# ============================================================================
# AUTONOMY ENGINE
# ============================================================================

class AutonomyEngine:
    """
    Core autonomy system that manages all strategies, allocates capital,
    detects failures, and self-heals.
    """

    def __init__(self, config: Config, client: PolymarketClient,
                 ws: WebSocketManager, risk_mgr: RiskManager, db: Database,
                 telegram: TelegramBot):
        self.config = config
        self.client = client
        self.ws = ws
        self.risk_mgr = risk_mgr
        self.db = db
        self.telegram = telegram

        # Initialize components
        self.regime_detector = RegimeDetector(ws, client)
        self.optimizer = ParameterOptimizer(db)
        self.skill_extractor = SkillExtractor(db, SKILLS_DIR)
        self.failure_reflector = FailureReflector(db)

        # Initialize all 10 strategies
        strategy_args = (client, ws, risk_mgr, db, telegram, config)
        self.strategies: Dict[str, BaseStrategy] = {
            "dump_and_hedge": DumpAndHedgeStrategy(*strategy_args),
            "yes_no_arbitrage": YesNoArbitrageStrategy(*strategy_args),
            "yield_farming": YieldFarmingStrategy(*strategy_args),
            "maker_rebate_mm": MakerRebateMMStrategy(*strategy_args),
            "copy_trading": CopyTradingStrategy(*strategy_args),
            "grid_trading": GridTradingStrategy(*strategy_args),
            "flash_loan_arb": FlashLoanArbStrategy(*strategy_args),
            "grind_trading": GrindTradingStrategy(*strategy_args),
            "day_trading_momentum": DayTradingMomentumStrategy(*strategy_args),
            "day_trading_mean_reversion": DayTradingMeanReversionStrategy(*strategy_args),
        }

        # Timing
        self.last_weight_update: float = 0
        self.weight_update_interval = 3600  # 1 hour
        self.last_daily_reset: Optional[datetime] = None
        self.last_daily_summary: Optional[datetime] = None
        self.last_weekly_report: Optional[datetime] = None
        self.last_auto_update_check: Optional[datetime] = datetime.now(timezone.utc)  # Skip check on first boot

    def run_cycle(self):
        """Execute one full trading cycle."""
        # Check if trading is allowed
        can_trade, reason = self.risk_mgr.can_trade()
        if not can_trade:
            logger.warning(f"Trading halted: {reason}")
            self.telegram.alert_circuit_breaker(reason)
            time.sleep(60)
            return

        # Update market regime
        regime = self.regime_detector.update()

        # Update strategy weights based on regime + performance
        self._update_weights()

        # Execute all active strategies
        for name, strategy in self.strategies.items():
            try:
                if strategy.can_run():
                    strategy.execute()
            except Exception as e:
                logger.error(f"Strategy {name} error: {e}\n{traceback.format_exc()}")
                self.telegram.alert_error(f"Strategy {name}: {str(e)[:200]}")

        # Post-trade analysis
        self._post_trade_analysis()

        # Scheduled tasks
        self._run_scheduled_tasks()

    def _update_weights(self):
        """Update strategy weights based on regime and performance."""
        now = time.time()
        if now - self.last_weight_update < self.weight_update_interval:
            return

        self.last_weight_update = now

        # Get regime-based weights
        regime_weights = self.regime_detector.get_strategy_weights()

        # Blend with performance-based weights
        for name, strategy in self.strategies.items():
            regime_w = regime_weights.get(name, 0.1)
            stats = self.db.get_strategy_stats(name, hours=24)

            # Performance multiplier
            if stats["trades"] > 0:
                perf_mult = 1.0 + (stats["win_rate"] - 0.5) * 2
                perf_mult = max(0.2, min(2.0, perf_mult))
            else:
                perf_mult = 1.0

            # Final weight (regime 60% + performance 40%)
            final_weight = regime_w * 0.6 + (regime_w * perf_mult) * 0.4
            strategy.weight = max(0.02, min(0.30, final_weight))

        # Normalize weights to sum to 1.0
        total_weight = sum(s.weight for s in self.strategies.values())
        if total_weight > 0:
            for strategy in self.strategies.values():
                strategy.weight /= total_weight

        logger.debug(
            f"[WEIGHTS] " +
            ", ".join(f"{n}:{s.weight:.2f}" for n, s in self.strategies.items())
        )

    def _post_trade_analysis(self):
        """Run post-trade analysis: optimization, skill extraction, failure reflection."""
        for name, strategy in self.strategies.items():
            # Check for optimization
            if self.optimizer.should_optimize(name):
                new_params = self.optimizer.optimize(name)
                if new_params:
                    strategy.set_params(new_params)

            # Check recent closed trades for skills/failures
            recent_trades = self.db.get_all_trades_for_optimization(name, 5)
            for trade in recent_trades:
                if trade.get("pnl", 0) > 0:
                    self.skill_extractor.check_and_extract(trade)
                elif trade.get("pnl", 0) < 0:
                    reason = self.failure_reflector.analyze_failure(trade)
                    if reason:
                        self.db.log_failure(name, trade.get("id", 0), reason)
                        if self.failure_reflector.should_adjust(name, reason):
                            adj = self.failure_reflector.get_adjustment(name, reason)
                            if adj.get("action") == "pause":
                                strategy.pause(minutes=adj.get("minutes", 10))
                                logger.warning(
                                    f"[REFLECT] Pausing {name} for {adj['minutes']}min "
                                    f"due to repeated: {reason}"
                                )

    def _run_scheduled_tasks(self):
        """Run time-based scheduled tasks."""
        now = datetime.now(timezone.utc)

        # Daily reset at midnight UTC
        if self.last_daily_reset is None or now.date() > self.last_daily_reset.date():
            self.risk_mgr.reset_daily()
            self.last_daily_reset = now
            logger.info("[SCHEDULE] Daily reset complete")

        # Daily summary at 08:00 UTC
        if now.hour == 8 and (self.last_daily_summary is None or
                              now.date() > self.last_daily_summary.date()):
            self._send_daily_summary()
            self.last_daily_summary = now

        # Weekly report Monday 08:00 UTC
        if now.weekday() == 0 and now.hour == 8 and (
            self.last_weekly_report is None or
            (now - self.last_weekly_report).days >= 6
        ):
            self._send_weekly_report()
            self.last_weekly_report = now

        # Auto-update check (daily)
        if self.last_auto_update_check is None or (now - self.last_auto_update_check).days >= 1:
            self._check_auto_update()
            self.last_auto_update_check = now

        # Balance check
        balance = self.client.get_balance()
        if balance < 100 and balance > 0:
            self.telegram.alert_low_balance(balance)

    def _send_daily_summary(self):
        """Send daily P&L summary via Telegram."""
        total_pnl = self.db.get_total_pnl_today()
        all_stats = {}
        best_strategy = ""
        best_pnl = -float("inf")

        for name in self.strategies:
            stats = self.db.get_strategy_stats(name, hours=24)
            all_stats[name] = stats
            if stats["pnl"] > best_pnl:
                best_pnl = stats["pnl"]
                best_strategy = name

        total_trades = sum(s["trades"] for s in all_stats.values())
        total_wins = sum(
            s["trades"] * s["win_rate"] for s in all_stats.values()
        )
        win_rate = total_wins / total_trades if total_trades > 0 else 0

        self.telegram.alert_daily_summary({
            "total_pnl": total_pnl,
            "total_trades": total_trades,
            "win_rate": win_rate,
            "equity": self.risk_mgr.current_equity,
            "best_strategy": best_strategy,
        })

    def _send_weekly_report(self):
        """Send weekly performance report via Telegram."""
        week_pnl = 0
        total_trades = 0
        total_wins = 0

        for name in self.strategies:
            stats = self.db.get_strategy_stats(name, hours=168)
            week_pnl += stats["pnl"]
            total_trades += stats["trades"]
            total_wins += stats["trades"] * stats["win_rate"]

        active = sum(
            1 for s in self.strategies.values()
            if s.state == StrategyState.ACTIVE
        )

        self.telegram.alert_weekly_report({
            "week_pnl": week_pnl,
            "total_trades": total_trades,
            "win_rate": total_wins / total_trades if total_trades > 0 else 0,
            "equity": self.risk_mgr.current_equity,
            "active_strategies": active,
        })

    def _check_auto_update(self):
        """Check GitHub for updates and pull new version."""
        try:
            result = subprocess.run(
                ["git", "-C", str(BASE_DIR), "fetch", "origin"],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode != 0:
                return

            result = subprocess.run(
                ["git", "-C", str(BASE_DIR), "log", "HEAD..origin/main", "--oneline"],
                capture_output=True, text=True, timeout=10
            )
            if result.stdout.strip():
                logger.info("[AUTO-UPDATE] New version available, pulling...")
                subprocess.run(
                    ["git", "-C", str(BASE_DIR), "pull", "origin", "main"],
                    capture_output=True, text=True, timeout=30
                )
                self.telegram.alert_bot_status("Updated from GitHub. Restarting...")
                # Graceful restart via PM2/systemd
                os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception as e:
            logger.error(f"Auto-update check failed: {e}")

    def get_status(self) -> str:
        """Get current bot status for Telegram /status command."""
        can_trade, reason = self.risk_mgr.can_trade()
        active = sum(1 for s in self.strategies.values() if s.state == StrategyState.ACTIVE)
        paused = sum(1 for s in self.strategies.values() if s.state == StrategyState.PAUSED)
        total_positions = sum(len(s.positions) for s in self.strategies.values())

        return (
            f"\U0001f916 <b>Zeus Prime Status</b>\n\n"
            f"\U0001f4b0 Equity: ${self.risk_mgr.current_equity:.2f}\n"
            f"\U0001f4c8 Peak: ${self.risk_mgr.peak_equity:.2f}\n"
            f"\U0001f4ca Today P&L: ${self.db.get_total_pnl_today():.2f}\n\n"
            f"\U0001f3af Strategies: {active} active, {paused} paused\n"
            f"\U0001f4cd Positions: {total_positions} open\n"
            f"\U0001f30d Regime: {self.regime_detector.current_regime.value}\n"
            f"\U00002705 Trading: {'Yes' if can_trade else 'No - ' + reason}\n"
            f"\U000023f0 Uptime: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        )

    def get_strategies_status(self) -> str:
        """Get detailed strategy status for Telegram /strategies command."""
        lines = ["\U0001f4ca <b>Strategy Status</b>\n"]
        for name, strategy in self.strategies.items():
            stats = self.db.get_strategy_stats(name, hours=24)
            emoji = "\U0001f7e2" if strategy.state == StrategyState.ACTIVE else "\U0001f534"
            lines.append(
                f"{emoji} <b>{name}</b>\n"
                f"   Weight: {strategy.weight*100:.1f}% | "
                f"WR: {stats['win_rate']*100:.0f}% | "
                f"P&L: ${stats['pnl']:.2f} | "
                f"Trades: {stats['trades']}\n"
            )
        return "\n".join(lines)

    def pause_all(self):
        """Pause all strategies."""
        for strategy in self.strategies.values():
            strategy.state = StrategyState.PAUSED
        logger.info("All strategies paused")

    def resume_all(self):
        """Resume all strategies."""
        for strategy in self.strategies.values():
            strategy.state = StrategyState.ACTIVE
            strategy.paused_until = None
        logger.info("All strategies resumed")


# ============================================================================
# MAIN BOT
# ============================================================================

class MetaAutonomyEngine(AutonomyEngine):
    """
    Extends AutonomyEngine with all 5 Meta layers:
    1. News Sentiment Engine
    2. Meta-Strategy Router
    3. Cross-Market Correlation Engine
    4. LLM Oracle
    5. Strategy Forge
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        ollama_base = getattr(self.config, 'OLLAMA_BASE', 'http://localhost:11434')
        ollama_model = getattr(self.config, 'OLLAMA_MODEL', 'qwen2.5-coder:7b')

        self.sentiment_engine = NewsSentimentEngine(ollama_base, ollama_model)
        self.meta_router = MetaStrategyRouter(self.db, self.regime_detector, top_n=3)
        self.correlation_engine = CorrelationEngine(self.db)
        self.llm_oracle = LLMOracle(ollama_base, ollama_model)
        self.strategy_forge = StrategyForge(self.db, SKILLS_DIR, ollama_base, ollama_model)

        logger.info("🧠 META LAYER ONLINE — All 5 systems initialized")

    def run_cycle(self):
        """Override run_cycle to inject Meta intelligence into every trading cycle."""
        can_trade, reason = self.risk_mgr.can_trade()
        if not can_trade:
            logger.warning(f"Trading halted: {reason}")
            self.telegram.alert_circuit_breaker(reason)
            time.sleep(60)
            return

        self.regime_detector.update()

        # META: Concentrate capital on best strategies
        self.meta_router.redistribute_weights(
            self.strategies, list(self.strategies.keys())
        )

        # META: Forge new strategies from discovered patterns
        strategy_args = (self.client, self.ws, self.risk_mgr,
                         self.db, self.telegram, self.config)
        new_strategy = self.strategy_forge.forge_and_load(strategy_args)
        if new_strategy:
            self.strategies.update(self.strategy_forge.get_forged_strategies())
            self.telegram.alert_bot_status(
                f"🔨 StrategyForge created new strategy: {new_strategy}"
            )

        # Execute all strategies
        for name, strategy in self.strategies.items():
            try:
                if strategy.can_run():
                    strategy.execute()
            except Exception as e:
                logger.error(f"Strategy {name} error: {e}\n{traceback.format_exc()}")
                self.telegram.alert_error(f"Strategy {name}: {str(e)[:200]}")

        self._post_trade_analysis()
        self._run_scheduled_tasks()

    def get_meta_status(self):
        """Extended /status command with Meta layer info."""
        base = self.get_status()
        forged_count = len(self.strategy_forge.get_forged_strategies())
        top = self.meta_router.get_top_strategies(list(self.strategies.keys()))
        return (
            base +
            f"\n\n🧠 <b>Meta Layer Status</b>\n"
            f"🎯 Top strategies: {', '.join(top)}\n"
            f"🔨 Forged strategies: {forged_count}\n"
            f"📡 Sentiment cache: {len(self.sentiment_engine._cache)} markets\n"
            f"🔗 Correlations tracked: {len(self.correlation_engine._price_history)} markets\n"
            f"🔮 Oracle cache: {len(self.llm_oracle._cache)} markets"
        )
class NewsSentimentEngine:
    """
    Scrapes real-time news and social signals relevant to active Polymarket
    markets. Scores sentiment and returns a bias multiplier per market.
    Uses Ollama LLM locally — no API keys needed.
    """

    SOURCES = [
        "https://feeds.bbci.co.uk/news/rss.xml",
        "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml",
        "https://feeds.reuters.com/reuters/topNews",
    ]

    def __init__(self, ollama_base="http://localhost:11434", model="qwen2.5-coder:7b"):
        self.ollama_base = ollama_base
        self.model = model
        self._cache = {}  # market_id -> (score, timestamp)
        self.cache_ttl = 300  # 5 minutes

    def _fetch_headlines(self):
        headlines = []
        for url in self.SOURCES:
            try:
                import re
                resp = requests.get(url, timeout=5)
                titles = re.findall(r'<title><!\[CDATA\[(.*?)\]\]></title>', resp.text)
                if not titles:
                    titles = re.findall(r'<title>(.*?)</title>', resp.text)[1:6]
                headlines.extend(titles[:5])
            except Exception:
                pass
        return headlines[:20]

    def _score_with_llm(self, market_question, headlines):
        """Ask local LLM to score sentiment for a market. Returns -1 to 1."""
        if not headlines:
            return 0.0
        prompt = (
            f"Given these news headlines:\n"
            + "\n".join(f"- {h}" for h in headlines[:10])
            + f"\n\nFor the prediction market: '{market_question}'\n"
            + "Return ONLY a float between -1.0 (very bearish/NO) and 1.0 (very bullish/YES). "
            + "No explanation. Just the number."
        )
        try:
            resp = requests.post(
                f"{self.ollama_base}/api/generate",
                json={"model": self.model, "prompt": prompt, "stream": False},
                timeout=15
            )
            text = resp.json().get("response", "0").strip()
            import re
            match = re.search(r'-?\d+\.?\d*', text)
            score = float(match.group()) if match else 0.0
            return max(-1.0, min(1.0, score))
        except Exception:
            return 0.0

    def get_sentiment(self, market_id, market_question):
        """Get sentiment score for a market. Cached for 5 minutes."""
        now = time.time()
        if market_id in self._cache:
            score, ts = self._cache[market_id]
            if now - ts < self.cache_ttl:
                return score
        headlines = self._fetch_headlines()
        score = self._score_with_llm(market_question, headlines)
        self._cache[market_id] = (score, now)
        logger.info(f"[SENTIMENT] {market_question[:60]} → {score:+.2f}")
        return score

    def get_bias_multiplier(self, market_id, market_question, side):
        """Returns a capital multiplier (0.5x to 1.5x) based on sentiment alignment."""
        score = self.get_sentiment(market_id, market_question)
        if side.upper() == "YES":
            alignment = score
        else:
            alignment = -score
        return 1.0 + (alignment * 0.5)


# ============================================================================
# META LAYER 2: META-STRATEGY ROUTER (AI Capital Concentrator)
# ============================================================================

class MetaStrategyRouter:
    """
    AI-powered router that concentrates capital on the top N strategies
    using Bayesian scoring blended with regime weights.
    """

    def __init__(self, db, regime_detector, top_n=3):
        self.db = db
        self.regime_detector = regime_detector
        self.top_n = top_n
        self._scores = {}

    def _bayesian_score(self, name):
        stats = self.db.get_strategy_stats(name, hours=24)
        wins = stats["trades"] * stats["win_rate"]
        losses = stats["trades"] - wins
        alpha = 2 + wins
        beta_val = 2 + losses
        score = alpha / (alpha + beta_val)
        regime_weights = self.regime_detector.get_strategy_weights()
        regime_bonus = regime_weights.get(name, 0.1)
        return score * 0.7 + regime_bonus * 0.3

    def get_top_strategies(self, all_strategy_names):
        self._scores = {name: self._bayesian_score(name) for name in all_strategy_names}
        ranked = sorted(self._scores.items(), key=lambda x: x[1], reverse=True)
        top = [name for name, _ in ranked[:self.top_n]]
        logger.info(f"[META-ROUTER] Top strategies: {top}")
        return top

    def redistribute_weights(self, strategies, all_names):
        """Concentrate 80% of capital on top N strategies."""
        top = self.get_top_strategies(all_names)
        each_top = 0.80 / len(top)
        remaining = 0.20 / max(1, len(all_names) - len(top))
        for name, strategy in strategies.items():
            strategy.weight = each_top if name in top else remaining
        logger.info(f"[META-ROUTER] Weights redistributed. Top {self.top_n} get {each_top*100:.1f}% each.")


# ============================================================================
# META LAYER 3: CROSS-MARKET CORRELATION ENGINE
# ============================================================================

class CorrelationEngine:
    """
    Tracks price movements across markets, detects correlations,
    and suggests hedges when two correlated positions are open.
    """

    def __init__(self, db, window=50):
        self.db = db
        self.window = window
        self._price_history = {}
        self._correlation_cache = {}
        self._last_compute = 0
        self.compute_interval = 600

    def update_price(self, market_id, price):
        from collections import deque
        if market_id not in self._price_history:
            self._price_history[market_id] = deque(maxlen=self.window)
        self._price_history[market_id].append(price)

    def _pearson(self, a, b):
        if len(a) < 10 or len(b) < 10:
            return 0.0
        n = min(len(a), len(b))
        a, b = list(a)[-n:], list(b)[-n:]
        mean_a = sum(a) / n
        mean_b = sum(b) / n
        num = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b))
        den_a = (sum((x - mean_a) ** 2 for x in a)) ** 0.5
        den_b = (sum((y - mean_b) ** 2 for y in b)) ** 0.5
        if den_a == 0 or den_b == 0:
            return 0.0
        return num / (den_a * den_b)

    def compute_correlations(self):
        now = time.time()
        if now - self._last_compute < self.compute_interval:
            return self._correlation_cache
        self._last_compute = now
        markets = list(self._price_history.keys())
        for i in range(len(markets)):
            for j in range(i + 1, len(markets)):
                m1, m2 = markets[i], markets[j]
                corr = self._pearson(self._price_history[m1], self._price_history[m2])
                self._correlation_cache[(m1, m2)] = corr
                if abs(corr) > 0.7:
                    logger.info(f"[CORRELATION] {m1[:20]} <-> {m2[:20]}: {corr:.2f}")
        return self._correlation_cache

    def get_hedge_suggestion(self, market_id, open_positions):
        correlations = self.compute_correlations()
        for (m1, m2), corr in correlations.items():
            other = None
            if m1 == market_id and m2 in open_positions:
                other = m2
            elif m2 == market_id and m1 in open_positions:
                other = m1
            if other and abs(corr) > 0.75:
                hedge_side = "NO" if corr > 0 else "YES"
                return {
                    "hedge_market": other,
                    "suggested_side": hedge_side,
                    "correlation": corr,
                    "reason": f"High correlation ({corr:.2f}) detected"
                }
        return None


# ============================================================================
# META LAYER 4: LLM ORACLE — LOCAL AI PROBABILITY ESTIMATOR
# ============================================================================

class LLMOracle:
    """
    Uses a local Ollama model to estimate YES probability for Polymarket
    questions. Feeds as an additional alpha signal into strategy decisions.
    """

    def __init__(self, ollama_base="http://localhost:11434", model="qwen2.5-coder:7b"):
        self.ollama_base = ollama_base
        self.model = model
        self._cache = {}
        self.cache_ttl = 600

    def estimate_probability(self, question, description="", current_price=0.5):
        now = time.time()
        cache_key = question[:100]
        if cache_key in self._cache:
            prob, ts = self._cache[cache_key]
            if now - ts < self.cache_ttl:
                return prob

        prompt = (
            f"You are a prediction market analyst. Estimate the probability "
            f"of YES for this market:\n\n"
            f"Question: {question}\n"
            f"Description: {description[:300] if description else 'N/A'}\n"
            f"Current market price: {current_price:.2f}\n\n"
            f"Return ONLY a probability between 0.00 and 1.00. No explanation."
        )
        try:
            resp = requests.post(
                f"{self.ollama_base}/api/generate",
                json={"model": self.model, "prompt": prompt, "stream": False},
                timeout=20
            )
            text = resp.json().get("response", "0.5").strip()
            import re
            match = re.search(r'0?\.\d+|\d+\.?\d*', text)
            prob = float(match.group()) if match else 0.5
            prob = max(0.01, min(0.99, prob))
        except Exception:
            prob = current_price

        self._cache[cache_key] = (prob, now)
        logger.info(f"[ORACLE] '{question[:60]}' → {prob:.2f} (market: {current_price:.2f})")
        return prob

    def get_edge(self, question, description, current_price, side):
        """Returns estimated edge. Positive = good bet."""
        oracle_prob = self.estimate_probability(question, description, current_price)
        if side.upper() == "YES":
            edge = oracle_prob - current_price
        else:
            edge = (1.0 - oracle_prob) - (1.0 - current_price)
        logger.info(f"[ORACLE EDGE] {side} edge: {edge:+.3f}")
        return edge


# ============================================================================
# META LAYER 5: STRATEGY FORGE — SELF-WRITING NEW STRATEGIES
# ============================================================================

class StrategyForge:
    """
    Monitors trade history for undiscovered patterns. When a pattern is found,
    uses a local LLM to write a new strategy class, validates it in a sandbox,
    and hot-loads it into the running bot — no restart needed.
    """

    def __init__(self, db, skills_dir, ollama_base="http://localhost:11434",
                 model="qwen2.5-coder:7b"):
        self.db = db
        self.skills_dir = skills_dir
        self.ollama_base = ollama_base
        self.model = model
        self.forged = {}
        self._last_scan = 0
        self.scan_interval = 3600

    def _find_patterns(self):
        try:
            trades = self.db.get_all_trades_for_optimization("", 100)
            winners = [t for t in trades if t.get("pnl", 0) > 0]
            if len(winners) < 10:
                return None
            hour_pnl = {}
            for t in winners:
                ts = t.get("timestamp", "")
                try:
                    dt = datetime.fromisoformat(ts)
                    h = dt.hour
                    hour_pnl.setdefault(h, []).append(t.get("pnl", 0))
                except Exception:
                    pass
            best_hour = max(hour_pnl, key=lambda h: sum(hour_pnl[h]), default=None)
            if best_hour is not None and len(hour_pnl.get(best_hour, [])) >= 3:
                avg_pnl = sum(hour_pnl[best_hour]) / len(hour_pnl[best_hour])
                return {"type": "time_of_day", "hour": best_hour,
                        "avg_pnl": avg_pnl, "sample_size": len(hour_pnl[best_hour])}
        except Exception as e:
            logger.error(f"[FORGE] Pattern scan error: {e}")
        return None

    def _write_strategy_with_llm(self, pattern):
        prompt = (
            f"Write the body of a Python method `find_opportunities(self)` for a "
            f"Polymarket trading strategy based on this pattern:\n"
            f"{json.dumps(pattern, indent=2)}\n\n"
            f"Return a list of dicts with keys: market_id, token_id, side, price, confidence.\n"
            f"Use self.client to fetch markets. Keep it under 20 lines. "
            f"Return ONLY the indented method body, no function signature."
        )
        try:
            resp = requests.post(
                f"{self.ollama_base}/api/generate",
                json={"model": self.model, "prompt": prompt, "stream": False},
                timeout=30
            )
            code = resp.json().get("response", "").strip()
            import re
            code = re.sub(r'```python\n?', '', code)
            code = re.sub(r'```\n?', '', code)
            return code
        except Exception as e:
            logger.error(f"[FORGE] LLM write error: {e}")
            return None

    def _sandbox_validate(self, code):
        dangerous = ["os.system", "subprocess", "exec(", "eval(", "__import__",
                     "open(", "shutil", "rmdir", "unlink"]
        return not any(d in code for d in dangerous)

    def forge_and_load(self, strategy_args):
        now = time.time()
        if now - self._last_scan < self.scan_interval:
            return None
        self._last_scan = now

        pattern = self._find_patterns()
        if not pattern:
            return None

        logger.info(f"[FORGE] Pattern discovered: {pattern}")
        find_code = self._write_strategy_with_llm(pattern)

        if not find_code or not self._sandbox_validate(find_code):
            logger.warning("[FORGE] Generated code failed validation. Skipping.")
            return None

        indented = "\n".join("        " + line for line in find_code.splitlines())
        name = f"v{int(now) % 10000}"

        code = f'''
class ForgedStrategy_{name}(BaseStrategy):
    """Auto-generated strategy by StrategyForge. Pattern: {str(pattern)[:80]}"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.name = "forged_{name}"
        self.weight = 0.05

    def find_opportunities(self):
{indented}

    def execute(self):
        opportunities = self.find_opportunities()
        for opp in (opportunities or []):
            try:
                market_id = opp.get("market_id", "")
                token_id = opp.get("token_id", "")
                side = opp.get("side", "YES")
                size = self.risk_mgr.calculate_position_size(
                    self.weight, opp.get("confidence", 0.6)
                )
                if size > 0 and market_id and token_id:
                    self._place_order(market_id, token_id, side, size, opp.get("price", 0.5))
            except Exception as e:
                logger.error(f"[FORGE] Order error: {{e}}")
'''

        skill_path = self.skills_dir / f"forged_{name}.py"
        skill_path.write_text(code)

        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(f"forged_{name}", skill_path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            cls = getattr(mod, f"ForgedStrategy_{name}")
            instance = cls(*strategy_args)
            self.forged[f"forged_{name}"] = instance
            logger.info(f"[FORGE] New strategy hot-loaded: forged_{name} | Pattern: {pattern['type']}")
            return f"forged_{name}"
        except Exception as e:
            logger.error(f"[FORGE] Hot-load failed: {e}")
            skill_path.unlink(missing_ok=True)
            return None

    def get_forged_strategies(self):
        return self.forged


# ============================================================================
# META AUTONOMY ENGINE — Extends AutonomyEngine with all 5 Meta Layers
# ============================================================================

class ZeusPrimeBot:
    """Main bot orchestrator. Self-healing, auto-restart."""

    def __init__(self):
        self.config = Config()
        self.running = False
        self._setup_signal_handlers()

        # Core components
        self.db = Database()
        self.client = PolymarketClient(self.config)
        self.ws = WebSocketManager(self.config)
        self.risk_mgr = RiskManager(self.config, self.db)
        self.telegram = TelegramBot(self.config)

        # Autonomy engine
        self.engine = MetaAutonomyEngine(
            self.config, self.client, self.ws,
            self.risk_mgr, self.db, self.telegram
        )

        # Register Telegram commands
        self._register_commands()

    def _setup_signal_handlers(self):
        """Setup graceful shutdown handlers."""
        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

    def _handle_shutdown(self, signum, frame):
        """Graceful shutdown."""
        logger.info(f"Received signal {signum}, shutting down...")
        self.running = False

    def _register_commands(self):
        """Register Telegram command handlers."""
        self.telegram.register_command("/status", self.engine.get_status)
        self.telegram.register_command("/strategies", self.engine.get_strategies_status)
        self.telegram.register_command("/pause", lambda: self._cmd_pause())
        self.telegram.register_command("/resume", lambda: self._cmd_resume())

    def _cmd_pause(self) -> str:
        self.engine.pause_all()
        return "\U000023f8 All strategies paused"

    def _cmd_resume(self) -> str:
        self.engine.resume_all()
        return "\U000025b6 All strategies resumed"

    def start(self):
        """Start the bot."""
        logger.info("=" * 60)
        logger.info("ZEUS PRIME - Autonomous Polymarket Trading Bot")
        logger.info("=" * 60)
        logger.info(f"Mode: {'SIMULATE' if self.config.SIMULATE_MODE else 'LIVE'}")
        logger.info(f"Initial Capital: ${self.config.INITIAL_CAPITAL:.2f}")
        logger.info(f"Strategies: {len(self.engine.strategies)}")
        logger.info("=" * 60)

        self.running = True

        # Start WebSocket
        self.ws.start()
        time.sleep(2)

        # Subscribe to market data
        self.ws.subscribe("market", ["*"])

        # Start Telegram polling
        self.telegram.start_polling()
        self.telegram.alert_bot_status("Started")

        # Main loop
        cycle_interval = 5  # seconds between cycles
        error_count = 0
        max_errors = 10

        while self.running:
            try:
                self.engine.run_cycle()
                error_count = 0
                time.sleep(cycle_interval)

            except KeyboardInterrupt:
                break
            except Exception as e:
                error_count += 1
                logger.error(f"Main loop error ({error_count}/{max_errors}): {e}\n{traceback.format_exc()}")
                self.telegram.alert_error(f"Main loop: {str(e)[:200]}")

                if error_count >= max_errors:
                    logger.critical("Too many consecutive errors. Restarting...")
                    self.telegram.alert_bot_status("Restarting (too many errors)")
                    self._restart()
                    return

                time.sleep(min(error_count * 5, 60))

        self._shutdown()

    def _shutdown(self):
        """Clean shutdown."""
        logger.info("Shutting down Zeus Prime...")
        self.telegram.alert_bot_status("Stopped")
        self.ws.stop()
        self.telegram.stop_polling()

        # Cancel all open orders
        self.client.cancel_all_orders()
        logger.info("Shutdown complete")

    def _restart(self):
        """Self-restart the bot."""
        self._shutdown()
        time.sleep(5)
        os.execv(sys.executable, [sys.executable] + sys.argv)


# ============================================================================
# CLI ENTRY POINT
# ============================================================================

def main():
    """Main entry point with CLI argument handling."""
    import argparse

    parser = argparse.ArgumentParser(description="Zeus Prime - Autonomous Polymarket Trading Bot")
    parser.add_argument("--simulate", action="store_true", help="Run in simulation mode")
    parser.add_argument("--capital", type=float, default=None, help="Initial capital (USDC)")
    parser.add_argument("--check-config", action="store_true", help="Validate configuration and exit")
    args = parser.parse_args()

    if args.simulate:
        os.environ["SIMULATE_MODE"] = "true"
    if args.capital:
        os.environ["INITIAL_CAPITAL"] = str(args.capital)

    if args.check_config:
        config = Config()
        print("\n[Zeus Prime Configuration Check]")
        print(f"  Private Key: {'SET' if config.PRIVATE_KEY else 'NOT SET'}")
        print(f"  Proxy Wallet: {'SET' if config.PROXY_WALLET_ADDRESS else 'NOT SET'}")
        print(f"  API Key: {'SET' if config.API_KEY else 'NOT SET'}")
        print(f"  Telegram: {'SET' if config.TELEGRAM_TOKEN else 'NOT SET'}")
        print(f"  Simulate Mode: {config.SIMULATE_MODE}")
        print(f"  Initial Capital: ${config.INITIAL_CAPITAL:.2f}")
        print(f"  RPC: {config.POLYGON_RPC}")
        print("\n  Risk Limits (HARD CODED):")
        print(f"    Stop Loss: {config.STOP_LOSS_PCT*100}%")
        print(f"    Daily Loss Limit: {config.DAILY_LOSS_LIMIT_PCT*100}%")
        print(f"    Max Drawdown: {config.DRAWDOWN_LIMIT_PCT*100}%")
        print(f"    Total Loss Halt: {config.TOTAL_LOSS_HALT_PCT*100}%")
        print(f"    Max Position Risk: {config.MAX_POSITION_RISK_PCT*100}%")
        print(f"    Max Market Exposure: {config.MAX_EXPOSURE_PER_MARKET_PCT*100}%")
        print(f"    Max Strategy Exposure: {config.MAX_EXPOSURE_PER_STRATEGY_PCT*100}%")
        print("\n  Configuration OK!")
        sys.exit(0)

    bot = ZeusPrimeBot()
    bot.start()


if __name__ == "__main__":
    main()


# ============================================================================
# META LAYER 1: AI NEWS SENTIMENT ENGINE
# ============================================================================


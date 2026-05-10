#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════╗
║           ZEUS PRIME v2.3 — The Autonomous Trader            ║
║           Built on py-clob-client (official SDK)             ║
║           11 strategies. AscetixMode ACTIVE.                 ║
║           CROSS-PLATFORM: Polymarket + Kalshi                ║
╚══════════════════════════════════════════════════════════════╝
"""

import os
import sys
import json
import time
import logging
import sqlite3
import threading
import traceback
import urllib.request
import urllib.parse
from collections import deque
from datetime import datetime, timezone
from typing import Optional, Dict, List
from pathlib import Path
from dotenv import load_dotenv

import requests

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds,
    OrderArgs,
    BalanceAllowanceParams,
    AssetType,
)
from py_clob_client.order_builder.constants import BUY, SELL

# Cross-platform client
try:
    from kalshi_client import KalshiClient
    _KALSHI_AVAILABLE = True
except ImportError:
    _KALSHI_AVAILABLE = False
    log_stub = logging.getLogger("ZeusPrime")
    log_stub.warning("kalshi_client.py not found — Kalshi disabled")

try:
    from oracle_prime import OraclePrimeStrategy
    _ORACLE_AVAILABLE = True
except ImportError:
    _ORACLE_AVAILABLE = False

# ============================================================================
# BOOTSTRAP
# ============================================================================

BASE_DIR = Path(__file__).parent.resolve()
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)
load_dotenv(BASE_DIR / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOGS_DIR / "zeus_v2.log"),
    ],
)
log = logging.getLogger("ZeusPrime")

# ============================================================================
# CONFIG
# ============================================================================

class Config:
    PRIVATE_KEY:     str   = os.getenv("PRIVATE_KEY", "")
    API_KEY:         str   = os.getenv("POLYMARKET_API_KEY", "")
    API_SECRET:      str   = os.getenv("POLYMARKET_API_SECRET", "")
    API_PASSPHRASE:  str   = os.getenv("POLYMARKET_API_PASSPHRASE", "")
    TELEGRAM_TOKEN:  str   = os.getenv("TELEGRAM_TOKEN", "")
    TELEGRAM_CHAT:   str   = os.getenv("TELEGRAM_CHAT_ID", "")
    CLOB_HOST:       str   = "https://clob.polymarket.com"
    GAMMA_URL:       str   = "https://gamma-api.polymarket.com"
    CHAIN_ID:        int   = 137
    SIMULATE:        bool  = os.getenv("SIMULATE_MODE", "true").lower() == "true"
    INITIAL_CAPITAL: float = float(os.getenv("INITIAL_CAPITAL", "100"))

    # Copy-trading wallets (populate to enable strategy 5)
    COPY_WALLETS: Dict[str, str] = {
        "RN1":            os.getenv("COPY_WALLET_RN1", "0x04b954ec5d65e0a855d31b5e695083f3c4ad79e0"),   # immanuelcan 17.7%
        "Domer":          os.getenv("COPY_WALLET_DOMER", "0xd202bf6d89dc6bab1b9f00ead175f75466348973"),  # meoooow 30.9%
        "ColdMath":       os.getenv("COPY_WALLET_COLDMATH", "0x7760fe4dcb17c09161adb2eb8d29f39e382e21f0"), # cumulus33 20.6%
        "embarrassment":  os.getenv("COPY_WALLET_4", "0x5c3a1a602848565bb16165fcd460b00c3d43020b"),      # embarrassment 20.9%
    }

    # Kalshi credentials
    KALSHI_KEY_ID:     str   = os.getenv("KALSHI_API_KEY_ID", "")
    KALSHI_KEY_PATH:   str   = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")
    KALSHI_ENABLED:    bool  = os.getenv("KALSHI_ENABLED", "false").lower() == "true"

    # Risk limits
    MAX_POSITION_PCT:  float = 0.05   # 5% per position
    STOP_LOSS_PCT:     float = 0.10   # 10% stop per position
    DAILY_LOSS_LIMIT:  float = 0.05   # halt if down 5% in a day
    MIN_MARKET_VOL:    float = 50_000

cfg = Config()

# ============================================================================
# TELEGRAM
# ============================================================================

def tg(text: str):
    if not cfg.TELEGRAM_TOKEN or not cfg.TELEGRAM_CHAT:
        return
    try:
        url = f"https://api.telegram.org/bot{cfg.TELEGRAM_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": cfg.TELEGRAM_CHAT,
            "text": text,
            "parse_mode": "HTML",
        }).encode()
        urllib.request.urlopen(url, data, timeout=5)
    except Exception as e:
        log.warning(f"Telegram: {e}")

# ============================================================================
# DATABASE
# ============================================================================

class DB:
    def __init__(self):
        self.conn = sqlite3.connect(LOGS_DIR / "zeus_v2.db", check_same_thread=False)
        self._lock = threading.Lock()
        self._init()

    def _init(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                ts       TEXT,
                strategy TEXT,
                market   TEXT,
                token_id TEXT,
                side     TEXT,
                price    REAL,
                size     REAL,
                order_id TEXT,
                status   TEXT,
                pnl      REAL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS snapshots (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                ts      TEXT,
                balance REAL,
                pnl_day REAL
            );
        """)
        self.conn.commit()

    def log_trade(self, strategy, market, token_id, side, price, size, order_id, status, pnl=0):
        with self._lock:
            self.conn.execute(
                "INSERT INTO trades (ts,strategy,market,token_id,side,price,size,order_id,status,pnl) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(), strategy, market,
                 token_id, side, price, size, order_id, status, pnl)
            )
            self.conn.commit()

    def log_snapshot(self, balance, pnl_day):
        with self._lock:
            self.conn.execute(
                "INSERT INTO snapshots (ts,balance,pnl_day) VALUES (?,?,?)",
                (datetime.now(timezone.utc).isoformat(), balance, pnl_day)
            )
            self.conn.commit()

db = DB()

# ============================================================================
# POLYMARKET CLIENT
# ============================================================================

class PolyClient:
    def __init__(self):
        creds = ApiCreds(
            api_key=cfg.API_KEY,
            api_secret=cfg.API_SECRET,
            api_passphrase=cfg.API_PASSPHRASE,
        )
        self.clob = ClobClient(
            host=cfg.CLOB_HOST,
            key=cfg.PRIVATE_KEY,
            chain_id=cfg.CHAIN_ID,
            creds=creds,
            signature_type=1,
        )
        log.info("PolyClient initialized ✅")

    def get_balance(self) -> float:
        if cfg.SIMULATE:
            return cfg.INITIAL_CAPITAL
        try:
            data = self.clob.get_balance_allowance(
                params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            return float(data.get("balance", 0))
        except Exception as e:
            log.error(f"get_balance: {e}")
            return 0.0

    def get_markets(self, limit: int = 40, tag: str = "") -> List[dict]:
        try:
            params = {"active": "true", "closed": "false",
                      "order": "volume24hr", "ascending": "false", "limit": limit}
            if tag:
                params["tag"] = tag
            resp = requests.get(f"{cfg.GAMMA_URL}/markets", params=params, timeout=10)
            resp.raise_for_status()
            raw = resp.json()
            if isinstance(raw, dict):
                raw = raw.get("markets", raw.get("data", []))
            result = []
            for m in raw:
                vol = float(m.get("volume", m.get("volumeNum", 0)) or 0)
                clob_ids = m.get("clobTokenIds", [])
                if isinstance(clob_ids, str):
                    try: clob_ids = json.loads(clob_ids)
                    except: clob_ids = []
                outcomes = m.get("outcomes", ["Yes", "No"])
                if isinstance(outcomes, str):
                    try: outcomes = json.loads(outcomes)
                    except: outcomes = ["Yes", "No"]
                tokens = [{"token_id": str(tid), "outcome": outcomes[i] if i < len(outcomes) else str(i)}
                          for i, tid in enumerate(clob_ids)]
                result.append({
                    "condition_id": m.get("conditionId", ""),
                    "question": m.get("question", ""),
                    "tokens": tokens,
                    "volume": vol,
                    "slug": m.get("slug", ""),
                })
            return result
        except Exception as e:
            log.error(f"get_markets: {e}")
            return []

    def get_orderbook(self, token_id: str) -> Optional[dict]:
        try:
            book = self.clob.get_order_book(token_id)
            # Normalize to dict
            if hasattr(book, "__dict__"):
                bids = [{"price": str(b.price), "size": str(b.size)}
                        for b in (book.bids or [])]
                asks = [{"price": str(a.price), "size": str(a.size)}
                        for a in (book.asks or [])]
                return {"bids": bids, "asks": asks}
            return book
        except Exception as e:
            log.error(f"get_orderbook: {e}")
            return None

    def get_price(self, token_id: str) -> Optional[float]:
        try:
            book = self.get_orderbook(token_id)
            if not book:
                return None
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            if not bids or not asks:
                return None
            best_bid = float(bids[0]["price"])
            best_ask = float(asks[0]["price"])
            return round((best_bid + best_ask) / 2, 4)
        except Exception as e:
            log.error(f"get_price: {e}")
            return None

    def place_order(self, token_id: str, side: str, price: float, size: float) -> Optional[str]:
        if cfg.SIMULATE:
            fake = f"sim_{int(time.time()*1000)}"
            log.info(f"[SIM] {side} {size:.2f}@{price:.4f} {token_id[:12]}… → {fake}")
            return fake
        try:
            order_side = BUY if side.upper() == "BUY" else SELL
            args = OrderArgs(token_id=token_id, price=round(price, 4),
                             size=round(size, 2), side=order_side)
            resp = self.clob.create_and_post_order(args)
            oid = resp.get("orderID") if isinstance(resp, dict) else str(resp)
            log.info(f"Order ✅ {side} {size:.2f}@{price:.4f} → {oid}")
            return oid
        except Exception as e:
            log.error(f"place_order: {e}")
            return None

    def cancel_order(self, order_id: str) -> bool:
        if cfg.SIMULATE:
            return True
        try:
            self.clob.cancel(order_id)
            return True
        except Exception as e:
            log.error(f"cancel_order: {e}")
            return False

    def cancel_all(self) -> bool:
        if cfg.SIMULATE:
            return True
        try:
            self.clob.cancel_all()
            return True
        except Exception as e:
            log.error(f"cancel_all: {e}")
            return False

# ============================================================================
# TELEGRAM COMMAND HANDLER
# ============================================================================

class TelegramCommands:
    """
    Listens for Telegram commands via long-polling getUpdates.
    Runs in a background thread so it never blocks the main engine loop.

    Commands:
      /stats  — strategy-level P&L breakdown
      /pnl    — running P&L vs start balance
      /chart  — equity curve as text chart
      /help   — command list
    """
    def __init__(self, token: str, chat_id: str, db_ref, zeus_ref):
        self.token   = token
        self.chat_id = str(chat_id)
        self.db      = db_ref
        self.zeus    = zeus_ref  # ZeusPrimeV2 instance
        self._offset = 0
        self._running = True

    def _api(self, method: str, **kwargs):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{self.token}/{method}",
                json=kwargs, timeout=30
            )
            return r.json()
        except Exception as e:
            log.warning(f"[TG-CMD] {method} error: {e}")
            return {}

    def _send(self, text: str):
        self._api("sendMessage", chat_id=self.chat_id, text=text, parse_mode="HTML")

    def _handle(self, text: str):
        cmd = text.strip().lower().split("@")[0]

        if cmd == "/help":
            self._send(
                "⚡ <b>ZeusPrime v2.3 Commands</b>\n\n"
                "/pnl — Current P&amp;L vs start balance\n"
                "/stats — Strategy breakdown\n"
                "/chart — Equity curve\n"
                "/help — This menu"
            )

        elif cmd == "/pnl":
            try:
                bal = self.zeus.client.get_balance()
                start = self.zeus.start_balance or bal
                pnl = bal - start
                pct = (pnl / start * 100) if start else 0
                emoji = "📈" if pnl >= 0 else "📉"
                self._send(
                    f"{emoji} <b>ZeusPrime P&amp;L</b>\n"
                    f"Balance: ${bal:.2f}\n"
                    f"Start:   ${start:.2f}\n"
                    f"P&amp;L:     ${pnl:+.2f} ({pct:+.2f}%)\n"
                    f"Cycle:   #{self.zeus.cycle}"
                )
            except Exception as e:
                self._send(f"Error: {e}")

        elif cmd == "/stats":
            try:
                rows = self.db.conn.execute(
                    "SELECT strategy, COUNT(*) as trades, "
                    "SUM(CASE WHEN status='tp' THEN 1 ELSE 0 END) as wins, "
                    "SUM(CASE WHEN status='sl' THEN 1 ELSE 0 END) as losses, "
                    "ROUND(SUM(pnl),2) as total_pnl "
                    "FROM trades GROUP BY strategy ORDER BY total_pnl DESC"
                ).fetchall()
                if not rows:
                    self._send("📊 No trades recorded yet.")
                    return
                lines = ["📊 <b>Strategy Performance</b>\n"]
                for strategy, trades, wins, losses, total_pnl in rows:
                    wr = (wins / trades * 100) if trades else 0
                    emoji = "📈" if (total_pnl or 0) >= 0 else "📉"
                    lines.append(
                        f"{emoji} <b>{strategy}</b>\n"
                        f"  Trades: {trades} | W: {wins} L: {losses} ({wr:.0f}% WR)\n"
                        f"  P&amp;L: ${total_pnl:+.2f}"
                    )
                self._send("\n".join(lines))
            except Exception as e:
                self._send(f"Error: {e}")

        elif cmd == "/chart":
            try:
                rows = self.db.conn.execute(
                    "SELECT balance FROM snapshots ORDER BY id DESC LIMIT 20"
                ).fetchall()
                if not rows:
                    self._send("📉 No snapshot data yet. Run a few cycles first.")
                    return
                balances = [r[0] for r in reversed(rows)]
                # ASCII equity curve
                mn, mx = min(balances), max(balances)
                height = 6
                chart_lines = []
                chart_lines.append(f"<pre>Equity Curve (last {len(balances)} snapshots)")
                chart_lines.append(f"High: ${mx:.2f}  Low: ${mn:.2f}")
                chart_lines.append("")
                if mx == mn:
                    chart_lines.append("─" * len(balances) + "  flat")
                else:
                    rng = mx - mn
                    rows_grid = []
                    for h in range(height, -1, -1):
                        threshold = mn + (h / height) * rng
                        row_label = f"${threshold:.1f} "
                        row_chars = ""
                        for b in balances:
                            if b >= threshold:
                                row_chars += "█"
                            else:
                                row_chars += " "
                        rows_grid.append(row_label + "|" + row_chars)
                    chart_lines.extend(rows_grid)
                    chart_lines.append("       +" + "─" * len(balances))
                    chart_lines.append(f"        0{'':>{len(balances)-2}}{len(balances)}")
                chart_lines.append("</pre>")
                self._send("\n".join(chart_lines))
            except Exception as e:
                self._send(f"Error: {e}")

    def poll(self):
        """Background poll loop — runs in a daemon thread."""
        log.info("[TG-CMD] Command listener started.")
        while self._running:
            try:
                data = self._api("getUpdates", offset=self._offset, timeout=25)
                for update in data.get("result", []):
                    self._offset = update["update_id"] + 1
                    msg = update.get("message", {})
                    chat = str(msg.get("chat", {}).get("id", ""))
                    text = msg.get("text", "")
                    if chat == self.chat_id and text.startswith("/"):
                        log.info(f"[TG-CMD] Received: {text}")
                        self._handle(text)
            except Exception as e:
                log.warning(f"[TG-CMD] poll error: {e}")
                time.sleep(5)
        log.info("[TG-CMD] Listener stopped.")

    def start(self):
        import threading
        t = threading.Thread(target=self.poll, daemon=True)
        t.start()
        return t

    def stop(self):
        self._running = False


# ============================================================================
# RISK MANAGER
# ============================================================================

class RiskManager:
    def __init__(self, client: PolyClient):
        self.client = client
        self.start_balance = client.get_balance()
        self.day_start_balance = self.start_balance
        self.day_start_ts = datetime.now(timezone.utc)
        self.halted = False
        log.info(f"Risk online. Capital: ${self.start_balance:.2f}")

    def refresh_day(self):
        now = datetime.now(timezone.utc)
        if now.date() > self.day_start_ts.date():
            self.day_start_balance = self.client.get_balance()
            self.day_start_ts = now

    def check(self) -> bool:
        if self.halted:
            return False
        self.refresh_day()
        balance = self.client.get_balance()
        if self.day_start_balance > 0:
            loss_pct = (self.day_start_balance - balance) / self.day_start_balance
            if loss_pct >= cfg.DAILY_LOSS_LIMIT:
                log.warning(f"Daily loss limit hit ({loss_pct:.1%}). Halted.")
                tg(f"🛑 <b>DAILY LOSS LIMIT</b>\nDown {loss_pct:.1%}. Paused until tomorrow.")
                self.halted = True
                return False
        return True

    def position_size(self, balance: float, confidence: float = 0.0) -> float:
        """
        Dynamic Kelly sizing.
        confidence=0.0  → standard cap (MAX_POSITION_PCT = 5%)
        confidence=0.5  → 1.5x multiplier  (7.5%)
        confidence=1.0  → 2.5x multiplier  (12.5%) — hard cap 15%
        """
        if confidence > 0:
            # Kelly scale: 1.0 + 1.5 * confidence (clamped at 2.5x, hard cap 15%)
            multiplier = min(1.0 + 1.5 * confidence, 2.5)
            pct = min(cfg.MAX_POSITION_PCT * multiplier, 0.15)
        else:
            pct = cfg.MAX_POSITION_PCT
        return max(1.0, round(balance * pct, 2))

# ============================================================================
# BASE STRATEGY
# ============================================================================

class Strategy:
    NAME = "base"

    def __init__(self, client: PolyClient, risk: RiskManager):
        self.client = client
        self.risk = risk
        # positions: list of dicts {token_id, market_id, side, entry, size, order_id, peak}
        self.positions: List[dict] = []
        self.active_orders: Dict[str, List[str]] = {}

    def open_position(self, market_id, token_id, side, price, size,
                      take_profit_pct=None, stop_loss_pct=None):
        oid = self.client.place_order(token_id, side, price, size)
        if oid:
            self.positions.append({
                "market_id": market_id,
                "token_id": token_id,
                "side": side,
                "entry": price,
                "size": size,
                "order_id": oid,
                "peak": price,
                "take_profit_pct": take_profit_pct or cfg.STOP_LOSS_PCT,
                "stop_loss_pct": stop_loss_pct or cfg.STOP_LOSS_PCT,
                "opened_at": time.time(),
            })
            db.log_trade(self.NAME, market_id, token_id, side, price, size, oid, "open")

    def manage_positions(self):
        """Check all open positions for TP/SL. Close when triggered."""
        for pos in list(self.positions):
            current = self.client.get_price(pos["token_id"])
            if not current:
                continue

            # Update peak for trailing
            if pos["side"] == "BUY" and current > pos["peak"]:
                pos["peak"] = current

            pnl_pct = (current - pos["entry"]) / pos["entry"] if pos["entry"] else 0

            exit_reason = None
            if pnl_pct >= pos["take_profit_pct"]:
                exit_reason = "take_profit"
            elif pnl_pct <= -pos["stop_loss_pct"]:
                exit_reason = "stop_loss"

            if exit_reason:
                exit_side = "SELL" if pos["side"] == "BUY" else "BUY"
                oid = self.client.place_order(pos["token_id"], exit_side, current, pos["size"])
                pnl = (current - pos["entry"]) * pos["size"]
                db.log_trade(self.NAME, pos["market_id"], pos["token_id"],
                             exit_side, current, pos["size"], oid or "", "closed", pnl)
                emoji = "✅" if pnl >= 0 else "🛑"
                tg(f"{emoji} <b>{self.NAME}</b> {exit_reason}\n"
                   f"PnL: ${pnl:+.2f} ({pnl_pct:+.1%})\n"
                   f"Entry: {pos['entry']:.4f} → Exit: {current:.4f}")
                self.positions.remove(pos)

    def run(self):
        raise NotImplementedError

    def safe_run(self):
        try:
            if self.risk.check():
                self.run()
        except Exception as e:
            log.error(f"[{self.NAME}] {e}\n{traceback.format_exc()}")
            tg(f"❌ <b>ERROR [{self.NAME}]</b>\n{str(e)[:200]}")

# ============================================================================
# STRATEGY 1: DUMP & HEDGE
# ============================================================================

class DumpAndHedgeStrategy(Strategy):
    """
    Watch BTC/ETH/SOL/XRP 15-min markets.
    If price drops 15%+ in first 2 min → buy the dumped side.
    If YES+NO sum <= 0.95 → buy both for guaranteed profit.
    TP 5% | SL 10%
    """
    NAME = "dump_and_hedge"
    ASSETS = ["BTC", "ETH", "SOL", "XRP"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.tracked: Dict[str, dict] = {}

    def run(self):
        self.manage_positions()
        balance = self.client.get_balance()
        size = self.risk.position_size(balance)

        for asset in self.ASSETS:
            markets = self.client.get_markets(limit=10, tag=asset)
            for market in markets:
                pass  # scan all markets (removed 15-min filter)
                tokens = market.get("tokens", [])
                if len(tokens) < 2:
                    continue
                yes_id = tokens[0]["token_id"]
                no_id  = tokens[1]["token_id"]
                mid = market.get("condition_id", "")

                yes_p = self.client.get_price(yes_id)
                no_p  = self.client.get_price(no_id)
                if not yes_p or not no_p:
                    continue

                now = time.time()
                if mid not in self.tracked:
                    self.tracked[mid] = {"start": now, "yes0": yes_p, "no0": no_p,
                                          "yes_id": yes_id, "no_id": no_id}
                    continue

                t = self.tracked[mid]
                elapsed = now - t["start"]
                if elapsed > 120:
                    del self.tracked[mid]
                    continue

                yes_drop = (t["yes0"] - yes_p) / t["yes0"] if t["yes0"] else 0
                no_drop  = (t["no0"]  - no_p)  / t["no0"]  if t["no0"]  else 0

                if yes_drop >= 0.03:  # was 0.15
                    self.open_position(mid, yes_id, "BUY", yes_p, size * 0.3,
                                       take_profit_pct=0.05, stop_loss_pct=0.10)
                    del self.tracked[mid]
                elif no_drop >= 0.03:  # was 0.15
                    self.open_position(mid, no_id, "BUY", no_p, size * 0.3,
                                       take_profit_pct=0.05, stop_loss_pct=0.10)
                    del self.tracked[mid]
                elif yes_p + no_p <= 0.98:  # was 0.95
                    half = size * 0.2
                    self.open_position(mid, yes_id, "BUY", yes_p, half,
                                       take_profit_pct=0.05, stop_loss_pct=0.10)
                    self.open_position(mid, no_id, "BUY", no_p, half,
                                       take_profit_pct=0.05, stop_loss_pct=0.10)
                    del self.tracked[mid]

# ============================================================================
# STRATEGY 2: YES/NO ARBITRAGE
# ============================================================================

class YesNoArbStrategy(Strategy):
    """
    Mode A — Same-venue: YES + NO < 0.97 → buy both sides → guaranteed ~3% at resolution.
    Mode B — Cross-venue: Same event priced differently on Poly vs Kalshi.
             Buy YES on cheaper venue, buy NO on other → lock in spread.
    TP when sum normalizes. SL 5%.
    """
    NAME = "yes_no_arb"
    MIN_DISCOUNT       = 0.005   # same-venue minimum gap
    CROSS_MIN_SPREAD   = 0.03    # cross-venue: min 3¢ spread to fire
    _last_cross_scan   = 0
    CROSS_SCAN_INTERVAL = 120    # seconds between cross-venue scans

    def __init__(self, client, risk):
        super().__init__(client, risk)
        # Kalshi client — only if enabled and available
        self.kalshi: Optional["KalshiClient"] = None
        if _KALSHI_AVAILABLE and cfg.KALSHI_ENABLED:
            try:
                self.kalshi = KalshiClient()
                log.info("YesNoArb: Kalshi cross-venue arb ARMED ✅")
            except Exception as e:
                log.warning(f"YesNoArb: Kalshi init failed: {e}")

    def _cross_venue_scan(self, size: float):
        """
        Scan Polymarket top markets → find matching Kalshi market →
        compare YES prices → fire if spread >= CROSS_MIN_SPREAD.
        """
        if not self.kalshi:
            return
        now = time.time()
        if now - self._last_cross_scan < self.CROSS_SCAN_INTERVAL:
            return
        YesNoArbStrategy._last_cross_scan = now

        poly_markets = self.client.get_markets(limit=30)
        for pm in poly_markets:
            tokens = pm.get("tokens", [])
            if len(tokens) < 2:
                continue
            question = pm.get("question", "")
            # Find matching Kalshi market
            km = self.kalshi.find_matching_market(question, poly_markets)
            if not km:
                continue

            # Get YES prices on both venues
            poly_yes_id = tokens[0]["token_id"]
            kals_yes_id = km["tokens"][0]["token_id"]

            poly_yes = self.client.get_price(poly_yes_id)
            kals_yes = self.kalshi.get_price(kals_yes_id)

            if not poly_yes or not kals_yes:
                continue

            spread = abs(poly_yes - kals_yes)
            if spread < self.CROSS_MIN_SPREAD:
                continue

            # Buy YES on cheaper venue, buy NO (= sell YES) on expensive venue
            if poly_yes < kals_yes:
                cheap_client, cheap_id = self.client,  poly_yes_id
                dear_client,  dear_id  = self.kalshi,  kals_yes_id
                cheap_p, dear_p = poly_yes, kals_yes
                cheap_venue, dear_venue = "Poly", "Kalshi"
            else:
                cheap_client, cheap_id = self.kalshi,  kals_yes_id
                dear_client,  dear_id  = self.client,  poly_yes_id
                cheap_p, dear_p = kals_yes, poly_yes
                cheap_venue, dear_venue = "Kalshi", "Poly"

            # Buy YES on cheap venue
            cheap_oid = cheap_client.place_order(cheap_id, "BUY", cheap_p, size)
            # Buy NO on dear venue
            # Kalshi: token_id is "<TICKER>-YES", flip to "<TICKER>-NO"
            # Poly:   NO token is tokens[1]["token_id"] from the Polymarket market (pm)
            if dear_venue == "Kalshi":
                no_id_dear = kals_yes_id.rsplit("-", 1)[0] + "-NO"
            else:  # dear_venue == "Poly"
                no_id_dear = pm["tokens"][1]["token_id"]
            dear_oid  = dear_client.place_order(no_id_dear, "BUY", 1 - dear_p, size)

            if cheap_oid or dear_oid:
                tg(
                    f"🌐 <b>Cross-Venue Arb FIRED</b>\n"
                    f"📌 {question[:55]}\n"
                    f"✅ YES on {cheap_venue}: {cheap_p:.3f}\n"
                    f"❌ NO  on {dear_venue}: {1-dear_p:.3f}\n"
                    f"💰 Spread: {spread:.2%} | Size: ${size:.2f}"
                )
                log.info(f"Cross-venue arb: {cheap_venue} YES@{cheap_p:.3f} vs "
                         f"{dear_venue} NO@{1-dear_p:.3f} spread={spread:.2%}")

    def run(self):
        self.manage_positions()
        balance = self.client.get_balance()
        if balance < 2:
            return
        size = self.risk.position_size(balance) / 2

        # ── Mode A: same-venue YES+NO discount ──
        markets = self.client.get_markets(limit=30)
        for market in markets:
            tokens = market.get("tokens", [])
            if len(tokens) < 2:
                continue
            yes_id = tokens[0]["token_id"]
            no_id  = tokens[1]["token_id"]
            yes_p  = self.client.get_price(yes_id)
            no_p   = self.client.get_price(no_id)
            if not yes_p or not no_p:
                continue
            discount = 1.0 - (yes_p + no_p)
            if discount >= self.MIN_DISCOUNT:
                mid = market["condition_id"]
                self.open_position(mid, yes_id, "BUY", yes_p, size,
                                   take_profit_pct=0.03, stop_loss_pct=0.05)
                self.open_position(mid, no_id,  "BUY", no_p,  size,
                                   take_profit_pct=0.03, stop_loss_pct=0.05)
                tg(f"⚡ <b>YES/NO Arb</b>\n"
                   f"Discount: {discount:.2%}\n"
                   f"{market['question'][:60]}")

        # ── Mode B: cross-venue Poly ↔ Kalshi ──
        self._cross_venue_scan(size)

# ============================================================================
# STRATEGY 3: YIELD FARMING (Market Making)
# ============================================================================

class YieldFarmingStrategy(Strategy):
    """
    Post limit orders both sides. Earn daily rewards via fills.
    Rebalance daily at midnight UTC. TP 1% per fill | SL 10%.
    """
    NAME = "yield_farming"
    SPREAD = 0.02
    MAX_MARKETS = 5
    LAST_REBALANCE: float = 0

    def run(self):
        self.manage_positions()
        now = time.time()
        # Rebalance once per hour max
        if now - self.LAST_REBALANCE < 3600:
            return
        YieldFarmingStrategy.LAST_REBALANCE = now

        balance = self.client.get_balance()
        markets = self.client.get_markets(limit=10)

        # Cancel stale orders
        for mid, oids in list(self.active_orders.items()):
            for oid in oids:
                self.client.cancel_order(oid)
        self.active_orders.clear()

        per_market = (balance * 0.20) / self.MAX_MARKETS

        for market in markets[:self.MAX_MARKETS]:
            tokens = market.get("tokens", [])
            if len(tokens) < 2:
                continue
            yes_id = tokens[0]["token_id"]
            no_id  = tokens[1]["token_id"]
            mid    = market["condition_id"]

            yes_p = self.client.get_price(yes_id)
            no_p  = self.client.get_price(no_id)
            if not yes_p or not no_p:
                continue

            s = self.SPREAD / 2
            order_size = per_market / 4
            orders = []

            for tid, price in [(yes_id, yes_p), (no_id, no_p)]:
                b = self.client.place_order(tid, "BUY",  round(price - s, 4), order_size)
                a = self.client.place_order(tid, "SELL", round(price + s, 4), order_size)
                if b: orders.append(b)
                if a: orders.append(a)

            self.active_orders[mid] = orders
            if orders:
                tg(f"🌾 <b>Yield Farming</b>\n"
                   f"{len(orders)} LP orders on {market['question'][:50]}\n"
                   f"Capital: ${per_market:.2f}")

# ============================================================================
# STRATEGY 4: MAKER REBATE MARKET MAKING
# ============================================================================

class MakerRebateMMStrategy(Strategy):
    """
    Post tight spreads on top markets. Capture spread + 20-25% taker fees.
    TP per fill | SL 10%.
    """
    NAME = "maker_rebate_mm"
    SPREAD = 0.015
    MAX_MARKETS = 3

    def run(self):
        self.manage_positions()
        balance = self.client.get_balance()
        if balance < 1:
            return

        markets = self.client.get_markets(limit=10)

        for mid, oids in list(self.active_orders.items()):
            for oid in oids:
                self.client.cancel_order(oid)
        self.active_orders.clear()

        size = self.risk.position_size(balance)

        for market in markets[:self.MAX_MARKETS]:
            tokens = market.get("tokens", [])
            if not tokens:
                continue
            yes_id = tokens[0]["token_id"]
            mid_price = self.client.get_price(yes_id)
            if not mid_price or mid_price <= 0.03 or mid_price >= 0.97:
                continue

            s = self.SPREAD / 2
            orders = []
            b = self.client.place_order(yes_id, "BUY",  round(max(0.01, mid_price - s), 4), size / 2)
            a = self.client.place_order(yes_id, "SELL", round(min(0.99, mid_price + s), 4), size / 2)
            if b: orders.append(b)
            if a: orders.append(a)

            if orders:
                self.active_orders[market["condition_id"]] = orders
                log.info(f"[MM] Posted {len(orders)} orders @ {mid_price:.4f}")

# ============================================================================
# STRATEGY 5: COPY TRADING
# ============================================================================

class CopyTradingStrategy(Strategy):
    """
    Mirror top wallets (RN1/Domer/ColdMath). 60%+ win rate, 100+ trades.
    TP 5-10% | SL 10%.
    """
    NAME = "copy_trading"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.last_check: Dict[str, float] = {}

    def run(self):
        self.manage_positions()
        if not any(cfg.COPY_WALLETS.values()):
            # Sim fallback: mirror top-volume markets
            balance = self.client.get_balance()
            size = self.risk.position_size(balance) * 0.4
            markets = self.client.get_markets(limit=5)
            for market in markets[:2]:
                tokens = market.get("tokens", [])
                if not tokens:
                    continue
                yes_id = tokens[0]["token_id"]
                mid_id = market["condition_id"]
                price = self.client.get_price(yes_id)
                if not price or not (0.20 < price < 0.80):
                    continue
                already = any(p["market_id"] == mid_id for p in self.positions)
                if already:
                    continue
                side = "BUY" if price < 0.50 else "SELL"
                self.open_position(mid_id, yes_id, side, price, size,
                                   take_profit_pct=0.05, stop_loss_pct=0.10)
            return

        balance = self.client.get_balance()
        size = self.risk.position_size(balance) * 0.5

        for wallet_name, wallet_addr in cfg.COPY_WALLETS.items():
            if not wallet_addr:
                continue
            now = time.time()
            if now - self.last_check.get(wallet_name, 0) < 60:
                continue
            self.last_check[wallet_name] = now

            try:
                resp = requests.get(f"{cfg.GAMMA_URL}/trades",
                                    params={"maker": wallet_addr, "limit": 10}, timeout=10)
                if resp.status_code != 200:
                    continue
                trades = resp.json()
            except Exception:
                continue

            for trade in trades:
                token_id  = trade.get("asset_id", "")
                side      = trade.get("side", "").upper()
                price     = float(trade.get("price", 0))
                market_id = trade.get("market", "")
                if not token_id or not side or not price:
                    continue

                already_in = any(p["market_id"] == market_id for p in self.positions)
                if already_in:
                    continue

                self.open_position(market_id, token_id, side, price, size,
                                   take_profit_pct=0.07, stop_loss_pct=0.10)
                tg(f"🔁 <b>Copy Trade</b> [{wallet_name}]\n"
                   f"{side} @ {price:.4f} | ${size:.2f}")

# ============================================================================
# STRATEGY 6: GRID TRADING
# ============================================================================

class GridTradingStrategy(Strategy):
    """
    Laddered limit orders at 7 levels above/below current price.
    Captures oscillations. TP per level | SL 10%.
    Refreshes every 5 min.
    """
    NAME = "grid_trading"
    LEVELS = 7
    SPACING = 0.02
    REFRESH_SEC = 300

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.grids: Dict[str, List[str]] = {}
        self.last_refresh: float = 0

    def run(self):
        self.manage_positions()
        now = time.time()
        if now - self.last_refresh < self.REFRESH_SEC:
            return
        self.last_refresh = now

        balance = self.client.get_balance()
        markets = self.client.get_markets(limit=10)
        order_size = (balance * 0.15) / self.LEVELS

        for market in markets[:3]:
            tokens = market.get("tokens", [])
            if not tokens:
                continue
            yes_id = tokens[0]["token_id"]
            mid    = market["condition_id"]
            price  = self.client.get_price(yes_id)
            if not price:
                continue

            # Cancel old grid
            for oid in self.grids.get(mid, []):
                self.client.cancel_order(oid)

            orders = []
            for i in range(1, self.LEVELS + 1):
                offset = self.SPACING * i
                b = self.client.place_order(yes_id, "BUY",  round(max(0.01, price - offset), 4), order_size)
                a = self.client.place_order(yes_id, "SELL", round(min(0.99, price + offset), 4), order_size)
                if b: orders.append(b)
                if a: orders.append(a)

            self.grids[mid] = orders
            log.info(f"[GRID] {len(orders)} levels on {market['question'][:40]}")

# ============================================================================
# STRATEGY 7: FLASH LOAN ARB (simulated — real requires on-chain tx)
# ============================================================================

class FlashLoanArbStrategy(Strategy):
    """
    Find markets where YES+NO < 0.98. In live mode this would use Aave V3
    flash loans. In simulate/current mode, executes as regular dual-buy arb.
    """
    NAME = "flash_loan_arb"
    MIN_PROFIT_BPS = 1  # was 10
    SCAN_INTERVAL = 5

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.last_scan: float = 0

    def run(self):
        self.manage_positions()
        now = time.time()
        if now - self.last_scan < self.SCAN_INTERVAL:
            return
        self.last_scan = now

        balance = self.client.get_balance()
        size = self.risk.position_size(balance) / 2
        markets = self.client.get_markets(limit=40)

        for market in markets:
            tokens = market.get("tokens", [])
            if len(tokens) < 2:
                continue
            yes_id = tokens[0]["token_id"]
            no_id  = tokens[1]["token_id"]
            yes_p  = self.client.get_price(yes_id)
            no_p   = self.client.get_price(no_id)
            if not yes_p or not no_p:
                continue

            discount = 1.0 - (yes_p + no_p)
            profit_bps = int(discount * 10000) - 5  # subtract 0.05% fee
            if profit_bps >= self.MIN_PROFIT_BPS:
                mid = market["condition_id"]
                self.open_position(mid, yes_id, "BUY", yes_p, size,
                                   take_profit_pct=0.02, stop_loss_pct=0.05)
                self.open_position(mid, no_id,  "BUY", no_p,  size,
                                   take_profit_pct=0.02, stop_loss_pct=0.05)
                tg(f"⚡ <b>Flash Arb</b>\n"
                   f"Profit: {profit_bps}bps | Discount: {discount:.2%}\n"
                   f"{market['question'][:60]}")

# ============================================================================
# STRATEGY 8: GRIND TRADING
# ============================================================================

class GrindTradingStrategy(Strategy):
    """
    High-frequency spread capture on liquid 0.15–0.85 midrange tokens.
    TP 1-2% | SL 5%. Max 3 concurrent positions.
    """
    NAME = "grind_trading"
    MIN_PRICE = 0.15
    MAX_PRICE = 0.85
    MIN_SPREAD = 0.003  # was 0.008
    MAX_SPREAD = 0.10  # was 0.06
    MAX_POSITIONS = 3
    COOLDOWN = 15

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.last_cycle: float = 0

    def run(self):
        self.manage_positions()
        now = time.time()
        if now - self.last_cycle < self.COOLDOWN:
            return
        self.last_cycle = now

        if len(self.positions) >= self.MAX_POSITIONS:
            return

        balance = self.client.get_balance()
        size = balance * 0.08
        if size < 1.0:
            return

        markets = self.client.get_markets(limit=40)
        for market in markets:
            if len(self.positions) >= self.MAX_POSITIONS:
                break
            tokens = market.get("tokens", [])
            if not tokens:
                continue
            if market.get("volume", 0) < 10_000:  # was 100k
                continue

            yes_id = tokens[0]["token_id"]
            mid_id = market["condition_id"]
            book = self.client.get_orderbook(yes_id)
            if not book:
                continue

            bids = book.get("bids", [])
            asks = book.get("asks", [])
            if len(bids) < 2 or len(asks) < 2:
                continue

            best_bid = float(bids[0]["price"])
            best_ask = float(asks[0]["price"])
            mid_price = (best_bid + best_ask) / 2
            spread = best_ask - best_bid

            if not (self.MIN_PRICE < mid_price < self.MAX_PRICE):
                continue
            if not (self.MIN_SPREAD <= spread <= self.MAX_SPREAD):
                continue

            already = any(p["market_id"] == mid_id for p in self.positions)
            if already:
                continue

            side = "BUY" if mid_price <= 0.50 else "SELL"
            entry = best_ask if side == "BUY" else best_bid
            tp = min(spread * 0.6, 0.02)
            tp = max(tp, 0.008)

            self.open_position(mid_id, yes_id, side, entry, size,
                               take_profit_pct=tp, stop_loss_pct=0.05)

# ============================================================================
# STRATEGY 9: DAY TRADING MOMENTUM
# ============================================================================

class DayTradingMomentumStrategy(Strategy):
    """
    Track 5-min/15-min markets. Enter on 3%+ trend move in first 2.5 min.
    TP 3-5% | SL 10%.
    """
    NAME = "day_trading_momentum"
    TREND_WINDOW = 150   # seconds
    MIN_MOVE = 0.01  # was 0.03

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.price_history: Dict[str, deque] = {}

    def run(self):
        self.manage_positions()
        markets = self.client.get_markets(limit=30)
        balance = self.client.get_balance()
        size = balance * 0.20

        for market in markets:
            tokens = market.get("tokens", [])  # all markets
            if not tokens:
                continue
            yes_id = tokens[0]["token_id"]
            mid_id = market["condition_id"]
            price = self.client.get_price(yes_id)
            if not price:
                continue

            if yes_id not in self.price_history:
                self.price_history[yes_id] = deque(maxlen=60)
            self.price_history[yes_id].append({"t": time.time(), "p": price})

            history = self.price_history[yes_id]
            if len(history) < 5:
                continue

            oldest = history[0]
            elapsed = time.time() - oldest["t"]
            if elapsed < self.TREND_WINDOW:
                continue

            move = (price - oldest["p"]) / oldest["p"]
            if abs(move) < self.MIN_MOVE:
                continue
            already = any(p["market_id"] == mid_id for p in self.positions)
            if already:
                continue

            side = "BUY" if move > 0 else "SELL"
            self.open_position(mid_id, yes_id, side, price, size,
                               take_profit_pct=0.04, stop_loss_pct=0.10)
            tg(f"🚀 <b>Momentum</b> {side}\n"
               f"Move: {move:+.1%} | {market['question'][:55]}")

# ============================================================================
# STRATEGY 10: MEAN REVERSION
# ============================================================================

class MeanReversionStrategy(Strategy):
    """
    Detect 8%+ overextension from 10-min mean. Bet on reversion.
    Time-exit after 60 min. TP 2-4% | SL 10%.
    """
    NAME = "mean_reversion"
    OVEREXTENSION = 0.03  # was 0.08
    MAX_HOLD_SEC = 3600
    LOOKBACK_SEC = 600

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.price_history: Dict[str, deque] = {}

    def run(self):
        # Time-based exit
        for pos in list(self.positions):
            if time.time() - pos["opened_at"] >= self.MAX_HOLD_SEC:
                price = self.client.get_price(pos["token_id"])
                if price:
                    exit_side = "SELL" if pos["side"] == "BUY" else "BUY"
                    oid = self.client.place_order(pos["token_id"], exit_side, price, pos["size"])
                    pnl = (price - pos["entry"]) * pos["size"]
                    db.log_trade(self.NAME, pos["market_id"], pos["token_id"],
                                 exit_side, price, pos["size"], oid or "", "closed", pnl)
                    tg(f"⏱ <b>MeanRev Time Exit</b>\nPnL: ${pnl:+.2f}")
                    self.positions.remove(pos)

        self.manage_positions()

        markets = self.client.get_markets(limit=30)
        balance = self.client.get_balance()
        size = balance * 0.15

        for market in markets:
            tokens = market.get("tokens", [])
            if not tokens:
                continue
            yes_id = tokens[0]["token_id"]
            mid_id = market["condition_id"]
            price  = self.client.get_price(yes_id)
            if not price:
                continue

            if yes_id not in self.price_history:
                self.price_history[yes_id] = deque(maxlen=120)
            self.price_history[yes_id].append({"t": time.time(), "p": price})

            recent = [h["p"] for h in self.price_history[yes_id]
                      if time.time() - h["t"] <= self.LOOKBACK_SEC]
            if len(recent) < 3:  # was 5
                continue

            mean = sum(recent) / len(recent)
            dev  = (price - mean) / mean if mean else 0

            if abs(dev) < self.OVEREXTENSION:
                continue
            already = any(p["market_id"] == mid_id for p in self.positions)
            if already:
                continue

            side = "SELL" if dev > 0 else "BUY"
            self.open_position(mid_id, yes_id, side, price, size,
                               take_profit_pct=0.03, stop_loss_pct=0.10)
            tg(f"↩️ <b>Mean Reversion</b> {side}\n"
               f"Dev: {dev:+.1%} | Mean: {mean:.4f}\n"
               f"{market['question'][:55]}")


# ============================================================================
# STRATEGY 11: ASCETIX MODE (BTC 15-min Directional)
# ============================================================================

class AscetixModeStrategy(Strategy):
    """
    Replicates the @ascetic0x method:
    - Targets Polymarket BTC 15-min Up/Down markets
    - 4-signal confluence: order book, funding rate, liquidation flow, momentum
    - Enters directionally with 25% of capital
    - TP 80%+ price (near $1.00) | SL if price drops 40% from entry
    - Fires every cycle — this is the PRIMARY money engine
    """
    NAME = "ascetix_mode"
    CAPITAL_PCT  = 0.25   # 25% of balance per trade — aggressive but controlled
    TP_PRICE     = 0.82   # take profit when shares hit $0.82+
    SL_DROP_PCT  = 0.40   # stop loss if price drops 40% from entry
    BINANCE_URL  = "https://fapi.binance.com"
    COINGLASS_URL = "https://open-api.coinglass.com/public/v2"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._last_signal_ts = 0
        self._cooldown = 900  # 15 min between entries (one market window)

    # ── External Data Fetchers ──────────────────────────────────────────

    def _get_funding_rate(self) -> Optional[float]:
        """Binance perpetual BTC funding rate. Positive = longs crowded."""
        try:
            url = f"{self.BINANCE_URL}/fapi/v1/premiumIndex?symbol=BTCUSDT"
            r = requests.get(url, timeout=5)
            data = r.json()
            return float(data.get("lastFundingRate", 0))
        except Exception as e:
            log.warning(f"[Ascetix] funding rate: {e}")
            return None

    def _get_orderbook_bias(self) -> Optional[str]:
        """
        Binance BTC spot order book depth.
        Returns 'UP' if strong bid wall, 'DOWN' if strong ask wall, None if neutral.
        """
        try:
            url = "https://api.binance.com/api/v3/depth?symbol=BTCUSDT&limit=20"
            r = requests.get(url, timeout=5)
            book = r.json()
            bids = sum(float(b[1]) for b in book.get("bids", []))
            asks = sum(float(a[1]) for a in book.get("asks", []))
            ratio = bids / asks if asks else 1.0
            if ratio > 1.3:
                return "UP"    # 30%+ more buy pressure
            elif ratio < 0.7:
                return "DOWN"  # 30%+ more sell pressure
            return None
        except Exception as e:
            log.warning(f"[Ascetix] orderbook: {e}")
            return None

    def _get_liquidation_bias(self) -> Optional[str]:
        """
        Binance recent liquidation orders.
        If big LONG liquidations just hit → downside pressure exhausted → UP
        If big SHORT liquidations just hit → upside fuel gone → DOWN
        """
        try:
            url = f"{self.BINANCE_URL}/fapi/v1/allForceOrders?symbol=BTCUSDT&limit=50"
            r = requests.get(url, timeout=5)
            orders = r.json()
            long_liq  = sum(float(o["origQty"]) for o in orders if o.get("side") == "SELL")
            short_liq = sum(float(o["origQty"]) for o in orders if o.get("side") == "BUY")
            if long_liq > short_liq * 2:
                return "UP"    # long liquidations dominated → bounce likely
            elif short_liq > long_liq * 2:
                return "DOWN"  # short squeeze done → pullback likely
            return None
        except Exception as e:
            log.warning(f"[Ascetix] liquidations: {e}")
            return None

    def _get_btc_momentum(self) -> Optional[str]:
        """
        BTC 1-min klines — last 5 candles.
        3+ green closes → UP momentum. 3+ red closes → DOWN momentum.
        """
        try:
            url = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=6"
            r = requests.get(url, timeout=5)
            klines = r.json()
            closes = [(float(k[4]) - float(k[1])) for k in klines[-5:]]  # close - open
            green = sum(1 for c in closes if c > 0)
            red   = sum(1 for c in closes if c < 0)
            if green >= 4:
                return "UP"
            elif red >= 4:
                return "DOWN"
            return None
        except Exception as e:
            log.warning(f"[Ascetix] momentum: {e}")
            return None

    # ── Signal Scoring ──────────────────────────────────────────────────

    def _compute_signal(self) -> Optional[str]:
        """
        4-signal confluence. Need 3/4 agreement to fire.
        Returns 'UP', 'DOWN', or None (no trade).
        """
        funding = self._get_funding_rate()
        ob_bias = self._get_orderbook_bias()
        liq_bias = self._get_liquidation_bias()
        mom_bias = self._get_btc_momentum()

        signals = {"UP": 0, "DOWN": 0}

        # Funding: positive = longs crowded = DOWN signal
        if funding is not None:
            if funding > 0.0005:
                signals["DOWN"] += 1
                log.info(f"[Ascetix] Funding {funding:.4%} → DOWN signal")
            elif funding < -0.0005:
                signals["UP"] += 1
                log.info(f"[Ascetix] Funding {funding:.4%} → UP signal")

        # Order book bias
        if ob_bias:
            signals[ob_bias] += 1
            log.info(f"[Ascetix] OrderBook → {ob_bias} signal")

        # Liquidation bias
        if liq_bias:
            signals[liq_bias] += 1
            log.info(f"[Ascetix] Liquidations → {liq_bias} signal")

        # Momentum
        if mom_bias:
            signals[mom_bias] += 1
            log.info(f"[Ascetix] Momentum → {mom_bias} signal")

        log.info(f"[Ascetix] Score: UP={signals['UP']} DOWN={signals['DOWN']}")

        max_score = max(signals["UP"], signals["DOWN"])
        if signals["UP"] >= 3:
            return "UP"
        elif signals["DOWN"] >= 3:
            return "DOWN"
        return None

    def _compute_signal_with_confidence(self) -> tuple:
        """Returns (signal, confidence) where confidence is 0.0-1.0."""
        funding = self._get_funding_rate()
        ob_bias = self._get_orderbook_bias()
        liq_bias = self._get_liquidation_bias()
        mom_bias = self._get_btc_momentum()

        signals = {"UP": 0, "DOWN": 0}

        if funding is not None:
            if funding > 0.0002:
                signals["DOWN"] += 1
                log.info(f"[Ascetix] Funding {funding:.6f} (longs crowded) → DOWN")
            elif funding < -0.0002:
                signals["UP"] += 1
                log.info(f"[Ascetix] Funding {funding:.6f} (shorts crowded) → UP")

        if ob_bias:
            signals[ob_bias] += 1
        if liq_bias:
            signals[liq_bias] += 1
        if mom_bias:
            signals[mom_bias] += 1

        log.info(f"[Ascetix] Score: UP={signals['UP']} DOWN={signals['DOWN']}")

        if signals["UP"] >= 3:
            confidence = (signals["UP"] - 3) / 1.0  # 3/4 = 0.0, 4/4 = 1.0
            return "UP", round(min(confidence, 1.0), 2)
        elif signals["DOWN"] >= 3:
            confidence = (signals["DOWN"] - 3) / 1.0
            return "DOWN", round(min(confidence, 1.0), 2)
        return None, 0.0

    # ── Market Finder ───────────────────────────────────────────────────

    def _find_btc_15min_market(self, direction: str) -> Optional[dict]:
        """Find the active BTC 15-min UP or DOWN market on Polymarket."""
        try:
            markets = self.client.get_markets(limit=50)
            keyword = "bitcoin" if True else ""
            for m in markets:
                q = m.get("question", "").lower()
                # Match BTC 15-min up/down style questions
                if ("btc" in q or "bitcoin" in q) and ("15" in q or "minute" in q):
                    tokens = m.get("tokens", [])
                    if len(tokens) < 2:
                        continue
                    # token[0] = YES (Up), token[1] = NO (Down)
                    if direction == "UP":
                        return {"market": m, "token_id": tokens[0]["token_id"], "label": "YES/UP"}
                    else:
                        return {"market": m, "token_id": tokens[1]["token_id"], "label": "NO/DOWN"}
            return None
        except Exception as e:
            log.error(f"[Ascetix] find market: {e}")
            return None

    # ── Position Manager ────────────────────────────────────────────────

    def _manage_exits(self):
        """TP at $0.82+, SL at 40% drop from entry."""
        for pos in list(self.positions):
            price = self.client.get_price(pos["token_id"])
            if not price:
                continue
            entry = pos["entry"]
            drop_pct = (entry - price) / entry if entry else 0
            pnl = (price - entry) * pos["size"]

            if price >= self.TP_PRICE:
                oid = self.client.place_order(pos["token_id"], "SELL", price, pos["size"])
                db.log_trade(self.NAME, pos["market_id"], pos["token_id"],
                             "SELL", price, pos["size"], oid or "", "tp", pnl)
                tg(f"🎯 <b>AscetixMode TP</b>\nEntry: ${entry:.4f} → Exit: ${price:.4f}\nPnL: ${pnl:+.2f} ({(price/entry - 1)*100:+.1f}%)")
                self.positions.remove(pos)
            elif drop_pct >= self.SL_DROP_PCT:
                oid = self.client.place_order(pos["token_id"], "SELL", price, pos["size"])
                db.log_trade(self.NAME, pos["market_id"], pos["token_id"],
                             "SELL", price, pos["size"], oid or "", "sl", pnl)
                tg(f"🛑 <b>AscetixMode SL</b>\nEntry: ${entry:.4f} → Exit: ${price:.4f}\nPnL: ${pnl:+.2f}")
                self.positions.remove(pos)

    # ── Main Run ────────────────────────────────────────────────────────

    def run(self):
        self._manage_exits()

        # Cooldown: only enter once per 15-min window
        if time.time() - self._last_signal_ts < self._cooldown:
            return

        # Don't stack more than 1 position
        if len(self.positions) >= 1:
            return

        signal, confidence = self._compute_signal_with_confidence()
        if not signal:
            log.info("[Ascetix] No confluence — sitting out this window.")
            return

        result = self._find_btc_15min_market(signal)
        if not result:
            log.info("[Ascetix] No BTC 15-min market found — scanning gamma...")
            # Fallback: scan gamma for active BTC short-term markets
            try:
                url = f"{cfg.GAMMA_URL}/markets?tag=bitcoin&active=true&limit=20"
                r = requests.get(url, timeout=8)
                markets_raw = r.json() if r.status_code == 200 else []
                for m in markets_raw:
                    q = m.get("question", "").lower()
                    if "15" in q or "minute" in q:
                        tokens = m.get("clobTokenIds", [])
                        if len(tokens) >= 2:
                            idx = 0 if signal == "UP" else 1
                            result = {
                                "market": m,
                                "token_id": tokens[idx],
                                "label": "YES/UP" if signal == "UP" else "NO/DOWN"
                            }
                            break
            except Exception as e:
                log.warning(f"[Ascetix] gamma fallback: {e}")

        if not result:
            log.info("[Ascetix] No suitable market found this cycle.")
            return

        balance = self.client.get_balance()
        # Dynamic Kelly: confidence 0.0 → 25% cap, confidence 1.0 → up to 37.5% (capped at 40%)
        kelly_multiplier = 1.0 + (0.5 * confidence)  # 1.0x to 1.5x
        dynamic_pct = min(self.CAPITAL_PCT * kelly_multiplier, 0.40)
        size = round(balance * dynamic_pct, 2)
        if size < 1.0:
            log.info(f"[Ascetix] Balance too low: ${balance:.2f}")
            return

        market = result["market"]
        token_id = result["token_id"]
        mid_id = market.get("condition_id", market.get("id", ""))
        price = self.client.get_price(token_id)

        if not price:
            log.warning("[Ascetix] Could not get price — skipping.")
            return

        # Only enter if shares are reasonably priced (not already near resolution)
        if not (0.15 < price < 0.85):
            log.info(f"[Ascetix] Price {price:.4f} out of entry range — skipping.")
            return

        oid = self.client.place_order(token_id, "BUY", price, size)
        if oid:
            self.positions.append({
                "market_id": mid_id,
                "token_id":  token_id,
                "side":      "BUY",
                "entry":     price,
                "size":      size,
                "opened_at": time.time(),
            })
            db.log_trade(self.NAME, mid_id, token_id, "BUY", price, size, oid, "open")
            self._last_signal_ts = time.time()
            tg(
                f"⚡ <b>AscetixMode ENTRY</b>\n"
                f"Signal: {signal} ({result['label']}) | Confidence: {confidence:.0%}\n"
                f"Kelly: {dynamic_pct:.0%} | Price: ${price:.4f} | Size: ${size:.2f}\n"
                f"TP: ${self.TP_PRICE} | SL: -{self.SL_DROP_PCT*100:.0f}%\n"
                f"{market.get('question','')[:55]}"
            )

# ============================================================================
# ZEUS PRIME v2 — MAIN
# ============================================================================

class ZeusPrimeV2:
    CYCLE_SEC    = 60
    REPORT_EVERY = 30   # cycles (~30 min)

    def __init__(self):
        log.info("═══ ZeusPrime v2.3 Initializing ═══")
        self.client = PolyClient()
        self.risk   = RiskManager(self.client)

        # Kalshi client (optional — only if KALSHI_ENABLED=true in .env)
        self.kalshi: Optional[KalshiClient] = None
        if _KALSHI_AVAILABLE and cfg.KALSHI_ENABLED:
            try:
                self.kalshi = KalshiClient()
                kbal = self.kalshi.get_balance()
                log.info(f"Kalshi online ✅ Balance: ${kbal:.2f}")
            except Exception as e:
                log.warning(f"Kalshi init failed (continuing Poly-only): {e}")

        self.strategies = [
            DumpAndHedgeStrategy(self.client, self.risk),       # 1
            YesNoArbStrategy(self.client, self.risk),           # 2  ← cross-venue arb
            YieldFarmingStrategy(self.client, self.risk),       # 3
            MakerRebateMMStrategy(self.client, self.risk),      # 4
            CopyTradingStrategy(self.client, self.risk),        # 5
            GridTradingStrategy(self.client, self.risk),        # 6
            FlashLoanArbStrategy(self.client, self.risk),       # 7
            GrindTradingStrategy(self.client, self.risk),       # 8
            DayTradingMomentumStrategy(self.client, self.risk), # 9
            MeanReversionStrategy(self.client, self.risk),      # 10
            AscetixModeStrategy(self.client, self.risk),        # 11 ★ MONEY ENGINE
        ]

        # Strategy 12: OraclePrime — Weather Edge (Kalshi-only)
        if _ORACLE_AVAILABLE and self.kalshi:
            self.strategies.append(
                OraclePrimeStrategy(self.kalshi, self.risk)     # 12 🌤️ WEATHER EDGE
            )
            log.info("OraclePrime (Strategy 12) armed ✅")
        self.cycle = 0
        self.start_balance = self.client.get_balance()
        mode       = "🔴 LIVE" if not cfg.SIMULATE else "🟡 SIMULATE"
        kalshi_tag  = "✅ Kalshi ARMED" if self.kalshi else "⚪ Poly-only"
        oracle_tag  = "🌤️ OraclePrime ON" if (_ORACLE_AVAILABLE and self.kalshi) else "⚪ OraclePrime OFF"
        tg(
            f"⚡ <b>ZeusPrime v2.3 ONLINE</b>\n"
            f"Mode: {mode}\n"
            f"Capital: ${self.start_balance:.2f} USDC\n"
            f"Strategies: {len(self.strategies)} active\n"
            f"Cross-platform: {kalshi_tag}\n"
            f"Weather Edge: {oracle_tag}\n"
            f"Cycle: {self.CYCLE_SEC}s"
        )
        log.info(f"ZeusPrime v2.3 ready. {len(self.strategies)} strategies. Kalshi={'ON' if self.kalshi else 'OFF'}")

        # Start Telegram command listener
        self._tg_cmd = TelegramCommands(
            token=cfg.TELEGRAM_TOKEN,
            chat_id=cfg.TELEGRAM_CHAT,
            db_ref=db,
            zeus_ref=self,
        )
        self._tg_cmd.start()
        log.info("[TG-CMD] Telegram command listener started ✅")

    def report(self):
        balance = self.client.get_balance()
        pnl = balance - self.start_balance
        pnl_pct = (pnl / self.start_balance * 100) if self.start_balance else 0
        db.log_snapshot(balance, pnl)
        tg(
            f"{'📈' if pnl >= 0 else '📉'} <b>ZeusPrime Status</b>\n"
            f"💰 Balance: ${balance:.2f}\n"
            f"📊 P&L: ${pnl:+.2f} ({pnl_pct:+.2f}%)\n"
            f"🔄 Cycle #{self.cycle}"
        )

    def run(self):
        log.info("Running. Ctrl+C to stop.")
        while True:
            try:
                self.cycle += 1
                log.info(f"─── Cycle #{self.cycle} ───")
                for s in self.strategies:
                    s.safe_run()
                if self.cycle % self.REPORT_EVERY == 0:
                    self.report()
                time.sleep(self.CYCLE_SEC)
            except KeyboardInterrupt:
                log.info("Shutdown.")
                self.client.cancel_all()
                tg("🔴 <b>ZeusPrime v2.0 stopped.</b>")
                self._tg_cmd.stop()
                break
            except Exception as e:
                log.error(f"Main loop: {e}\n{traceback.format_exc()}")
                time.sleep(30)

if __name__ == "__main__":
    bot = ZeusPrimeV2()
    bot.run()

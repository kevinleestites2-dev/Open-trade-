#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════╗
║              ARB PRIME v2.0 — The Flash Loan Scout                  ║
║              DEX Arbitrage Scanner for Furucombo                    ║
║              Powered by DexPaprika API (free, no key)               ║
║              Polygon: Uniswap V3 vs QuickSwap V2 vs SushiSwap       ║
╚══════════════════════════════════════════════════════════════════════╝
"""

import os
import json
import time
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

# ── Bootstrap ──────────────────────────────────────────────────────────────
BASE_DIR  = Path(__file__).parent.resolve()
LOGS_DIR  = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)
load_dotenv(BASE_DIR / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [ARB-PRIME] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOGS_DIR / "arb_prime.log"),
    ],
)
log = logging.getLogger("ArbPrime")

# ── Config ─────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT  = os.getenv("TELEGRAM_CHAT_ID", "")
SIMULATE       = os.getenv("SIMULATE_MODE", "true").lower() == "true"
SCAN_INTERVAL  = int(os.getenv("ARB_SCAN_INTERVAL", "30"))
MIN_PROFIT_PCT = float(os.getenv("ARB_MIN_PROFIT_PCT", "0.3"))
FLASH_LOAN_FEE = 0.09
GAS_BUFFER     = 0.10

# ── DexPaprika API ─────────────────────────────────────────────────────────
DEXPAPRIKA_BASE = "https://api.dexpaprika.com"
NETWORK         = "polygon"

DEX_LIST = [
    ("uniswap_v3",   "Uniswap V3"),
    ("quickswap_v2", "QuickSwap V2"),
    ("sushiswap",    "SushiSwap"),
]

TOKENS = {
    "WPOL":  "0x0d500b1d8e8ef31e21c99d1db9a6444d3adf1270",
    "WETH":  "0x7ceb23fd6bc0add59e62ac25578270cff1b9f619",
    "WBTC":  "0x1bfd67037b42cf73acf2047067bd4f2c47d9bfd6",
    "USDC":  "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359",
    "USDT0": "0xc2132d05d31c914a87c6611c10748aeb04b58e8f",  # USDT on Polygon (USDT0)
    "DAI":   "0x8f3cf7ad23cd3cadbd9735aff958023239c6a063",
}

WATCH_PAIRS = [
    ("WPOL",  "USDT0"),
    ("WETH",  "USDT0"),
    ("WBTC",  "USDC"),
    ("WPOL",  "WETH"),
    ("WETH",  "DAI"),
    ("DAI",   "USDT0"),
]


# ── Price Fetcher ──────────────────────────────────────────────────────────

def get_pool_for_pair(dex_id: str, token_a: str, token_b: str) -> Optional[dict]:
    """Find highest-volume pool containing both tokens on given DEX."""
    addr_a = TOKENS[token_a].lower()
    addr_b = TOKENS[token_b].lower()
    url    = f"{DEXPAPRIKA_BASE}/networks/{NETWORK}/dexes/{dex_id}/pools"
    # NOTE: do NOT pass limit/sort params — they break the API and return 0 pools
    try:
        r = requests.get(url, timeout=12)
        r.raise_for_status()
        best = None
        best_vol = 0.0
        for pool in r.json().get("pools", []):
            ids = [t["id"].lower() for t in pool.get("tokens", [])]
            if addr_a in ids and addr_b in ids:
                vol = pool.get("volume_usd", 0) or 0
                if best is None or vol > best_vol:
                    best = pool
                    best_vol = vol
        return best
    except Exception as e:
        log.debug(f"Pool fetch error ({dex_id}): {e}")
    return None


def get_pair_price(dex_id: str, token_a: str, token_b: str) -> Optional[float]:
    """
    Returns price_usd of the pool's token0.
    We compare the same field across DEXes — a mismatch = arb opportunity.
    """
    pool = get_pool_for_pair(dex_id, token_a, token_b)
    if not pool:
        return None
    price = pool.get("price_usd")
    if price and float(price) > 0:
        return float(price)
    return None


# ── Scanner ────────────────────────────────────────────────────────────────

def scan_pair(token_a: str, token_b: str) -> Optional[dict]:
    symbol = f"{token_a}/{token_b}"
    log.info(f"Scanning {symbol}...")

    prices = {}
    for dex_id, dex_name in DEX_LIST:
        p = get_pair_price(dex_id, token_a, token_b)
        if p:
            prices[dex_name] = p
            log.info(f"  {dex_name}: ${p:.6f}")
        else:
            log.info(f"  {dex_name}: no data")

    if len(prices) < 2:
        log.info(f"  {symbol}: need 2+ DEXes with data — skipping")
        return None

    names = list(prices.keys())
    best_opp    = None
    best_spread = 0.0

    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            na, pa = names[i], prices[names[i]]
            nb, pb = names[j], prices[names[j]]
            spread = abs(pa - pb) / min(pa, pb) * 100
            net    = spread - FLASH_LOAN_FEE - GAS_BUFFER
            if spread > best_spread:
                best_spread = spread
                buy_name,  buy_p  = (na, pa) if pa < pb else (nb, pb)
                sell_name, sell_p = (nb, pb) if pa < pb else (na, pa)
                best_opp = {
                    "timestamp":      datetime.now(timezone.utc).isoformat(),
                    "symbol":         symbol,
                    "token_a":        token_a,
                    "token_b":        token_b,
                    "buy_dex":        buy_name,
                    "buy_price":      buy_p,
                    "sell_dex":       sell_name,
                    "sell_price":     sell_p,
                    "spread_pct":     round(spread, 4),
                    "net_profit_pct": round(net, 4),
                    "all_prices":     dict(prices),
                    "furucombo_url":  "https://furucombo.app/combo?chain=polygon",
                }

    if not best_opp:
        return None

    log.info(f"  Spread: {best_opp['spread_pct']:.4f}% | Net: {best_opp['net_profit_pct']:.4f}%")

    if best_opp["net_profit_pct"] < MIN_PROFIT_PCT:
        log.info(f"  Below threshold ({MIN_PROFIT_PCT}%) — no trade")
        return None

    log.info(f"  OPPORTUNITY: buy {best_opp['buy_dex']} | sell {best_opp['sell_dex']}")
    best_opp["instructions"] = build_instructions(best_opp)
    return best_opp


def build_instructions(opp: dict) -> str:
    ta  = opp["token_a"]
    tb  = opp["token_b"]
    net = opp["net_profit_pct"]
    est = 1000 * (net / 100)
    return (
        f"FURUCOMBO SETUP ({opp['symbol']}):\n"
        f"1. Aave V3 Flash Loan: 1000 {ta}\n"
        f"2. {opp['buy_dex']}: Swap {ta} → {tb}\n"
        f"3. {opp['sell_dex']}: Swap {tb} → {ta}\n"
        f"4. Aave V3: Return loan + 0.09% fee\n"
        f"Est. Profit: ~${est:.2f} on $1,000 flash loan"
    )


# ── Telegram ───────────────────────────────────────────────────────────────

def send_telegram(message: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id":    TELEGRAM_CHAT,
            "text":       message,
            "parse_mode": "HTML",
        }, timeout=10)
        return r.status_code == 200
    except Exception as e:
        log.error(f"Telegram error: {e}")
        return False


def fire_alert(opp: dict):
    prices_str = " | ".join([f"{k}: ${v:.4f}" for k, v in opp["all_prices"].items()])
    msg = (
        f"ARBITRAGE ALERT\n\n"
        f"Pair: {opp['symbol']}\n"
        f"BUY:  {opp['buy_dex']} @ ${opp['buy_price']:.6f}\n"
        f"SELL: {opp['sell_dex']} @ ${opp['sell_price']:.6f}\n"
        f"Spread: {opp['spread_pct']:.3f}% | Net: {opp['net_profit_pct']:.3f}%\n\n"
        f"Prices: {prices_str}\n\n"
        f"Furucombo: {opp['furucombo_url']}\n\n"
        f"{opp['instructions']}\n\n"
        f"{datetime.now(timezone.utc).strftime('%H:%M:%S UTC')} | ArbPrime v2.0"
    )
    if SIMULATE:
        log.info(f"\n{'='*60}\n[SIMULATE ALERT]\n{msg}\n{'='*60}")
    else:
        ok = send_telegram(msg)
        log.info(f"  Telegram sent: {ok}")


# ── Log ────────────────────────────────────────────────────────────────────

ARB_LOG = LOGS_DIR / "arb_log.json"

def log_opportunity(opp: dict):
    existing = []
    if ARB_LOG.exists():
        try:
            existing = json.loads(ARB_LOG.read_text())
        except Exception:
            existing = []
    existing.append(opp)
    ARB_LOG.write_text(json.dumps(existing, indent=2))


# ── Main ───────────────────────────────────────────────────────────────────

def run():
    mode = "SIMULATE" if SIMULATE else "LIVE"
    log.info(
        f"\n{'='*60}\n"
        f"  ARB PRIME v2.0 [{mode}]\n"
        f"  DexPaprika engine | {len(WATCH_PAIRS)} pairs | {len(DEX_LIST)} DEXes\n"
        f"  Scan every {SCAN_INTERVAL}s | Min net profit: {MIN_PROFIT_PCT}%\n"
        f"{'='*60}\n"
    )

    if not SIMULATE:
        send_telegram("ArbPrime v2.0 ONLINE — DexPaprika engine. Scanning Polygon DEXes.")

    scan_count = 0
    opps_found = 0

    while True:
        scan_count += 1
        log.info(f"\n── Scan #{scan_count} ──────────────────────────────────────")

        for token_a, token_b in WATCH_PAIRS:
            try:
                opp = scan_pair(token_a, token_b)
                if opp:
                    opps_found += 1
                    log_opportunity(opp)
                    fire_alert(opp)
            except Exception as e:
                log.error(f"Error scanning {token_a}/{token_b}: {e}")

        log.info(f"\n── Scans={scan_count} | Total Opps={opps_found} | Next in {SCAN_INTERVAL}s")
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    run()

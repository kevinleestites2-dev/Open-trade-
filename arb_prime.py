#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════╗
║              ARB PRIME v2.1 — The Flash Loan Scout                  ║
║              DEX Arbitrage Scanner for Furucombo                    ║
║              Powered by DexPaprika API (free, no key)               ║
║              Polygon: Uniswap V3 / QuickSwap / SushiSwap / Balancer ║
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

# Minimum NET profit % after ALL fees before firing an alert
# Set to 0.10% — means the raw spread must be ~0.74%+ to survive fees
MIN_PROFIT_PCT = float(os.getenv("ARB_MIN_PROFIT_PCT", "0.10"))

# Flash loan + estimated gas (flat %)
FLASH_LOAN_FEE = 0.09   # Aave V3: 0.09%
GAS_BUFFER_PCT = 0.05   # Polygon gas ~$0.50 on $1000 loan = 0.05%

# ── DEX LP Fee Table ───────────────────────────────────────────────────────
# Real LP fees charged per swap on each DEX.
# These are deducted from gross spread to compute real profitability.
# Uniswap V3 has multiple tiers — we use the dominant pool's tier.
# Note: price_usd from DexPaprika is the MID price, not post-fee.
DEX_LP_FEES = {
    "Uniswap V3":   0.30,  # dominant WPOL/WETH pool is 0.30% tier
    "QuickSwap V2": 0.30,  # standard V2 AMM
    "QuickSwap V3": 0.15,  # QuickSwap V3 default fee
    "SushiSwap":    0.30,  # standard SushiSwap
    "Balancer V2":  0.10,  # Balancer weighted pools avg
}

# ── DexPaprika API ─────────────────────────────────────────────────────────
DEXPAPRIKA_BASE = "https://api.dexpaprika.com"
NETWORK         = "polygon"

DEX_LIST = [
    ("uniswap_v3",   "Uniswap V3"),
    ("quickswap_v2", "QuickSwap V2"),
    ("quickswap_v3", "QuickSwap V3"),
    ("sushiswap",    "SushiSwap"),
    ("balancer_v2",  "Balancer V2"),
]

TOKENS = {
    "WPOL":  "0x0d500b1d8e8ef31e21c99d1db9a6444d3adf1270",
    "WETH":  "0x7ceb23fd6bc0add59e62ac25578270cff1b9f619",
    "WBTC":  "0x1bfd67037b42cf73acf2047067bd4f2c47d9bfd6",
    "USDC":  "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359",
    "USDT0": "0xc2132d05d31c914a87c6611c10748aeb04b58e8f",
    "DAI":   "0x8f3cf7ad23cd3cadbd9735aff958023239c6a063",
}

WATCH_PAIRS = [
    ("WPOL",  "USDT0"),
    ("WETH",  "USDT0"),
    ("WBTC",  "USDC"),
    ("WBTC",  "WETH"),
    ("WPOL",  "WETH"),
    ("WETH",  "DAI"),
    ("DAI",   "USDT0"),
    ("WPOL",  "USDC"),
]


# ── Price Fetcher ──────────────────────────────────────────────────────────

def get_pool_for_pair(dex_id: str, token_a: str, token_b: str) -> Optional[dict]:
    """
    Find highest-volume EXACTLY 2-token pool containing both tokens on given DEX.
    Multi-token pools (Balancer 4/8-token) are excluded — their price_usd is
    not comparable to standard 2-token AMM pools and produces false arb signals.
    NOTE: do NOT pass limit/sort params — they break the API (returns 0 pools).
    """
    addr_a = TOKENS[token_a].lower()
    addr_b = TOKENS[token_b].lower()
    url    = f"{DEXPAPRIKA_BASE}/networks/{NETWORK}/dexes/{dex_id}/pools"
    try:
        r = requests.get(url, timeout=12)
        r.raise_for_status()
        best = None
        best_vol = 0.0
        for pool in r.json().get("pools", []):
            tokens = pool.get("tokens", [])
            # STRICT: only 2-token pools
            if len(tokens) != 2:
                continue
            ids = [t["id"].lower() for t in tokens]
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
    """Returns price_usd of the pool for comparison across DEXes."""
    pool = get_pool_for_pair(dex_id, token_a, token_b)
    if not pool:
        return None
    price = pool.get("price_usd")
    if price and float(price) > 0:
        return float(price)
    return None


def calc_net_profit_pct(
    gross_spread_pct: float,
    buy_dex_name: str,
    sell_dex_name: str,
) -> float:
    """
    Real net profit after:
      - Aave V3 flash loan fee (0.09%)
      - Buy DEX LP fee
      - Sell DEX LP fee
      - Gas buffer (0.05%)
    All as percentages of loan amount.
    """
    lp_buy  = DEX_LP_FEES.get(buy_dex_name,  0.30)
    lp_sell = DEX_LP_FEES.get(sell_dex_name, 0.30)
    total_cost = FLASH_LOAN_FEE + lp_buy + lp_sell + GAS_BUFFER_PCT
    return gross_spread_pct - total_cost


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
    best_net    = -999.0

    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            na, pa = names[i], prices[names[i]]
            nb, pb = names[j], prices[names[j]]

            gross_pct = abs(pa - pb) / min(pa, pb) * 100
            buy_name,  buy_p  = (na, pa) if pa < pb else (nb, pb)
            sell_name, sell_p = (nb, pb) if pa < pb else (na, pa)

            net_pct = calc_net_profit_pct(gross_pct, buy_name, sell_name)

            if net_pct > best_net:
                best_net = net_pct
                lp_buy  = DEX_LP_FEES.get(buy_name,  0.30)
                lp_sell = DEX_LP_FEES.get(sell_name, 0.30)
                best_opp = {
                    "timestamp":      datetime.now(timezone.utc).isoformat(),
                    "symbol":         symbol,
                    "token_a":        token_a,
                    "token_b":        token_b,
                    "buy_dex":        buy_name,
                    "buy_price":      buy_p,
                    "sell_dex":       sell_name,
                    "sell_price":     sell_p,
                    "gross_pct":      round(gross_pct, 4),
                    "net_profit_pct": round(net_pct, 4),
                    "fee_breakdown": {
                        "flash_loan":  FLASH_LOAN_FEE,
                        "lp_buy":      lp_buy,
                        "lp_sell":     lp_sell,
                        "gas_buffer":  GAS_BUFFER_PCT,
                        "total_cost":  round(FLASH_LOAN_FEE + lp_buy + lp_sell + GAS_BUFFER_PCT, 4),
                    },
                    "all_prices":     dict(prices),
                    "furucombo_url":  "https://furucombo.app/combo?chain=polygon",
                }

    if not best_opp:
        return None

    log.info(
        f"  Gross: {best_opp['gross_pct']:.3f}% | "
        f"Fees: {best_opp['fee_breakdown']['total_cost']:.3f}% | "
        f"Net: {best_opp['net_profit_pct']:.3f}%"
    )

    if best_opp["net_profit_pct"] < MIN_PROFIT_PCT:
        log.info(f"  Below threshold ({MIN_PROFIT_PCT}%) — no alert")
        # Near-miss diagnostic: log if within 2x of threshold (warming up the engine)
        if best_opp["net_profit_pct"] > 0 and best_opp["net_profit_pct"] >= MIN_PROFIT_PCT * 0.5:
            log.info(
                f"  📊 NEAR-MISS: {best_opp['symbol']} | "
                f"net={best_opp['net_profit_pct']:.4f}% (threshold={MIN_PROFIT_PCT}%) | "
                f"buy={best_opp['buy_dex']} sell={best_opp['sell_dex']}"
            )
        return None

    log.info(f"  OPPORTUNITY: buy {best_opp['buy_dex']} | sell {best_opp['sell_dex']}")
    best_opp["instructions"] = build_instructions(best_opp)
    return best_opp


def build_instructions(opp: dict) -> str:
    ta    = opp["token_a"]
    tb    = opp["token_b"]
    net   = opp["net_profit_pct"]
    gross = opp["gross_pct"]
    fees  = opp["fee_breakdown"]
    est   = 1000 * (net / 100)
    est10k = 10000 * (net / 100)
    return (
        f"FURUCOMBO SETUP ({opp['symbol']}):\n"
        f"1. Aave V3 Flash Loan: 1000 {ta}\n"
        f"2. {opp['buy_dex']}: Swap {ta} → {tb}\n"
        f"3. {opp['sell_dex']}: Swap {tb} → {ta}\n"
        f"4. Aave V3: Return loan + 0.09% fee\n\n"
        f"Fee Breakdown:\n"
        f"  Gross spread:  {gross:.3f}%\n"
        f"  Flash loan:   -{fees['flash_loan']:.2f}%\n"
        f"  LP buy:       -{fees['lp_buy']:.2f}%\n"
        f"  LP sell:      -{fees['lp_sell']:.2f}%\n"
        f"  Gas buffer:   -{fees['gas_buffer']:.2f}%\n"
        f"  NET:           {net:.3f}%\n\n"
        f"Est. Profit: ~${est:.2f} on $1,000 | ~${est10k:.2f} on $10,000"
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
        f"Gross: {opp['gross_pct']:.3f}% | Net: {opp['net_profit_pct']:.3f}%\n\n"
        f"Prices: {prices_str}\n\n"
        f"Furucombo: {opp['furucombo_url']}\n\n"
        f"{opp['instructions']}\n\n"
        f"{datetime.now(timezone.utc).strftime('%H:%M:%S UTC')} | ArbPrime v2.1"
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
        f"  ARB PRIME v2.1 [{mode}]\n"
        f"  DexPaprika engine | {len(WATCH_PAIRS)} pairs | {len(DEX_LIST)} DEXes\n"
        f"  Scan every {SCAN_INTERVAL}s | Min NET profit: {MIN_PROFIT_PCT}%\n"
        f"  Fee model: flash 0.09% + LP buy + LP sell + gas 0.05%\n"
        f"{'='*60}\n"
    )

    if not SIMULATE:
        send_telegram("ArbPrime v2.1 ONLINE — Real fee-aware arb scanner. Polygon DEXes.")

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

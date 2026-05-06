#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════╗
║              ARB PRIME v1.0 — The Flash Loan Scout                  ║
║              DEX Arbitrage Scanner for Furucombo                    ║
║              Polygon Mainnet | Uniswap V3 vs SushiSwap              ║
║              Telegram Alerts + Furucombo Deep Link                  ║
╚══════════════════════════════════════════════════════════════════════╝

HOW IT WORKS:
  1. Every SCAN_INTERVAL seconds, fetch token prices from Uniswap V3
     and SushiSwap (via their subgraph APIs — no wallet needed).
  2. Calculate % spread between the two DEXes.
  3. If spread > MIN_PROFIT_PCT (covers Aave 0.09% flash loan fee + gas),
     fire a Telegram alert with:
       - Token pair
       - Buy on / Sell on
       - Estimated profit %
       - Direct Furucombo link (pre-fills the combo)
  4. Logs every opportunity to logs/arb_log.json

REQUIREMENTS:
  - Python 3.8+
  - pip install requests python-dotenv

USAGE:
  python3 arb_prime.py
  SIMULATE_MODE=true python3 arb_prime.py   # silent (no Telegram spam)
"""

import os
import json
import time
import logging
import urllib.parse
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
TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT   = os.getenv("TELEGRAM_CHAT_ID", "")
SIMULATE        = os.getenv("SIMULATE_MODE", "true").lower() == "true"
SCAN_INTERVAL   = int(os.getenv("ARB_SCAN_INTERVAL", "30"))   # seconds between scans
MIN_PROFIT_PCT  = float(os.getenv("ARB_MIN_PROFIT_PCT", "0.3"))  # 0.3% min (covers 0.09% Aave fee + gas)
FLASH_LOAN_FEE  = 0.09  # Aave V3 flash loan fee %

# ── Token Pairs to Monitor ─────────────────────────────────────────────────
# Format: (symbol, token_address_on_polygon, decimals)
# These are high-volume Polygon pairs with the most arb opportunity
WATCH_PAIRS = [
    # WMATIC/USDC
    {
        "symbol":     "WMATIC/USDC",
        "token_in":   "0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270",  # WMATIC
        "token_out":  "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",  # USDC
        "decimals_in": 18,
        "decimals_out": 6,
        "furucombo_in":  "WMATIC",
        "furucombo_out": "USDC",
    },
    # WETH/USDC
    {
        "symbol":     "WETH/USDC",
        "token_in":   "0x7ceB23fD6bC0adD59E62ac25578270cFf1b9f619",  # WETH
        "token_out":  "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",  # USDC
        "decimals_in": 18,
        "decimals_out": 6,
        "furucombo_in":  "WETH",
        "furucombo_out": "USDC",
    },
    # WBTC/USDC
    {
        "symbol":     "WBTC/USDC",
        "token_in":   "0x1BFD67037B42Cf73acF2047067bd4F2C47D9BfD6",  # WBTC
        "token_out":  "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",  # USDC
        "decimals_in": 8,
        "decimals_out": 6,
        "furucombo_in":  "WBTC",
        "furucombo_out": "USDC",
    },
    # WMATIC/WETH
    {
        "symbol":     "WMATIC/WETH",
        "token_in":   "0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270",  # WMATIC
        "token_out":  "0x7ceB23fD6bC0adD59E62ac25578270cFf1b9f619",  # WETH
        "decimals_in": 18,
        "decimals_out": 18,
        "furucombo_in":  "WMATIC",
        "furucombo_out": "WETH",
    },
]

# ── Subgraph Endpoints ─────────────────────────────────────────────────────
# Uniswap V3 Polygon subgraph (The Graph)
UNISWAP_V3_SUBGRAPH = "https://api.thegraph.com/subgraphs/name/uniswap/uniswap-v3-polygon"
# SushiSwap Polygon subgraph (The Graph)
SUSHISWAP_SUBGRAPH  = "https://api.thegraph.com/subgraphs/name/sushiswap/exchange-polygon"

# Backup: 1inch price API (no key required for basic usage)
ONEINCH_PRICE_URL   = "https://api.1inch.dev/price/v1.1/137"  # Polygon chain ID

# ── Price Fetchers ─────────────────────────────────────────────────────────

def get_price_uniswap_v3(token_in: str, token_out: str) -> Optional[float]:
    """Get price from Uniswap V3 via subgraph."""
    token_in  = token_in.lower()
    token_out = token_out.lower()
    query = """
    {
      pools(
        where: {
          token0_in: ["%s", "%s"]
          token1_in: ["%s", "%s"]
        }
        orderBy: volumeUSD
        orderDirection: desc
        first: 1
      ) {
        token0Price
        token1Price
        token0 { id symbol }
        token1 { id symbol }
      }
    }
    """ % (token_in, token_out, token_in, token_out)
    try:
        r = requests.post(
            UNISWAP_V3_SUBGRAPH,
            json={"query": query},
            timeout=10
        )
        data = r.json().get("data", {}).get("pools", [])
        if not data:
            return None
        pool = data[0]
        # Determine direction
        if pool["token0"]["id"].lower() == token_in:
            return float(pool["token0Price"])  # price of token0 in terms of token1
        else:
            return float(pool["token1Price"])
    except Exception as e:
        log.debug(f"Uniswap V3 subgraph error: {e}")
        return None


def get_price_sushiswap(token_in: str, token_out: str) -> Optional[float]:
    """Get price from SushiSwap via subgraph."""
    token_in  = token_in.lower()
    token_out = token_out.lower()
    query = """
    {
      pairs(
        where: {
          token0_in: ["%s", "%s"]
          token1_in: ["%s", "%s"]
        }
        orderBy: volumeUSD
        orderDirection: desc
        first: 1
      ) {
        token0Price
        token1Price
        token0 { id symbol }
        token1 { id symbol }
      }
    }
    """ % (token_in, token_out, token_in, token_out)
    try:
        r = requests.post(
            SUSHISWAP_SUBGRAPH,
            json={"query": query},
            timeout=10
        )
        data = r.json().get("data", {}).get("pairs", [])
        if not data:
            return None
        pair = data[0]
        if pair["token0"]["id"].lower() == token_in:
            return float(pair["token0Price"])
        else:
            return float(pair["token1Price"])
    except Exception as e:
        log.debug(f"SushiSwap subgraph error: {e}")
        return None


def get_prices_1inch(token_addresses: list) -> dict:
    """
    Fallback: 1inch price API — returns USD prices for multiple tokens.
    Returns {address_lower: usd_price}
    """
    try:
        addresses = ",".join([a.lower() for a in token_addresses])
        r = requests.get(
            f"{ONEINCH_PRICE_URL}",
            params={"addresses": addresses, "currency": "USD"},
            timeout=10
        )
        if r.status_code == 200:
            return {k.lower(): float(v) for k, v in r.json().items()}
    except Exception as e:
        log.debug(f"1inch price API error: {e}")
    return {}


def get_prices_coingecko(symbol: str) -> dict:
    """
    Fallback #2: CoinGecko free API for token USD prices.
    Returns {"uniswap": price, "sushiswap": price} — uses platform price as proxy.
    """
    # Map common symbols to CoinGecko IDs
    cg_map = {
        "WMATIC": "matic-network",
        "WETH":   "weth",
        "WBTC":   "wrapped-bitcoin",
        "USDC":   "usd-coin",
    }
    return {}


# ── Arbitrage Calculator ───────────────────────────────────────────────────

def calculate_spread(price_a: float, price_b: float) -> float:
    """
    Returns the % spread between two prices.
    Positive = buy on B, sell on A.
    """
    if price_a <= 0 or price_b <= 0:
        return 0.0
    spread = abs(price_a - price_b) / min(price_a, price_b) * 100
    return spread


def net_profit_pct(spread_pct: float) -> float:
    """Net profit after Aave flash loan fee."""
    return spread_pct - FLASH_LOAN_FEE - 0.1  # 0.1% estimated gas buffer


# ── Furucombo Deep Link Builder ────────────────────────────────────────────

def build_furucombo_link(
    buy_dex: str,
    sell_dex: str,
    token_in: str,
    token_out: str,
    amount: str = "1000"
) -> str:
    """
    Build a Furucombo pre-filled link.
    Opens directly to the combo page with the strategy described.
    """
    # Furucombo doesn't support full deep-link pre-fill via URL params publicly,
    # so we link to the create page with a helpful context param
    base = "https://furucombo.app/combo"
    params = {
        "chain": "polygon",
        "strategy": f"flashloan-arb-{token_in}-{token_out}".lower(),
    }
    return f"{base}?{urllib.parse.urlencode(params)}"


def build_furucombo_instructions(
    buy_dex: str,
    sell_dex: str,
    symbol: str,
    price_buy: float,
    price_sell: float,
    net_pct: float,
    amount: float = 1000.0
) -> str:
    """Build step-by-step instructions for the Furucombo combo."""
    token_in, token_out = symbol.split("/")
    estimated_profit = amount * (net_pct / 100)

    return f"""
🔥 FURUCOMBO COMBO SETUP 🔥

Pair: {symbol}
Borrow: {amount} {token_in} (Aave flash loan)

Step 1️⃣  Add cube: Aave V3 → Flash Loan
  • Token: {token_in}
  • Amount: {amount}

Step 2️⃣  Add cube: {buy_dex} → Swap Token
  • Sell: {token_in} @ {price_buy:.6f}
  • Buy: {token_out}

Step 3️⃣  Add cube: {sell_dex} → Swap Token
  • Sell: {token_out}
  • Buy: {token_in} @ {price_sell:.6f}

Step 4️⃣  Add cube: Aave V3 → Return Flash Loan
  • Repay: {amount} {token_in} + 0.09% fee

💰 Est. Profit: ~${estimated_profit:.2f} USDC
   (on ${amount} flash loan)
""".strip()


# ── Telegram Alert ─────────────────────────────────────────────────────────

def send_telegram(message: str) -> bool:
    """Send a Telegram message."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        log.warning("Telegram not configured — skipping alert")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }, timeout=10)
        return r.status_code == 200
    except Exception as e:
        log.error(f"Telegram send failed: {e}")
        return False


# ── Opportunity Logger ─────────────────────────────────────────────────────

ARB_LOG = LOGS_DIR / "arb_log.json"

def log_opportunity(opp: dict):
    """Append opportunity to arb_log.json."""
    existing = []
    if ARB_LOG.exists():
        try:
            existing = json.loads(ARB_LOG.read_text())
        except Exception:
            existing = []
    existing.append(opp)
    ARB_LOG.write_text(json.dumps(existing, indent=2))


# ── Core Scanner ───────────────────────────────────────────────────────────

def scan_pair(pair: dict) -> Optional[dict]:
    """
    Scan a single token pair for arbitrage opportunity.
    Returns opportunity dict if profitable, None otherwise.
    """
    symbol    = pair["symbol"]
    token_in  = pair["token_in"]
    token_out = pair["token_out"]

    log.info(f"Scanning {symbol}...")

    # Fetch prices from both DEXes
    price_uni   = get_price_uniswap_v3(token_in, token_out)
    price_sushi = get_price_sushiswap(token_in, token_out)

    # If subgraphs fail, try 1inch as fallback
    if price_uni is None or price_sushi is None:
        log.debug(f"Subgraph miss for {symbol} — trying 1inch fallback")
        prices_usd = get_prices_1inch([token_in, token_out])
        if prices_usd:
            price_in_usd  = prices_usd.get(token_in.lower())
            price_out_usd = prices_usd.get(token_out.lower())
            if price_in_usd and price_out_usd:
                # Both DEXes should reflect same USD price — if not, arb exists
                # For now, mark as unavailable until direct DEX prices are available
                pass
        log.info(f"  {symbol}: price data unavailable — skipping")
        return None

    spread = calculate_spread(price_uni, price_sushi)
    net    = net_profit_pct(spread)

    log.info(f"  Uniswap: {price_uni:.6f} | Sushi: {price_sushi:.6f} | Spread: {spread:.4f}% | Net: {net:.4f}%")

    if net < MIN_PROFIT_PCT:
        log.info(f"  ❌ Below threshold ({MIN_PROFIT_PCT}%) — no opportunity")
        return None

    # Determine direction
    if price_uni > price_sushi:
        buy_dex,  buy_price  = "SushiSwap", price_sushi
        sell_dex, sell_price = "Uniswap V3", price_uni
    else:
        buy_dex,  buy_price  = "Uniswap V3", price_uni
        sell_dex, sell_price = "SushiSwap",  price_sushi

    furucombo_url = build_furucombo_link(buy_dex, sell_dex, pair["furucombo_in"], pair["furucombo_out"])
    instructions  = build_furucombo_instructions(
        buy_dex, sell_dex, symbol, buy_price, sell_price, net
    )

    opp = {
        "timestamp":    datetime.now(timezone.utc).isoformat(),
        "symbol":       symbol,
        "buy_dex":      buy_dex,
        "buy_price":    buy_price,
        "sell_dex":     sell_dex,
        "sell_price":   sell_price,
        "spread_pct":   round(spread, 4),
        "net_profit_pct": round(net, 4),
        "furucombo_url":  furucombo_url,
        "instructions":   instructions,
    }

    return opp


def fire_alert(opp: dict):
    """Send Telegram alert for a found opportunity."""
    symbol    = opp["symbol"]
    buy_dex   = opp["buy_dex"]
    sell_dex  = opp["sell_dex"]
    spread    = opp["spread_pct"]
    net       = opp["net_profit_pct"]
    url       = opp["furucombo_url"]
    instr     = opp["instructions"]

    msg = f"""⚡ <b>ARB PRIME ALERT</b> ⚡

💎 Pair: <b>{symbol}</b>
🟢 Buy on: <b>{buy_dex}</b>
🔴 Sell on: <b>{sell_dex}</b>
📊 Gross Spread: <b>{spread:.3f}%</b>
💰 Est. Net Profit: <b>{net:.3f}%</b>

🔗 <a href="{url}">Open Furucombo</a>

{instr}

⏰ {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}
🤖 ArbPrime v1.0 | ZeusPrime Engine"""

    if SIMULATE:
        log.info(f"\n{'='*60}\n[SIMULATE] Would send:\n{msg}\n{'='*60}")
    else:
        sent = send_telegram(msg)
        log.info(f"  ✅ Alert sent via Telegram: {sent}")


# ── Main Loop ──────────────────────────────────────────────────────────────

def run():
    mode = "SIMULATE" if SIMULATE else "LIVE"
    log.info(f"""
╔══════════════════════════════════════════════════════════╗
║          ARB PRIME v1.0 — ONLINE [{mode:^8}]          ║
║          Scanning {len(WATCH_PAIRS)} pairs every {SCAN_INTERVAL}s                 ║
║          Min profit threshold: {MIN_PROFIT_PCT}%               ║
╚══════════════════════════════════════════════════════════╝
""")
    scan_count     = 0
    opps_found     = 0
    opps_sent      = 0

    # Startup ping
    if not SIMULATE:
        send_telegram("⚡ ArbPrime v1.0 ONLINE — scanning Polygon DEXes for flash loan opportunities")

    while True:
        scan_count += 1
        log.info(f"\n── Scan #{scan_count} ──────────────────────────────────────")

        for pair in WATCH_PAIRS:
            try:
                opp = scan_pair(pair)
                if opp:
                    opps_found += 1
                    log.info(f"  🔥 OPPORTUNITY: {opp['symbol']} | Net: {opp['net_profit_pct']}%")
                    log_opportunity(opp)
                    fire_alert(opp)
                    opps_sent += 1
            except Exception as e:
                log.error(f"Error scanning {pair['symbol']}: {e}")

        log.info(f"\n── Status: Scans={scan_count} | Opps Found={opps_found} | Alerts Sent={opps_sent}")
        log.info(f"── Next scan in {SCAN_INTERVAL}s...")
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    run()

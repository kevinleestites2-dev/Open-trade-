#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════╗
║              ARB PRIME v3.0 — The Flash Loan Executor               ║
║              Scan → Detect → Execute → Profit                       ║
║              Polygon: Uniswap V3 / QuickSwap / SushiSwap / Balancer ║
╚══════════════════════════════════════════════════════════════════════╝

SIMULATE_MODE=true  → scan + alert, no on-chain tx
SIMULATE_MODE=false → scan + alert + execute flash loan arb

EXECUTION PREREQ:
  1. Deploy contract once: python3 deploy_flash_arb.py
  2. Set FLASH_ARB_CONTRACT in .env
  3. Set POLY_PRIVATE_KEY in .env
  4. Set SIMULATE_MODE=false
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
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT    = os.getenv("TELEGRAM_CHAT_ID", "")
SIMULATE         = os.getenv("SIMULATE_MODE", "true").lower() == "true"
SCAN_INTERVAL    = int(os.getenv("ARB_SCAN_INTERVAL", "30"))
MIN_PROFIT_PCT   = float(os.getenv("ARB_MIN_PROFIT_PCT", "0.10"))

# Execution config (only used when SIMULATE=false)
PRIVATE_KEY      = os.getenv("POLY_PRIVATE_KEY", "")
CONTRACT_ADDRESS = os.getenv("FLASH_ARB_CONTRACT", "")
RPC_URL          = os.getenv("POLYGON_RPC", "https://polygon-rpc.com")
FLASH_LOAN_SIZE  = int(os.getenv("FLASH_LOAN_SIZE_USD", "5000"))   # USD value to borrow
MIN_PROFIT_USD   = float(os.getenv("MIN_PROFIT_USD", "5.0"))        # Min $5 profit per trade

# Fee constants
FLASH_LOAN_FEE = 0.09
GAS_BUFFER_PCT = 0.05

# ── DEX LP Fee Table ───────────────────────────────────────────────────────
DEX_LP_FEES = {
    "Uniswap V3":   0.30,
    "QuickSwap V2": 0.30,
    "QuickSwap V3": 0.15,
    "SushiSwap":    0.30,
    "Balancer V2":  0.10,
}

# ── DEX name → contract ID mapping ────────────────────────────────────────
DEX_ID_MAP = {
    "Uniswap V3":   0,
    "QuickSwap V2": 1,
    "SushiSwap":    2,
    "QuickSwap V3": 3,
}

# ── V3 fee tiers by DEX+pair (used for contract routing) ──────────────────
# 3000 = 0.30%  |  500 = 0.05%  |  100 = 0.01%
V3_FEE_TIER = {
    "Uniswap V3":   3000,
    "QuickSwap V3": 3000,
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

# Token decimals for amount calculations
TOKEN_DECIMALS = {
    "WPOL":  18,
    "WETH":  18,
    "WBTC":  8,
    "USDC":  6,
    "USDT0": 6,
    "DAI":   18,
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
    """Find highest-volume 2-token pool containing both tokens on given DEX."""
    addr_a = TOKENS[token_a].lower()
    addr_b = TOKENS[token_b].lower()
    url    = f"{DEXPAPRIKA_BASE}/networks/{NETWORK}/dexes/{dex_id}/pools"
    try:
        r = requests.get(url, timeout=12)
        r.raise_for_status()
        best     = None
        best_vol = 0.0
        for pool in r.json().get("pools", []):
            tokens = pool.get("tokens", [])
            if len(tokens) != 2:
                continue
            ids = [t["id"].lower() for t in tokens]
            if addr_a in ids and addr_b in ids:
                vol = pool.get("volume_usd", 0) or 0
                if best is None or vol > best_vol:
                    best     = pool
                    best_vol = vol
        return best
    except Exception as e:
        log.debug(f"Pool fetch error ({dex_id}): {e}")
    return None


def get_pair_price(dex_id: str, token_a: str, token_b: str) -> Optional[float]:
    pool = get_pool_for_pair(dex_id, token_a, token_b)
    if not pool:
        return None
    price = pool.get("price_usd")
    if price and float(price) > 0:
        return float(price)
    return None


def calc_net_profit_pct(gross_spread_pct: float, buy_dex: str, sell_dex: str) -> float:
    lp_buy   = DEX_LP_FEES.get(buy_dex,  0.30)
    lp_sell  = DEX_LP_FEES.get(sell_dex, 0.30)
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
        log.info(f"  {symbol}: need 2+ DEXes — skipping")
        return None

    names    = list(prices.keys())
    best_opp = None
    best_net = -999.0

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
                lp_buy   = DEX_LP_FEES.get(buy_name,  0.30)
                lp_sell  = DEX_LP_FEES.get(sell_name, 0.30)
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
                    "all_prices": dict(prices),
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
        if best_opp["net_profit_pct"] > 0 and best_opp["net_profit_pct"] >= MIN_PROFIT_PCT * 0.5:
            log.info(
                f"  NEAR-MISS: {best_opp['symbol']} | "
                f"net={best_opp['net_profit_pct']:.4f}% | "
                f"buy={best_opp['buy_dex']} sell={best_opp['sell_dex']}"
            )
        return None

    log.info(f"  OPPORTUNITY: buy {best_opp['buy_dex']} | sell {best_opp['sell_dex']}")
    best_opp["instructions"] = build_instructions(best_opp)
    return best_opp


def build_instructions(opp: dict) -> str:
    ta     = opp["token_a"]
    tb     = opp["token_b"]
    net    = opp["net_profit_pct"]
    gross  = opp["gross_pct"]
    fees   = opp["fee_breakdown"]
    est1k  = 1000  * (net / 100)
    est10k = 10000 * (net / 100)
    return (
        f"ARB SETUP ({opp['symbol']}):\n"
        f"1. Aave V3 Flash Loan: {FLASH_LOAN_SIZE} {ta}\n"
        f"2. {opp['buy_dex']}: Swap {ta} → {tb} (buy cheap)\n"
        f"3. {opp['sell_dex']}: Swap {tb} → {ta} (sell high)\n"
        f"4. Repay Aave + 0.09% fee\n\n"
        f"Fee Breakdown:\n"
        f"  Gross:      {gross:.3f}%\n"
        f"  Flash fee: -{fees['flash_loan']:.2f}%\n"
        f"  LP buy:    -{fees['lp_buy']:.2f}%\n"
        f"  LP sell:   -{fees['lp_sell']:.2f}%\n"
        f"  Gas:       -{fees['gas_buffer']:.2f}%\n"
        f"  NET:        {net:.3f}%\n\n"
        f"Est. Profit: ~${est1k:.2f} on $1k | ~${est10k:.2f} on $10k"
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


def fire_alert(opp: dict, tx_hash: str = None, profit_usd: float = None):
    """Send Telegram alert. If tx_hash set = live execution confirmed."""
    prices_str = " | ".join([f"{k}: ${v:.4f}" for k, v in opp["all_prices"].items()])

    if tx_hash:
        header = f"⚡ ARB EXECUTED — PROFIT CONFIRMED\n\n"
        footer = (
            f"\nTx: https://polygonscan.com/tx/{tx_hash}\n"
            f"Profit: ~${profit_usd:.2f}\n"
        )
    else:
        header = f"🔍 ARB SIGNAL [{'' if not SIMULATE else 'SIMULATE'}]\n\n"
        footer = ""

    msg = (
        f"{header}"
        f"Pair: {opp['symbol']}\n"
        f"BUY:  {opp['buy_dex']} @ ${opp['buy_price']:.6f}\n"
        f"SELL: {opp['sell_dex']} @ ${opp['sell_price']:.6f}\n"
        f"Gross: {opp['gross_pct']:.3f}% | Net: {opp['net_profit_pct']:.3f}%\n\n"
        f"Prices: {prices_str}\n\n"
        f"{opp['instructions']}"
        f"{footer}\n"
        f"{datetime.now(timezone.utc).strftime('%H:%M:%S UTC')} | ArbPrime v3.0"
    )

    if SIMULATE:
        log.info(f"\n{'='*60}\n[SIMULATE ALERT]\n{msg}\n{'='*60}")
    else:
        ok = send_telegram(msg)
        log.info(f"  Telegram sent: {ok}")


# ── Execution Layer ────────────────────────────────────────────────────────

def load_contract_abi() -> list:
    """Load ABI from deployed contract JSON, or use minimal ABI."""
    abi_path = BASE_DIR / "logs" / "flash_arb_contract.json"
    if abi_path.exists():
        data = json.loads(abi_path.read_text())
        return data.get("abi", [])
    # Minimal ABI — just the execute() and withdraw() functions
    return [
        {
            "name": "execute",
            "type": "function",
            "stateMutability": "nonpayable",
            "inputs": [
                {"name": "token",        "type": "address"},
                {"name": "amount",       "type": "uint256"},
                {"name": "intermediate", "type": "address"},
                {"name": "buyDex",       "type": "uint8"},
                {"name": "sellDex",      "type": "uint8"},
                {"name": "v3Fee",        "type": "uint24"},
                {"name": "minProfit",    "type": "uint256"},
            ],
            "outputs": [],
        },
        {
            "name": "withdraw",
            "type": "function",
            "stateMutability": "nonpayable",
            "inputs": [{"name": "token", "type": "address"}],
            "outputs": [],
        },
        {
            "name": "ArbExecuted",
            "type": "event",
            "inputs": [
                {"name": "tokenIn",    "type": "address", "indexed": True},
                {"name": "tokenMid",   "type": "address", "indexed": True},
                {"name": "loanAmount", "type": "uint256", "indexed": False},
                {"name": "profit",     "type": "uint256", "indexed": False},
                {"name": "buyDex",     "type": "uint8",   "indexed": False},
                {"name": "sellDex",    "type": "uint8",   "indexed": False},
            ],
        },
    ]


def calc_loan_amount(token: str, usd_value: int) -> int:
    """Convert USD flash loan target to token base units."""
    # Use approximate prices for sizing (not critical — Aave enforces repayment)
    approx_price_usd = {
        "WPOL":  0.35,
        "WETH":  3000.0,
        "WBTC":  65000.0,
        "USDC":  1.0,
        "USDT0": 1.0,
        "DAI":   1.0,
    }
    price   = approx_price_usd.get(token, 1.0)
    decimals = TOKEN_DECIMALS.get(token, 18)
    amount_tokens = usd_value / price
    return int(amount_tokens * (10 ** decimals))


def execute_arb(opp: dict) -> tuple:
    """
    Execute flash loan arb on-chain.
    Returns (tx_hash, profit_usd) on success, (None, 0) on failure.
    """
    if not PRIVATE_KEY or not CONTRACT_ADDRESS:
        log.error("LIVE mode: POLY_PRIVATE_KEY or FLASH_ARB_CONTRACT not set in .env")
        return None, 0

    try:
        from web3 import Web3
        from web3.middleware import ExtraDataToPOAMiddleware
        from eth_account import Account

        w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 30}))
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

        if not w3.is_connected():
            log.error("Cannot connect to Polygon RPC")
            return None, 0

        account  = Account.from_key(PRIVATE_KEY)
        abi      = load_contract_abi()
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(CONTRACT_ADDRESS),
            abi=abi,
        )

        token_a  = opp["token_a"]
        token_b  = opp["token_b"]
        buy_dex  = opp["buy_dex"]
        sell_dex = opp["sell_dex"]

        # Determine intermediate token
        # For WPOL/USDC arb: we borrow WPOL, swap to USDC and back
        # The "intermediate" is the non-borrowed token in the pair
        token_addr  = Web3.to_checksum_address(TOKENS[token_a])
        inter_addr  = Web3.to_checksum_address(TOKENS[token_b])
        buy_dex_id  = DEX_ID_MAP.get(buy_dex, 2)
        sell_dex_id = DEX_ID_MAP.get(sell_dex, 1)
        v3_fee      = V3_FEE_TIER.get(buy_dex, 3000)

        # Flash loan size
        loan_amount = calc_loan_amount(token_a, FLASH_LOAN_SIZE)

        # Min profit: require at least MIN_PROFIT_USD worth of token_a back
        approx_price = {"WPOL": 0.35, "WETH": 3000.0, "WBTC": 65000.0}.get(token_a, 1.0)
        decimals     = TOKEN_DECIMALS.get(token_a, 18)
        min_profit   = int((MIN_PROFIT_USD / approx_price) * (10 ** decimals))

        log.info(
            f"  EXECUTING: loan={FLASH_LOAN_SIZE} USD ({loan_amount} units) "
            f"| buy={buy_dex}({buy_dex_id}) sell={sell_dex}({sell_dex_id}) "
            f"| v3fee={v3_fee} | min_profit={min_profit}"
        )

        nonce    = w3.eth.get_transaction_count(account.address)
        gas_price = w3.eth.gas_price

        tx = contract.functions.execute(
            token_addr,
            loan_amount,
            inter_addr,
            buy_dex_id,
            sell_dex_id,
            v3_fee,
            min_profit,
        ).build_transaction({
            "from":     account.address,
            "nonce":    nonce,
            "gas":      500_000,
            "gasPrice": gas_price,
        })

        signed  = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        tx_hex  = tx_hash.hex()
        log.info(f"  Tx submitted: {tx_hex}")

        # Wait for receipt (up to 60s)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

        if receipt["status"] == 1:
            # Parse ArbExecuted event for actual profit
            profit_units = 0
            try:
                arb_event = contract.events.ArbExecuted()
                logs = arb_event.process_receipt(receipt)
                if logs:
                    profit_units = logs[0]["args"]["profit"]
            except Exception:
                pass

            profit_tokens = profit_units / (10 ** decimals) if profit_units else 0
            profit_usd    = profit_tokens * approx_price
            log.info(f"  SUCCESS: profit={profit_tokens:.6f} {token_a} (~${profit_usd:.2f})")

            # Auto-withdraw profit to owner wallet
            try:
                withdraw_tx = contract.functions.withdraw(token_addr).build_transaction({
                    "from":     account.address,
                    "nonce":    w3.eth.get_transaction_count(account.address),
                    "gas":      100_000,
                    "gasPrice": gas_price,
                })
                signed_w = account.sign_transaction(withdraw_tx)
                w3.eth.send_raw_transaction(signed_w.raw_transaction)
                log.info(f"  Profit withdrawn to {account.address}")
            except Exception as e:
                log.warning(f"  Auto-withdraw failed (manual withdraw needed): {e}")

            return tx_hex, profit_usd

        else:
            log.error(f"  Tx REVERTED: {tx_hex} — likely spread closed before execution")
            return None, 0

    except ImportError:
        log.error("web3 not installed — run: pip install web3")
        return None, 0
    except Exception as e:
        log.error(f"Execution error: {e}")
        return None, 0


# ── Log ────────────────────────────────────────────────────────────────────

ARB_LOG = LOGS_DIR / "arb_log.json"

def log_opportunity(opp: dict, tx_hash: str = None, profit_usd: float = None):
    existing = []
    if ARB_LOG.exists():
        try:
            existing = json.loads(ARB_LOG.read_text())
        except Exception:
            existing = []
    entry = dict(opp)
    if tx_hash:
        entry["tx_hash"]   = tx_hash
        entry["profit_usd"] = profit_usd
        entry["executed"]  = True
    existing.append(entry)
    ARB_LOG.write_text(json.dumps(existing, indent=2))


# ── Main ───────────────────────────────────────────────────────────────────

def run():
    mode = "SIMULATE" if SIMULATE else "LIVE"
    if not SIMULATE and (not PRIVATE_KEY or not CONTRACT_ADDRESS):
        log.warning(
            "LIVE mode requires POLY_PRIVATE_KEY + FLASH_ARB_CONTRACT in .env\n"
            "Falling back to SIMULATE mode.\n"
            "Run python3 deploy_flash_arb.py first to deploy the contract."
        )

    log.info(
        f"\n{'='*60}\n"
        f"  ARB PRIME v3.0 [{mode}]\n"
        f"  DexPaprika engine | {len(WATCH_PAIRS)} pairs | {len(DEX_LIST)} DEXes\n"
        f"  Scan every {SCAN_INTERVAL}s | Min NET: {MIN_PROFIT_PCT}%\n"
        f"  Flash loan size: ${FLASH_LOAN_SIZE} | Min profit: ${MIN_PROFIT_USD}\n"
        f"{'='*60}\n"
    )

    if not SIMULATE:
        send_telegram(
            f"⚡ ArbPrime v3.0 LIVE\n"
            f"Contract: {CONTRACT_ADDRESS[:10]}...\n"
            f"Flash size: ${FLASH_LOAN_SIZE} | Min profit: ${MIN_PROFIT_USD}"
        )

    scan_count  = 0
    opps_found  = 0
    trades_exec = 0
    total_profit = 0.0

    while True:
        scan_count += 1
        log.info(f"\n── Scan #{scan_count} ──────────────────────────────────────")

        for token_a, token_b in WATCH_PAIRS:
            try:
                opp = scan_pair(token_a, token_b)
                if opp:
                    opps_found += 1
                    tx_hash    = None
                    profit_usd = 0.0

                    if not SIMULATE:
                        # LIVE: execute the flash loan
                        tx_hash, profit_usd = execute_arb(opp)
                        if tx_hash:
                            trades_exec  += 1
                            total_profit += profit_usd

                    log_opportunity(opp, tx_hash, profit_usd)
                    fire_alert(opp, tx_hash, profit_usd if tx_hash else None)

            except Exception as e:
                log.error(f"Error on {token_a}/{token_b}: {e}")

        log.info(
            f"\n── Scans={scan_count} | Opps={opps_found} | "
            f"Executed={trades_exec} | Profit=${total_profit:.2f} | Next in {SCAN_INTERVAL}s"
        )
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    run()

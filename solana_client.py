"""
╔══════════════════════════════════════════════════════════════╗
║         SOLANA CLIENT — OpenTrade Chain C                    ║
║         Phantom Wallet + Drift Protocol                      ║
║         Prediction market arb engine on Solana               ║
╚══════════════════════════════════════════════════════════════╝
"""

import os
import json
import logging
import time
import urllib.request
from typing import Optional, List, Dict
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("OpenTrade.Solana")

# ============================================================================
# CONFIG
# ============================================================================

SOLANA_WALLET        = os.getenv("SOLANA_WALLET_ADDRESS", "3fHba4xfAeeiDCicmww4NJUEAd1cXN4ptEwg2Z8MbYbN")
SOLANA_PRIVATE_KEY   = os.getenv("SOLANA_PRIVATE_KEY", "")
DRIFT_ENV            = os.getenv("DRIFT_ENV", "mainnet-beta")
HELIUS_API_KEY       = os.getenv("HELIUS_API_KEY", "")
SIMULATE             = os.getenv("SIMULATE_MODE", "true").lower() == "true"

RPC_ENDPOINT = (
    f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
    if HELIUS_API_KEY else
    "https://api.mainnet-beta.solana.com"
)

# ============================================================================
# SOLANA RPC CLIENT (zero-dependency, pure urllib)
# ============================================================================

class SolanaRPC:
    """Minimal JSON-RPC wrapper — no solana-py required."""

    def __init__(self, endpoint: str = RPC_ENDPOINT):
        self.endpoint = endpoint
        self._id = 0

    def _call(self, method: str, params: list) -> dict:
        self._id += 1
        payload = json.dumps({
            "jsonrpc": "2.0",
            "id": self._id,
            "method": method,
            "params": params
        }).encode()
        req = urllib.request.Request(
            self.endpoint,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except Exception as e:
            log.error(f"[SolanaRPC] {method} error: {e}")
            return {}

    def get_balance(self, address: str) -> float:
        result = self._call("getBalance", [address])
        lamports = result.get("result", {}).get("value", 0)
        return lamports / 1e9

    def get_token_accounts(self, address: str) -> List[dict]:
        result = self._call("getTokenAccountsByOwner", [
            address,
            {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
            {"encoding": "jsonParsed"}
        ])
        return result.get("result", {}).get("value", [])

    def get_usdc_balance(self, address: str) -> float:
        USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
        for acct in self.get_token_accounts(address):
            info = acct.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
            if info.get("mint") == USDC_MINT:
                return float(info.get("tokenAmount", {}).get("uiAmount", 0))
        return 0.0

# ============================================================================
# DRIFT MARKETS SCANNER
# ============================================================================

class DriftScanner:
    """Scans Drift Protocol for arb opportunities via REST API."""

    DRIFT_STATS = "https://mainnet-beta.api.drift.trade"

    def __init__(self, rpc: SolanaRPC):
        self.rpc = rpc

    def _fetch(self, url: str) -> dict:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "OpenTrade/3.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except Exception as e:
            log.error(f"[Drift] fetch error {url}: {e}")
            return {}

    def get_markets(self) -> List[dict]:
        data = self._fetch(f"{self.DRIFT_STATS}/v2/markets")
        markets = data.get("markets", [])
        log.info(f"[Drift] {len(markets)} markets fetched")
        return markets

    def get_best_opportunity(self, markets: List[dict], min_funding_rate: float = 0.01) -> Optional[dict]:
        """Score = abs(funding_rate) * sqrt(volume_24h)"""
        best = None
        best_score = 0.0
        for m in markets:
            try:
                funding = float(m.get("lastFundingRate", 0))
                volume  = float(m.get("volume24H", 0))
                score   = abs(funding) * (volume ** 0.5)
                if abs(funding) >= min_funding_rate and score > best_score:
                    best_score = score
                    best = {
                        "market":       m.get("symbol", ""),
                        "market_index": m.get("marketIndex"),
                        "funding_rate": funding,
                        "volume_24h":   volume,
                        "side":         "SHORT" if funding > 0 else "LONG",
                        "score":        score,
                    }
            except (ValueError, TypeError):
                continue
        return best

# ============================================================================
# SOLANA CLIENT — Main Interface for OpenTrade
# ============================================================================

class SolanaClient:
    """Drop-in Chain C for OpenTrade. Mirrors PolyClient / KalshiClient interface."""

    def __init__(self):
        self.rpc     = SolanaRPC()
        self.scanner = DriftScanner(self.rpc)
        self.wallet  = SOLANA_WALLET
        log.info(f"SolanaClient initialized ✅ | wallet: {self.wallet[:8]}...")

    def get_balance(self) -> Dict[str, float]:
        if SIMULATE:
            return {"sol": 1.0, "usdc": 100.0}
        sol  = self.rpc.get_balance(self.wallet)
        usdc = self.rpc.get_usdc_balance(self.wallet)
        log.info(f"[Solana] Balance → SOL: {sol:.4f} | USDC: {usdc:.2f}")
        return {"sol": sol, "usdc": usdc}

    def scan_opportunities(self) -> Optional[dict]:
        markets = self.scanner.get_markets()
        if not markets:
            log.warning("[Solana] No Drift markets returned")
            return None
        opp = self.scanner.get_best_opportunity(markets)
        if opp:
            log.info(f"[Solana] Best opp → {opp['market']} {opp['side']} | funding: {opp['funding_rate']:.4f}")
        return opp

    def place_order(self, market: str, side: str, size: float, price: float = 0.0) -> dict:
        if SIMULATE:
            mock_id = f"SIM-SOL-{int(time.time())}"
            log.info(f"[Solana][SIM] {side} {size} {market} @ {price} → {mock_id}")
            return {"order_id": mock_id, "status": "simulated", "chain": "solana"}
        # TODO: Live execution via drift-py (next sprint — needs SOLANA_PRIVATE_KEY)
        log.warning("[Solana] Live orders require drift-py integration (next sprint)")
        return {"order_id": None, "status": "not_implemented", "chain": "solana"}

    def get_status(self) -> str:
        balance = self.get_balance()
        opp     = self.scan_opportunities()
        lines   = [
            "⚡ *Solana (Chain C)*",
            f"  Wallet: `{self.wallet[:8]}...{self.wallet[-4:]}`",
            f"  SOL: {balance['sol']:.4f} | USDC: {balance['usdc']:.2f}",
        ]
        if opp:
            lines.append(f"  Best opp: {opp['market']} {opp['side']} (funding: {opp['funding_rate']:.4f})")
        else:
            lines.append("  No arb opportunity found")
        return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    client = SolanaClient()
    print("\n=== BALANCE ===")
    bal = client.get_balance()
    print(f"SOL: {bal['sol']} | USDC: {bal['usdc']}")
    print("\n=== DRIFT SCAN ===")
    opp = client.scan_opportunities()
    if opp:
        print(json.dumps(opp, indent=2))
    else:
        print("No opportunities found")
    print("\n=== STATUS ===")
    print(client.get_status())

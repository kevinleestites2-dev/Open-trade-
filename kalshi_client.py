"""
╔══════════════════════════════════════════════════════════════╗
║           KALSHI CLIENT — ZeusPrime Cross-Platform           ║
║           Drop-in alongside PolyClient                       ║
║           RSA key-pair auth | REST API v3                    ║
╚══════════════════════════════════════════════════════════════╝

AUTH SETUP (one-time):
  1. Go to kalshi.com → Account & Security → API Keys → Create Key
  2. Save the .key file (private key) and the Key ID (UUID)
  3. Add to .env:
       KALSHI_API_KEY_ID=a952bcbe-xxxx-xxxx-xxxx-xxxxxxxxxxxx
       KALSHI_PRIVATE_KEY_PATH=/path/to/your.key   (or paste raw below)
       KALSHI_PRIVATE_KEY_PEM=-----BEGIN RSA PRIVATE KEY-----\n...
"""

import os
import time
import base64
import hashlib
import logging
import requests
from typing import Optional, List, Dict
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")
log = logging.getLogger("KalshiClient")

# ─────────────────────────────────────────────
# CONFIG  (reads from .env)
# ─────────────────────────────────────────────
KALSHI_API_BASE   = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_KEY_ID     = os.getenv("KALSHI_API_KEY_ID", "")
KALSHI_KEY_PATH   = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")
KALSHI_KEY_PEM    = os.getenv("KALSHI_PRIVATE_KEY_PEM", "")   # fallback inline PEM
SIMULATE          = os.getenv("SIMULATE_MODE", "true").lower() == "true"


def _load_private_key():
    """Load RSA private key from file path or inline PEM."""
    pem = None
    if KALSHI_KEY_PATH and Path(KALSHI_KEY_PATH).exists():
        pem = Path(KALSHI_KEY_PATH).read_bytes()
    elif KALSHI_KEY_PEM:
        pem = KALSHI_KEY_PEM.replace("\\n", "\n").encode()
    if pem is None:
        raise RuntimeError("No Kalshi private key found. Set KALSHI_PRIVATE_KEY_PATH or KALSHI_PRIVATE_KEY_PEM in .env")
    return serialization.load_pem_private_key(pem, password=None, backend=default_backend())


class KalshiClient:
    """
    Minimal Kalshi REST client — mirrors PolyClient interface so strategies
    can call either interchangeably.

    Methods:
        get_balance()           → float (USDC)
        get_markets(limit, tag) → List[dict]   (normalized to Poly format)
        get_price(market_id)    → Optional[float]  (yes price 0-1)
        get_orderbook(market_id)→ Optional[dict]   {bids, asks}
        place_order(...)        → Optional[str] order_id
        cancel_order(order_id)  → bool
    """

    BASE = KALSHI_API_BASE

    def __init__(self):
        if not KALSHI_KEY_ID:
            log.warning("KALSHI_API_KEY_ID not set — Kalshi client in stub mode")
            self._stub = True
            return
        self._stub = False
        self._key_id = KALSHI_KEY_ID
        self._private_key = _load_private_key()
        self._session = requests.Session()
        log.info("KalshiClient initialized ✅")

    # ─────────────────────────────────────────
    # AUTH HEADER
    # ─────────────────────────────────────────
    def _sign(self, method: str, path: str) -> dict:
        """Generate Kalshi RSA-PSS signature headers.
        Kalshi requires RSA-PSS with SHA256 (NOT PKCS1v15).
        Timestamp must be milliseconds. Sign path WITHOUT query string.
        """
        ts_ms = str(int(time.time() * 1000))
        path_no_query = path.split("?")[0]
        msg = ts_ms + method.upper() + path_no_query
        signature = self._private_key.sign(
            msg.encode("utf-8"),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        sig_b64 = base64.b64encode(signature).decode()
        return {
            "KALSHI-ACCESS-KEY":       self._key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts_ms,
            "KALSHI-ACCESS-SIGNATURE": sig_b64,
            "Content-Type":            "application/json",
        }

    def _get(self, path: str, params: dict = None) -> Optional[dict]:
        if self._stub:
            return None
        try:
            from urllib.parse import urlparse
            full_path = urlparse(self.BASE + path).path   # strip host, keep /trade-api/v2/...
            headers = self._sign("GET", full_path)
            resp = self._session.get(
                self.BASE + path, headers=headers, params=params, timeout=10
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            log.error(f"Kalshi GET {path}: {e}")
            return None

    def _post(self, path: str, body: dict) -> Optional[dict]:
        if self._stub:
            return None
        try:
            from urllib.parse import urlparse
            full_path = urlparse(self.BASE + path).path
            headers = self._sign("POST", full_path)
            resp = self._session.post(
                self.BASE + path, headers=headers, json=body, timeout=10
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            log.error(f"Kalshi POST {path}: {e}")
            return None

    def _delete(self, path: str) -> bool:
        if self._stub:
            return True
        try:
            from urllib.parse import urlparse
            full_path = urlparse(self.BASE + path).path
            headers = self._sign("DELETE", full_path)
            resp = self._session.delete(self.BASE + path, headers=headers, timeout=10)
            resp.raise_for_status()
            return True
        except Exception as e:
            log.error(f"Kalshi DELETE {path}: {e}")
            return False

    # ─────────────────────────────────────────
    # BALANCE
    # ─────────────────────────────────────────
    def get_balance(self) -> float:
        if SIMULATE:
            return float(os.getenv("INITIAL_CAPITAL", "100"))
        data = self._get("/portfolio/balance")
        if data:
            # Kalshi returns cents — convert to dollars
            return data.get("balance", 0) / 100
        return 0.0

    # ─────────────────────────────────────────
    # MARKETS  (normalized to PolyClient format)
    # ─────────────────────────────────────────
    def get_markets(self, limit: int = 40, tag: str = "") -> List[dict]:
        """
        Returns markets in a normalized dict format compatible with PolyClient:
          {
            "condition_id":  str,          # Kalshi ticker (e.g. "KXBTCD-25DEC31-T50000")
            "question":      str,
            "volume":        float,
            "tokens": [
              {"token_id": "<ticker>-YES", "outcome": "Yes"},
              {"token_id": "<ticker>-NO",  "outcome": "No"},
            ],
            "_source": "kalshi"
          }
        """
        params = {"limit": min(limit, 200), "status": "open"}
        if tag:
            params["series_ticker"] = tag
        data = self._get("/markets", params=params)
        if not data:
            return []
        raw_markets = data.get("markets", [])
        out = []
        for m in raw_markets:
            ticker  = m.get("ticker", "")
            vol     = m.get("volume", 0) or 0
            if vol < 1000:          # skip micro-markets
                continue
            out.append({
                "condition_id": ticker,
                "question":     m.get("title", ticker),
                "volume":       float(vol),
                "tokens": [
                    {"token_id": f"{ticker}-YES", "outcome": "Yes"},
                    {"token_id": f"{ticker}-NO",  "outcome": "No"},
                ],
                "_source":      "kalshi",
                "_raw":         m,
            })
        # sort by volume desc
        out.sort(key=lambda x: x["volume"], reverse=True)
        return out[:limit]

    # ─────────────────────────────────────────
    # PRICE  (yes price as 0.0–1.0)
    # ─────────────────────────────────────────
    def get_price(self, token_id: str) -> Optional[float]:
        """
        token_id format: "<TICKER>-YES" or "<TICKER>-NO"
        Returns probability 0.0–1.0
        """
        parts = token_id.rsplit("-", 1)
        if len(parts) != 2:
            return None
        ticker, side = parts[0], parts[1].upper()

        data = self._get(f"/markets/{ticker}/orderbook")
        if not data:
            return None
        ob = data.get("orderbook", {})

        # Kalshi returns yes_bids (bids to buy YES) and no_bids
        if side == "YES":
            bids = ob.get("yes", [])
        else:
            bids = ob.get("no", [])

        if not bids:
            return None
        # bids are [[price_cents, quantity], ...]
        best = max(bids, key=lambda x: x[0])
        return best[0] / 100.0      # convert cents to 0-1

    # ─────────────────────────────────────────
    # ORDER BOOK
    # ─────────────────────────────────────────
    def get_orderbook(self, token_id: str) -> Optional[dict]:
        """
        Returns {bids: [(price, size), ...], asks: [(price, size), ...]}
        Normalized to same format as PolyClient.get_orderbook()
        token_id: "<TICKER>-YES" or "<TICKER>-NO"
        """
        parts = token_id.rsplit("-", 1)
        if len(parts) != 2:
            return None
        ticker, side = parts[0], parts[1].upper()

        data = self._get(f"/markets/{ticker}/orderbook")
        if not data:
            return None
        ob = data.get("orderbook", {})

        if side == "YES":
            raw_bids = ob.get("yes", [])
            raw_asks = ob.get("no", [])   # NO bids = implied YES asks
        else:
            raw_bids = ob.get("no", [])
            raw_asks = ob.get("yes", [])

        def to_pairs(lst):
            return [(round(r[0] / 100.0, 4), r[1]) for r in lst if len(r) >= 2]

        bids = sorted(to_pairs(raw_bids), reverse=True)
        asks = sorted(to_pairs(raw_asks))
        return {"bids": bids, "asks": asks}

    # ─────────────────────────────────────────
    # PLACE ORDER
    # ─────────────────────────────────────────
    def place_order(
        self,
        token_id: str,
        side: str,        # "BUY" or "SELL"
        price: float,     # 0.0–1.0
        size: float,      # dollar amount
        order_type: str = "limit",
    ) -> Optional[str]:
        """
        token_id: "<TICKER>-YES" or "<TICKER>-NO"
        Returns Kalshi order_id string on success.
        """
        if SIMULATE:
            sim_id = f"kalshi_sim_{int(time.time())}"
            log.info(f"[SIMULATE] Kalshi {side} {size:.2f}@{price:.2f} → {sim_id}")
            return sim_id

        parts = token_id.rsplit("-", 1)
        if len(parts) != 2:
            return None
        ticker, outcome = parts[0], parts[1].capitalize()  # "Yes" or "No"

        # Kalshi uses cents for price, contracts for size
        price_cents = int(round(price * 100))
        # 1 contract = $0.01 * (1/price) in theory; use dollar-based sizing
        contracts   = max(1, int(size / (price if price > 0 else 0.5)))

        body = {
            "ticker":       ticker,
            "client_order_id": f"zeus_{int(time.time()*1000)}",
            "type":         order_type,
            "action":       side.lower(),      # "buy" or "sell"
            "side":         outcome,           # "Yes" or "No"
            "count":        contracts,
            "yes_price":    price_cents if outcome == "Yes" else (100 - price_cents),
        }

        data = self._post("/portfolio/orders", body)
        if data:
            oid = data.get("order", {}).get("order_id") or data.get("order_id")
            log.info(f"Kalshi order ✅ {side} {contracts}x {ticker}-{outcome} @{price_cents}¢ → {oid}")
            return oid
        return None

    # ─────────────────────────────────────────
    # CANCEL ORDER
    # ─────────────────────────────────────────
    def cancel_order(self, order_id: str) -> bool:
        if SIMULATE:
            return True
        return self._delete(f"/portfolio/orders/{order_id}")

    def cancel_all(self) -> bool:
        """Cancel all open orders."""
        if SIMULATE:
            return True
        data = self._get("/portfolio/orders", params={"status": "resting"})
        if not data:
            return False
        orders = data.get("orders", [])
        ok = True
        for o in orders:
            if not self._delete(f"/portfolio/orders/{o['order_id']}"):
                ok = False
        return ok

    # ─────────────────────────────────────────
    # CROSS-VENUE ARB HELPER
    # ─────────────────────────────────────────
    def find_matching_market(self, poly_question: str, poly_markets: List[dict]) -> Optional[dict]:
        """
        Find a Kalshi market that matches a Polymarket question.
        Uses keyword overlap scoring. Returns best match or None.
        """
        kalshi_markets = self.get_markets(limit=100)
        if not kalshi_markets:
            return None

        # Tokenize poly question
        poly_words = set(poly_question.lower().split())
        # remove common stopwords
        stops = {"will", "the", "a", "an", "in", "on", "by", "be", "to", "of",
                 "at", "is", "or", "and", "for", "this", "that", "with"}
        poly_words -= stops

        best_score = 0
        best_market = None
        for km in kalshi_markets:
            kq = km["question"].lower()
            kwords = set(kq.split()) - stops
            overlap = len(poly_words & kwords)
            # bonus if key number appears in both (e.g. "50000")
            for w in poly_words:
                if w.isdigit() and w in kq:
                    overlap += 3
            if overlap > best_score:
                best_score = overlap
                best_market = km

        if best_score >= 3:     # minimum threshold
            return best_market
        return None

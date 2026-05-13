#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════╗
║         CCXT BRIDGE — ZeusPrime Exchange Layer               ║
║         Connects ZeusPrime to 100+ crypto exchanges          ║
║         Absorbed into Pantheon: 2026-05-13                   ║
╚══════════════════════════════════════════════════════════════╝
"""

import os
import logging
from typing import Optional
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("CCXTBridge")

try:
    import ccxt
    _CCXT_AVAILABLE = True
except ImportError:
    _CCXT_AVAILABLE = False
    log.warning("ccxt not installed — run: pip install ccxt")


class CCXTBridge:
    """
    ZeusPrime exchange bridge via ccxt.
    Supports spot + futures trading on 100+ exchanges.
    Drop-in alongside existing Polymarket/Kalshi engines.
    """

    def __init__(self, exchange_id: str = "binance"):
        if not _CCXT_AVAILABLE:
            raise RuntimeError("ccxt not installed")

        self.exchange_id = exchange_id
        exchange_class = getattr(ccxt, exchange_id)

        self.exchange = exchange_class({
            "apiKey":    os.getenv(f"{exchange_id.upper()}_API_KEY", ""),
            "secret":    os.getenv(f"{exchange_id.upper()}_SECRET", ""),
            "password":  os.getenv(f"{exchange_id.upper()}_PASSPHRASE", ""),
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        })

        self.simulate = os.getenv("SIMULATE_MODE", "true").lower() == "true"
        log.info(f"CCXTBridge initialized — exchange: {exchange_id} | simulate: {self.simulate}")

    def get_ticker(self, symbol: str) -> dict:
        try:
            ticker = self.exchange.fetch_ticker(symbol)
            return {
                "symbol": symbol,
                "last":   ticker["last"],
                "bid":    ticker["bid"],
                "ask":    ticker["ask"],
                "volume": ticker["baseVolume"],
                "change": ticker["percentage"],
            }
        except Exception as e:
            log.error(f"get_ticker({symbol}): {e}")
            return {}

    def get_orderbook(self, symbol: str, limit: int = 20) -> dict:
        try:
            return self.exchange.fetch_order_book(symbol, limit)
        except Exception as e:
            log.error(f"get_orderbook({symbol}): {e}")
            return {}

    def get_ohlcv(self, symbol: str, timeframe: str = "1h", limit: int = 100) -> list:
        try:
            return self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        except Exception as e:
            log.error(f"get_ohlcv({symbol}): {e}")
            return []

    def get_balance(self) -> dict:
        if self.simulate:
            log.info("[SIMULATE] get_balance — returning mock $1000 USDT")
            return {"USDT": {"free": 1000.0, "used": 0.0, "total": 1000.0}}
        try:
            balance = self.exchange.fetch_balance()
            return {k: v for k, v in balance["total"].items() if v > 0}
        except Exception as e:
            log.error(f"get_balance: {e}")
            return {}

    def buy_market(self, symbol: str, amount: float) -> Optional[dict]:
        if self.simulate:
            log.info(f"[SIMULATE] BUY {amount} {symbol}")
            return {"status": "simulated", "symbol": symbol, "amount": amount, "side": "buy"}
        try:
            order = self.exchange.create_market_buy_order(symbol, amount)
            log.info(f"BUY EXECUTED: {symbol} x{amount} | id: {order['id']}")
            return order
        except Exception as e:
            log.error(f"buy_market({symbol}, {amount}): {e}")
            return None

    def sell_market(self, symbol: str, amount: float) -> Optional[dict]:
        if self.simulate:
            log.info(f"[SIMULATE] SELL {amount} {symbol}")
            return {"status": "simulated", "symbol": symbol, "amount": amount, "side": "sell"}
        try:
            order = self.exchange.create_market_sell_order(symbol, amount)
            log.info(f"SELL EXECUTED: {symbol} x{amount} | id: {order['id']}")
            return order
        except Exception as e:
            log.error(f"sell_market({symbol}, {amount}): {e}")
            return None

    def buy_limit(self, symbol: str, amount: float, price: float) -> Optional[dict]:
        if self.simulate:
            log.info(f"[SIMULATE] LIMIT BUY {amount} {symbol} @ ${price}")
            return {"status": "simulated", "symbol": symbol, "amount": amount, "price": price, "side": "buy"}
        try:
            order = self.exchange.create_limit_buy_order(symbol, amount, price)
            log.info(f"LIMIT BUY: {symbol} x{amount} @ {price} | id: {order['id']}")
            return order
        except Exception as e:
            log.error(f"buy_limit({symbol}, {amount}, {price}): {e}")
            return None

    def scan_arb(self, symbol: str, exchanges: list) -> Optional[dict]:
        """Scan price spread across exchanges. Returns opportunity if spread > 0.3%"""
        prices = {}
        for ex_id in exchanges:
            try:
                ex_class = getattr(ccxt, ex_id)
                ex = ex_class({"enableRateLimit": True})
                ticker = ex.fetch_ticker(symbol)
                prices[ex_id] = {"bid": ticker["bid"], "ask": ticker["ask"]}
            except Exception as e:
                log.warning(f"scan_arb skip {ex_id}: {e}")

        if len(prices) < 2:
            return None

        best_bid = max(prices, key=lambda x: prices[x]["bid"])
        best_ask = min(prices, key=lambda x: prices[x]["ask"])
        spread = (prices[best_bid]["bid"] - prices[best_ask]["ask"]) / prices[best_ask]["ask"]

        if spread > 0.003:
            return {
                "symbol":     symbol,
                "buy_on":     best_ask,
                "buy_price":  prices[best_ask]["ask"],
                "sell_on":    best_bid,
                "sell_price": prices[best_bid]["bid"],
                "spread_pct": round(spread * 100, 4),
            }
        return None


if __name__ == "__main__":
    import json
    bridge = CCXTBridge("binance")
    print("Balance:", json.dumps(bridge.get_balance(), indent=2))
    print("BTC/USDT:", json.dumps(bridge.get_ticker("BTC/USDT"), indent=2))
    print("Arb scan:", json.dumps(bridge.scan_arb("BTC/USDT", ["binance", "kraken", "coinbase"]), indent=2))

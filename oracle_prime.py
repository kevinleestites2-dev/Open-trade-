"""
╔══════════════════════════════════════════════════════════════╗
║         ORACLE PRIME v1.0 — The Weather Edge Engine          ║
║         ZeusPrime Strategy 12                                ║
║                                                              ║
║  NWS API (free, no key) → real probability data             ║
║  Kalshi weather markets → implied probability               ║
║  Gap ≥ EDGE_THRESHOLD → fire directional bet                ║
╚══════════════════════════════════════════════════════════════╝

HOW IT WORKS:
  1. Scan Kalshi for active weather markets (temp, precip, wind)
  2. Parse the market question → extract city, threshold, date
  3. Pull NWS gridded forecast for that city
  4. Compute real probability that the threshold is met
  5. Compare to Kalshi's implied price
  6. If gap >= EDGE_THRESHOLD → bet the mispriced side
  7. Alert Telegram with full breakdown

SUPPORTED MARKET TYPES:
  - "Will [City] reach [X]°F on [date]?"
  - "Will [City] exceed [X]°F this week?"
  - "Will [City] see rain on [date]?"
  - "Will [City] see [X]+ inches of rain [period]?"
  - Wind speed markets (potentialOf[X]mphWinds)

CITIES COVERED: All major US cities (NWS national grid)
"""

import os
import re
import time
import math
import json
import logging
import requests
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, List, Tuple
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")
log = logging.getLogger("OraclePrime")

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────
EDGE_THRESHOLD  = 0.08   # minimum gap (8¢) between NWS prob and Kalshi price to fire
MAX_BET_SIZE    = 25.0   # max $ per weather bet
MIN_BET_SIZE    = 2.0
NWS_BASE        = "https://api.weather.gov"
SIMULATE        = os.getenv("SIMULATE_MODE", "true").lower() == "true"

# ─────────────────────────────────────────────────────────────
# CITY → COORDS MAP  (extend as needed)
# ─────────────────────────────────────────────────────────────
CITY_COORDS: Dict[str, Tuple[float, float]] = {
    "new york":     (40.7128, -74.0060),
    "nyc":          (40.7128, -74.0060),
    "los angeles":  (34.0522, -118.2437),
    "la":           (34.0522, -118.2437),
    "chicago":      (41.8781, -87.6298),
    "houston":      (29.7604, -95.3698),
    "phoenix":      (33.4484, -112.0740),
    "philadelphia": (39.9526, -75.1652),
    "san antonio":  (29.4241, -98.4936),
    "san diego":    (32.7157, -117.1611),
    "dallas":       (32.7767, -96.7970),
    "austin":       (30.2672, -97.7431),
    "miami":        (25.7617, -80.1918),
    "atlanta":      (33.7490, -84.3880),
    "seattle":      (47.6062, -122.3321),
    "denver":       (39.7392, -104.9903),
    "boston":       (42.3601, -71.0589),
    "las vegas":    (36.1699, -115.1398),
    "nashville":    (36.1627, -86.7816),
    "portland":     (45.5231, -122.6765),
    "orlando":      (28.5383, -81.3792),
    "tampa":        (27.9506, -82.4572),
    "fort myers":   (26.6406, -81.8723),
    "jacksonville": (30.3322, -81.6557),
    "charlotte":    (35.2271, -80.8431),
    "memphis":      (35.1495, -90.0490),
    "detroit":      (42.3314, -83.0458),
    "minneapolis":  (44.9778, -93.2650),
    "kansas city":  (39.0997, -94.5786),
    "indianapolis": (39.7684, -86.1581),
    "columbus":     (39.9612, -82.9988),
    "new orleans":  (29.9511, -90.0715),
    "oklahoma city":(35.4676, -97.5164),
    "tucson":       (32.2226, -110.9747),
    "raleigh":      (35.7796, -78.6382),
    "sacramento":   (38.5816, -121.4944),
    "st. louis":    (38.6270, -90.1994),
    "pittsburgh":   (40.4406, -79.9959),
    "salt lake city":(40.7608, -111.8910),
    "richmond":     (37.5407, -77.4360),
    "cincinnati":   (39.1031, -84.5120),
    "milwaukee":    (43.0389, -87.9065),
}

# ─────────────────────────────────────────────────────────────
# NWS CLIENT
# ─────────────────────────────────────────────────────────────
class NWSClient:
    """Pulls forecast data from api.weather.gov — no API key needed."""

    _grid_cache: Dict[str, dict] = {}   # coords → grid metadata
    _data_cache: Dict[str, Tuple[float, dict]] = {}  # grid_url → (ts, data)
    CACHE_TTL = 1800  # 30 min

    def _get_grid(self, lat: float, lon: float) -> Optional[dict]:
        key = f"{lat:.4f},{lon:.4f}"
        if key in self._grid_cache:
            return self._grid_cache[key]
        try:
            r = requests.get(
                f"{NWS_BASE}/points/{lat},{lon}",
                headers={"User-Agent": "OraclePrime/1.0 zeus@pantheon.ai"},
                timeout=10
            )
            r.raise_for_status()
            props = r.json()["properties"]
            grid = {
                "office": props["cwa"],
                "gridX":  props["gridX"],
                "gridY":  props["gridY"],
                "forecastGridData": props["forecastGridData"],
            }
            self._grid_cache[key] = grid
            return grid
        except Exception as e:
            log.error(f"NWS grid lookup {lat},{lon}: {e}")
            return None

    def _get_grid_data(self, grid_url: str) -> Optional[dict]:
        now = time.time()
        if grid_url in self._data_cache:
            ts, data = self._data_cache[grid_url]
            if now - ts < self.CACHE_TTL:
                return data
        try:
            r = requests.get(
                grid_url,
                headers={"User-Agent": "OraclePrime/1.0 zeus@pantheon.ai"},
                timeout=15
            )
            r.raise_for_status()
            data = r.json()["properties"]
            self._data_cache[grid_url] = (now, data)
            return data
        except Exception as e:
            log.error(f"NWS grid data {grid_url}: {e}")
            return None

    def get_forecast(self, city: str) -> Optional[dict]:
        """Returns raw NWS gridded properties for a city."""
        city_lower = city.lower().strip()
        coords = CITY_COORDS.get(city_lower)
        if not coords:
            # fuzzy match
            for k, v in CITY_COORDS.items():
                if city_lower in k or k in city_lower:
                    coords = v
                    break
        if not coords:
            log.warning(f"City not in coords map: {city}")
            return None
        grid = self._get_grid(*coords)
        if not grid:
            return None
        return self._get_grid_data(grid["forecastGridData"])

    @staticmethod
    def c_to_f(c: float) -> float:
        return c * 9 / 5 + 32

    def max_temp_on_date(self, city: str, target_date: datetime.date) -> Optional[float]:
        """Returns forecasted max temp in °F for a specific date."""
        data = self.get_forecast(city)
        if not data:
            return None
        best = None
        for v in data.get("maxTemperature", {}).get("values", []):
            ts = v["validTime"].split("/")[0]
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if dt.date() == target_date and v["value"] is not None:
                best = self.c_to_f(v["value"])
        return best

    def prob_exceed_temp(self, city: str, threshold_f: float, target_date: datetime.date) -> float:
        """
        Returns probability (0.0–1.0) that max temp exceeds threshold_f on target_date.
        Uses NWS hourly data to build a distribution + uncertainty model.
        """
        data = self.get_forecast(city)
        if not data:
            return 0.5  # unknown → assume coin flip

        # Collect all hourly max temps on that day
        temps_f = []
        for v in data.get("maxTemperature", {}).get("values", []):
            ts = v["validTime"].split("/")[0]
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if dt.date() == target_date and v["value"] is not None:
                temps_f.append(self.c_to_f(v["value"]))

        if not temps_f:
            # fall back to apparent temp
            for v in data.get("apparentTemperature", {}).get("values", []):
                ts = v["validTime"].split("/")[0]
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if dt.date() == target_date and v["value"] is not None:
                    temps_f.append(self.c_to_f(v["value"]))

        if not temps_f:
            return 0.5

        forecast_max = max(temps_f)

        # NWS forecast uncertainty grows with time horizon
        days_out = (target_date - datetime.now(timezone.utc).date()).days
        # σ ≈ 2°F at 1 day, 4°F at 3 days, 6°F at 7 days
        sigma = max(2.0, min(6.0, 2.0 + days_out * 0.6))

        # Probability that actual max > threshold given forecast_max ± sigma
        # Using normal CDF approximation
        z = (forecast_max - threshold_f) / sigma
        prob = _normal_cdf(z)
        return round(prob, 4)

    def prob_rain(self, city: str, target_date: datetime.date) -> float:
        """Returns NWS probability of precipitation on target_date (0.0–1.0)."""
        data = self.get_forecast(city)
        if not data:
            return 0.5
        probs = []
        for v in data.get("probabilityOfPrecipitation", {}).get("values", []):
            ts = v["validTime"].split("/")[0]
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if dt.date() == target_date and v["value"] is not None:
                probs.append(v["value"] / 100.0)
        return max(probs) if probs else 0.5

    def prob_wind(self, city: str, mph_threshold: int, target_date: datetime.date) -> float:
        """Returns NWS probability of wind gusts exceeding mph_threshold."""
        data = self.get_forecast(city)
        if not data:
            return 0.5

        # NWS has potentialOf[X]mphWinds fields
        field_map = {
            15: "potentialOf15mphWinds",
            20: "potentialOf20mphWinds",
            25: "potentialOf25mphWinds",
            30: "potentialOf30mphWindGusts",
            35: "potentialOf35mphWinds",
            40: "potentialOf40mphWindGusts",
            45: "potentialOf45mphWinds",
            50: "potentialOf50mphWindGusts",
            60: "potentialOf60mphWindGusts",
        }
        # find closest threshold
        closest = min(field_map.keys(), key=lambda x: abs(x - mph_threshold))
        field = field_map[closest]

        probs = []
        for v in data.get(field, {}).get("values", []):
            ts = v["validTime"].split("/")[0]
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if dt.date() == target_date and v["value"] is not None:
                probs.append(v["value"] / 100.0)

        return max(probs) if probs else 0.5


def _normal_cdf(z: float) -> float:
    """Approximation of the standard normal CDF."""
    # Abramowitz & Stegun approximation
    if z < -8: return 0.0
    if z > 8:  return 1.0
    t = 1 / (1 + 0.2316419 * abs(z))
    poly = t * (0.319381530
               + t * (-0.356563782
               + t * (1.781477937
               + t * (-1.821255978
               + t * 1.330274429))))
    p = 1 - (1 / math.sqrt(2 * math.pi)) * math.exp(-0.5 * z * z) * poly
    return p if z >= 0 else 1 - p


# ─────────────────────────────────────────────────────────────
# MARKET PARSER  — parse Kalshi question → structured data
# ─────────────────────────────────────────────────────────────

def parse_weather_market(question: str, ticker: str) -> Optional[dict]:
    """
    Parse a Kalshi market question into structured fields.
    Returns dict with: type, city, threshold, unit, date, direction
    Returns None if not a weather market.
    """
    q = question.lower()

    # Filter: must be weather-related
    weather_keywords = ["temperature", "temp", "°f", "degrees", "rain", "snow",
                        "wind", "mph", "precipitation", "high", "low", "heat",
                        "cold", "storm", "hurricane", "freeze", "frost"]
    if not any(kw in q for kw in weather_keywords):
        return None

    result = {"raw": question, "ticker": ticker}

    # ── Market type detection ──
    if any(w in q for w in ["rain", "precipitation", "precip", "wet"]):
        result["type"] = "rain"
    elif any(w in q for w in ["wind", "mph", "gust"]):
        result["type"] = "wind"
        m = re.search(r'(\d+)\s*mph', q)
        result["threshold"] = int(m.group(1)) if m else 20
    elif any(w in q for w in ["temperature", "temp", "°f", "degrees", "high", "heat"]):
        result["type"] = "temperature"
        m = re.search(r'(\d{2,3})\s*(?:°f|degrees|°)', q)
        result["threshold"] = float(m.group(1)) if m else None
    elif any(w in q for w in ["snow", "snowfall", "blizzard"]):
        result["type"] = "snow"
    else:
        return None

    # ── Direction (exceed / fall below) ──
    result["direction"] = "above"
    if any(w in q for w in ["below", "under", "less than", "not reach", "won't reach"]):
        result["direction"] = "below"

    # ── City extraction ──
    city_found = None
    for city in sorted(CITY_COORDS.keys(), key=len, reverse=True):
        if city in q:
            city_found = city
            break
    result["city"] = city_found

    # ── Date extraction ──
    today = datetime.now(timezone.utc).date()
    result["date"] = today  # default = today

    # "today"
    if "today" in q:
        result["date"] = today
    # "tomorrow"
    elif "tomorrow" in q:
        result["date"] = today + timedelta(days=1)
    # "this week" / "this weekend"
    elif "this week" in q or "this weekend" in q:
        result["date"] = today + timedelta(days=2)  # mid-week estimate
    # specific month/day: "may 8", "may 9th"
    else:
        month_map = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
                     "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12}
        for mon, num in month_map.items():
            m = re.search(rf'{mon}\w*\s+(\d{{1,2}})', q)
            if m:
                day = int(m.group(1))
                year = today.year
                candidate = datetime(year, num, day).date()
                if candidate < today:
                    candidate = datetime(year + 1, num, day).date()
                result["date"] = candidate
                break

    return result


# ─────────────────────────────────────────────────────────────
# ORACLE PRIME — STRATEGY CLASS
# ─────────────────────────────────────────────────────────────

class OraclePrimeStrategy:
    """
    ZeusPrime Strategy 12 — Weather Edge.
    Plugs directly into the ZeusPrime strategy list.
    """
    NAME = "oracle_prime_weather"
    SCAN_INTERVAL = 300   # scan every 5 min
    _last_scan = 0

    def __init__(self, kalshi_client, risk_manager=None):
        self.kalshi  = kalshi_client
        self.risk    = risk_manager
        self.nws     = NWSClient()
        self.fired   = {}   # ticker → ts, prevent duplicate bets
        log.info("OraclePrime v1.0 armed ✅ (NWS weather edge)")

    def safe_run(self):
        try:
            self.run()
        except Exception as e:
            log.error(f"OraclePrime error: {e}", exc_info=True)

    def run(self):
        now = time.time()
        if now - self._last_scan < self.SCAN_INTERVAL:
            return
        OraclePrimeStrategy._last_scan = now

        log.info("OraclePrime: scanning Kalshi weather markets...")
        markets = self.kalshi.get_markets(limit=100, tag="weather") if self.kalshi else []

        if not markets:
            # Also try without tag — some weather markets are uncategorized
            markets = self.kalshi.get_markets(limit=100) if self.kalshi else []

        fired_count = 0
        for market in markets:
            question = market.get("question", "")
            ticker   = market.get("condition_id", "")

            # Skip if we already bet this market recently
            if ticker in self.fired:
                if now - self.fired[ticker] < 3600:
                    continue

            parsed = parse_weather_market(question, ticker)
            if not parsed:
                continue
            if not parsed.get("city"):
                log.debug(f"OraclePrime: no city parsed from: {question}")
                continue

            edge = self._compute_edge(parsed, market)
            if edge:
                self._fire(edge, market)
                self.fired[ticker] = now
                fired_count += 1

        log.info(f"OraclePrime: scan complete. {fired_count} bets fired.")

    def _compute_edge(self, parsed: dict, market: dict) -> Optional[dict]:
        """
        Get NWS probability, compare to Kalshi price.
        Returns edge dict if gap >= EDGE_THRESHOLD, else None.
        """
        mtype  = parsed.get("type")
        city   = parsed.get("city")
        date   = parsed.get("date")
        dirn   = parsed.get("direction", "above")

        # ── Get NWS probability ──
        nws_prob = None
        if mtype == "temperature":
            threshold = parsed.get("threshold")
            if threshold is None:
                return None
            nws_prob = self.nws.prob_exceed_temp(city, threshold, date)
            if dirn == "below":
                nws_prob = 1.0 - nws_prob

        elif mtype == "rain":
            nws_prob = self.nws.prob_rain(city, date)
            if dirn == "below":
                nws_prob = 1.0 - nws_prob  # "will it NOT rain?"

        elif mtype == "wind":
            threshold = parsed.get("threshold", 20)
            nws_prob = self.nws.prob_wind(city, threshold, date)
            if dirn == "below":
                nws_prob = 1.0 - nws_prob

        if nws_prob is None:
            return None

        # ── Get Kalshi YES price ──
        tokens = market.get("tokens", [])
        if not tokens:
            return None
        yes_token = tokens[0]["token_id"]
        kalshi_price = self.kalshi.get_price(yes_token) if self.kalshi else None
        if kalshi_price is None:
            return None

        # ── Compute gap ──
        gap = nws_prob - kalshi_price   # positive = NWS says more likely than market

        if abs(gap) < EDGE_THRESHOLD:
            return None

        # Decide bet direction
        if gap > 0:
            bet_side   = "BUY"
            bet_token  = yes_token
            bet_price  = kalshi_price
            confidence = nws_prob
        else:
            # market overpriced → buy NO
            no_token  = tokens[1]["token_id"] if len(tokens) > 1 else None
            if not no_token:
                return None
            bet_side   = "BUY"
            bet_token  = no_token
            bet_price  = 1.0 - kalshi_price  # NO price
            confidence = 1.0 - nws_prob
            gap        = abs(gap)

        return {
            "ticker":      market["condition_id"],
            "question":    market["question"],
            "city":        city,
            "type":        mtype,
            "date":        str(date),
            "nws_prob":    nws_prob,
            "kalshi_price":kalshi_price,
            "gap":         gap,
            "confidence":  confidence,
            "bet_side":    bet_side,
            "bet_token":   bet_token,
            "bet_price":   bet_price,
        }

    def _fire(self, edge: dict, market: dict):
        """Size and place the bet. Alert Telegram."""
        # Kelly-inspired sizing: f = (edge) / (1 - bet_price)
        # Capped at MAX_BET_SIZE
        kelly_fraction = edge["gap"] / max(1 - edge["bet_price"], 0.01)
        balance = self.risk.client.get_balance() if self.risk else 100
        raw_size = balance * min(kelly_fraction * 0.25, 0.05)  # quarter-Kelly, max 5%
        size = round(max(MIN_BET_SIZE, min(MAX_BET_SIZE, raw_size)), 2)

        log.info(
            f"OraclePrime FIRE: {edge['question'][:50]} | "
            f"NWS={edge['nws_prob']:.0%} Kalshi={edge['kalshi_price']:.0%} "
            f"gap={edge['gap']:.0%} | {edge['bet_side']} ${size:.2f}"
        )

        order_id = None
        if self.kalshi:
            order_id = self.kalshi.place_order(
                token_id=edge["bet_token"],
                side=edge["bet_side"],
                price=edge["bet_price"],
                size=size,
            )

        # ── Telegram Alert ──
        status = "✅ FILLED" if order_id else ("🟡 SIMULATED" if SIMULATE else "❌ FAILED")
        _tg(
            f"🌤️ <b>OraclePrime — Weather Edge</b>\n"
            f"📍 {edge['city'].title()} | {edge['type'].upper()} | {edge['date']}\n"
            f"❓ {edge['question'][:60]}\n\n"
            f"📡 NWS Probability:  <b>{edge['nws_prob']:.0%}</b>\n"
            f"💹 Kalshi Implied:   <b>{edge['kalshi_price']:.0%}</b>\n"
            f"⚡ Edge Gap:        <b>{edge['gap']:.0%}</b>\n\n"
            f"🎯 Bet: {edge['bet_side']} ${size:.2f} @ {edge['bet_price']:.2f}\n"
            f"📊 {status}"
        )


def _tg(text: str):
    """Send Telegram alert (standalone, no Zeus import needed)."""
    token = os.getenv("TELEGRAM_TOKEN", "")
    chat  = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        log.info(f"[TG] {text[:120]}")
        return
    try:
        import urllib.request, urllib.parse
        url  = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id":    chat,
            "text":       text,
            "parse_mode": "HTML",
        }).encode()
        urllib.request.urlopen(urllib.request.Request(url, data), timeout=5)
    except Exception as e:
        log.warning(f"TG send failed: {e}")


# ─────────────────────────────────────────────────────────────
# STANDALONE SCANNER  (run directly: python oracle_prime.py)
# ─────────────────────────────────────────────────────────────

def _demo_scan():
    """
    Demo mode — tests NWS data pipeline without Kalshi.
    Shows what OraclePrime would see for Fort Myers today.
    """
    print("\n🌤️  OraclePrime v1.0 — Demo Mode (Fort Myers FL)\n" + "─"*50)
    nws = NWSClient()
    today = datetime.now(timezone.utc).date()

    print("\n📡 NWS Forecast Data:")
    for i in range(5):
        d = today + timedelta(days=i)
        max_t = nws.max_temp_on_date("fort myers", d)
        p90   = nws.prob_exceed_temp("fort myers", 90.0, d)
        p92   = nws.prob_exceed_temp("fort myers", 92.0, d)
        rain  = nws.prob_rain("fort myers", d)
        print(f"  {d.strftime('%a %b %d')}: max={max_t}°F | "
              f"P(>90°F)={p90:.0%} | P(>92°F)={p92:.0%} | P(rain)={rain:.0%}")

    print("\n💹 Example Edge Scenarios:")
    scenarios = [
        ("Will Miami reach 95°F tomorrow?",    "temperature", "miami",     95, today+timedelta(1)),
        ("Will Fort Myers hit 90°F on Friday?","temperature", "fort myers",90, today+timedelta(2)),
        ("Will Orlando see rain this weekend?", "rain",       "orlando",   None, today+timedelta(3)),
    ]
    for q, mtype, city, thresh, d in scenarios:
        if mtype == "temperature":
            nws_p = nws.prob_exceed_temp(city, thresh, d)
        else:
            nws_p = nws.prob_rain(city, d)
        # Simulated Kalshi price (for demo)
        import random
        random.seed(hash(q))
        kalshi_p = round(nws_p + random.uniform(-0.15, 0.15), 2)
        kalshi_p = max(0.05, min(0.95, kalshi_p))
        gap = abs(nws_p - kalshi_p)
        signal = "⚡ BET" if gap >= EDGE_THRESHOLD else "  pass"
        print(f"  {signal} | '{q[:45]}'\n"
              f"         NWS={nws_p:.0%}  Kalshi≈{kalshi_p:.0%}  gap={gap:.0%}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    _demo_scan()

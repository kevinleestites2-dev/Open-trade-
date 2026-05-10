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
    # International — Open-Meteo handles these globally; coords unlock the fallback
    "london":       (51.5074, -0.1278),
    "paris":        (48.8566,  2.3522),
    "toronto":      (43.6532, -79.3832),
    "mexico city":  (19.4326, -99.1332),
    "cdmx":         (19.4326, -99.1332),
    "bogota":       ( 4.7110, -74.0721),
    "buenos aires": (-34.6037, -58.3816),
    "sao paulo":    (-23.5505, -46.6333),
    "rio de janeiro":(-22.9068, -43.1729),
    "sydney":       (-33.8688, 151.2093),
    "tokyo":        (35.6762, 139.6503),
    "dubai":        (25.2048,  55.2708),
    "berlin":       (52.5200,  13.4050),
    "madrid":       (40.4168,  -3.7038),
    "amsterdam":    (52.3676,   4.9041),
    "mumbai":       (19.0760,  72.8777),
    "lagos":        ( 6.5244,   3.3792),
    "nairobi":      (-1.2864,  36.8172),
}

# ─────────────────────────────────────────────────────────────
# NWS CLIENT
# ─────────────────────────────────────────────────────────────
class NWSClient:
    """
    Pulls forecast data from api.weather.gov — no API key needed.
    For cities outside the NWS coverage map, Open-Meteo is used as fallback
    (global coverage, also no API key).
    """

    _grid_cache: Dict[str, dict] = {}   # coords → grid metadata
    _data_cache: Dict[str, Tuple[float, dict]] = {}  # grid_url → (ts, data)
    CACHE_TTL = 1800  # 30 min
    _open_meteo_instance = None  # lazily initialised after OpenMeteoClient is defined

    @property
    def _open_meteo(self):
        if NWSClient._open_meteo_instance is None:
            NWSClient._open_meteo_instance = OpenMeteoClient()
        return NWSClient._open_meteo_instance

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
            log.warning(f"City not in NWS coords map: {city} — Open-Meteo fallback not available at this level")
            return None
        grid = self._get_grid(*coords)
        if not grid:
            log.warning(f"NWS grid lookup failed for {city} — will fall back to Open-Meteo in callers")
            return None
        data = self._get_grid_data(grid["forecastGridData"])
        if data is None:
            log.warning(f"NWS grid data empty for {city} — will fall back to Open-Meteo in callers")
        return data

    def _get_coords(self, city: str) -> Optional[Tuple[float, float]]:
        """Resolve city string to (lat, lon), including fuzzy match."""
        city_lower = city.lower().strip()
        coords = CITY_COORDS.get(city_lower)
        if coords:
            return coords
        for k, v in CITY_COORDS.items():
            if city_lower in k or k in city_lower:
                return v
        return None

    @staticmethod
    def c_to_f(c: float) -> float:
        return c * 9 / 5 + 32

    def max_temp_on_date(self, city: str, target_date: datetime.date) -> Optional[float]:
        """Returns forecasted max temp in °F for a specific date. Falls back to Open-Meteo."""
        data = self.get_forecast(city)
        best = None
        if data:
            for v in data.get("maxTemperature", {}).get("values", []):
                ts = v["validTime"].split("/")[0]
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if dt.date() == target_date and v["value"] is not None:
                    best = self.c_to_f(v["value"])
        if best is None:
            coords = self._get_coords(city)
            if coords:
                best = self._open_meteo.max_temp_on_date(coords[0], coords[1], target_date)
        return best

    def prob_exceed_temp(self, city: str, threshold_f: float, target_date: datetime.date) -> float:
        """
        Returns probability (0.0–1.0) that max temp exceeds threshold_f on target_date.
        NWS primary, Open-Meteo fallback.
        """
        data = self.get_forecast(city)
        if not data:
            coords = self._get_coords(city)
            if coords:
                log.info(f"prob_exceed_temp: NWS miss for {city}, using Open-Meteo fallback")
                return self._open_meteo.prob_exceed_temp(coords[0], coords[1], threshold_f, target_date)
            return 0.5

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
            coords = self._get_coords(city)
            if coords:
                log.info(f"prob_exceed_temp: no NWS values for {city} on {target_date}, using Open-Meteo")
                return self._open_meteo.prob_exceed_temp(coords[0], coords[1], threshold_f, target_date)
            return 0.5

        forecast_max = max(temps_f)
        days_out = (target_date - datetime.now(timezone.utc).date()).days
        sigma = max(2.0, min(6.0, 2.0 + days_out * 0.6))
        z = (forecast_max - threshold_f) / sigma
        return round(_normal_cdf(z), 4)

    def prob_rain(self, city: str, target_date: datetime.date) -> float:
        """Returns NWS probability of precipitation on target_date (0.0–1.0). Open-Meteo fallback."""
        data = self.get_forecast(city)
        probs = []
        if data:
            for v in data.get("probabilityOfPrecipitation", {}).get("values", []):
                ts = v["validTime"].split("/")[0]
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if dt.date() == target_date and v["value"] is not None:
                    probs.append(v["value"] / 100.0)
        if probs:
            return max(probs)
        coords = self._get_coords(city)
        if coords:
            log.info(f"prob_rain: NWS miss for {city}, using Open-Meteo fallback")
            return self._open_meteo.prob_rain(coords[0], coords[1], target_date)
        return 0.5

    def prob_snow(self, city: str, target_date: datetime.date, inches_threshold: float = 0.1) -> float:
        """
        Returns probability of snowfall on target_date meeting inches_threshold.
        NWS primary, Open-Meteo fallback (global coverage).
        """
        data = self.get_forecast(city)
        nws_hit = False

        if data:
            # Primary: snowfallAmount (mm → inches)
            snow_vals = []
            for v in data.get("snowfallAmount", {}).get("values", []):
                ts = v["validTime"].split("/")[0]
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if dt.date() == target_date and v["value"] is not None:
                    snow_vals.append(v["value"] * 0.0393701)
            if snow_vals:
                nws_hit = True
                expected = max(snow_vals)
                if expected <= 0:
                    return 0.02
                ratio = expected / inches_threshold
                return round(min(ratio / (1 + ratio), 0.97), 3)

            # Internal fallback: precip × cold-temp probability
            precip_prob = self.prob_rain(city, target_date)
            max_temp    = self.max_temp_on_date(city, target_date)
            if max_temp is not None:
                nws_hit = True
                temp_snow_p = 1 / (1 + math.exp((max_temp - 34) / 3))
                return round(precip_prob * temp_snow_p, 3)

        # Open-Meteo fallback
        coords = self._get_coords(city)
        if coords:
            log.info(f"prob_snow: NWS miss for {city}, using Open-Meteo fallback")
            return self._open_meteo.prob_snow(coords[0], coords[1], target_date, inches_threshold)
        return 0.5

    def prob_wind(self, city: str, mph_threshold: int, target_date: datetime.date) -> float:
        """Returns NWS probability of wind gusts exceeding mph_threshold. Open-Meteo fallback."""
        data = self.get_forecast(city)
        probs = []
        if data:
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
            closest = min(field_map.keys(), key=lambda x: abs(x - mph_threshold))
            field = field_map[closest]
            for v in data.get(field, {}).get("values", []):
                ts = v["validTime"].split("/")[0]
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if dt.date() == target_date and v["value"] is not None:
                    probs.append(v["value"] / 100.0)
        if probs:
            return max(probs)
        coords = self._get_coords(city)
        if coords:
            log.info(f"prob_wind: NWS miss for {city}, using Open-Meteo fallback")
            return self._open_meteo.prob_wind(coords[0], coords[1], mph_threshold, target_date)
        return 0.5


# ─────────────────────────────────────────────────────────────
# OPEN-METEO CLIENT  — global fallback, no API key required
# Expands NWS's 38-city US limit to any lat/lon globally.
# ─────────────────────────────────────────────────────────────
class OpenMeteoClient:
    """
    Fetches hourly weather from Open-Meteo (open-meteo.com).
    Covers the entire globe, no API key needed.
    Used as fallback when NWS returns None (city not in CITY_COORDS or NWS outage).
    """
    BASE_URL = "https://api.open-meteo.com/v1/forecast"
    _cache: Dict[str, Tuple[float, dict]] = {}
    CACHE_TTL = 1800

    def get_forecast(self, lat: float, lon: float) -> Optional[dict]:
        key = f"{lat:.4f},{lon:.4f}"
        now = time.time()
        if key in self._cache:
            ts, data = self._cache[key]
            if now - ts < self.CACHE_TTL:
                return data
        params = {
            "latitude": lat,
            "longitude": lon,
            "hourly": "temperature_2m,precipitation_probability,precipitation,snowfall,windspeed_10m,windgusts_10m",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,snowfall_sum,windgusts_10m_max,precipitation_probability_max",
            "temperature_unit": "fahrenheit",
            "windspeed_unit": "mph",
            "precipitation_unit": "inch",
            "timezone": "auto",
            "forecast_days": 8,
        }
        try:
            r = requests.get(self.BASE_URL, params=params, timeout=10)
            r.raise_for_status()
            data = r.json()
            self._cache[key] = (now, data)
            return data
        except Exception as e:
            log.error(f"Open-Meteo fetch failed ({lat},{lon}): {e}")
            return None

    def prob_exceed_temp(self, lat: float, lon: float, threshold_f: float, target_date: datetime.date) -> float:
        data = self.get_forecast(lat, lon)
        if not data:
            return 0.5
        daily = data.get("daily", {})
        dates = daily.get("time", [])
        maxes = daily.get("temperature_2m_max", [])
        for d, mx in zip(dates, maxes):
            if datetime.strptime(d, "%Y-%m-%d").date() == target_date and mx is not None:
                days_out = (target_date - datetime.now(timezone.utc).date()).days
                sigma = max(2.0, min(6.0, 2.0 + days_out * 0.6))
                z = (mx - threshold_f) / sigma
                return round(_normal_cdf(z), 4)
        return 0.5

    def prob_rain(self, lat: float, lon: float, target_date: datetime.date) -> float:
        data = self.get_forecast(lat, lon)
        if not data:
            return 0.5
        daily = data.get("daily", {})
        dates = daily.get("time", [])
        probs = daily.get("precipitation_probability_max", [])
        for d, p in zip(dates, probs):
            if datetime.strptime(d, "%Y-%m-%d").date() == target_date and p is not None:
                return p / 100.0
        return 0.5

    def prob_snow(self, lat: float, lon: float, target_date: datetime.date, inches_threshold: float = 0.1) -> float:
        data = self.get_forecast(lat, lon)
        if not data:
            return 0.5
        daily = data.get("daily", {})
        dates = daily.get("time", [])
        snow  = daily.get("snowfall_sum", [])
        for d, s in zip(dates, snow):
            if datetime.strptime(d, "%Y-%m-%d").date() == target_date and s is not None:
                if s <= 0:
                    return 0.02
                ratio = s / inches_threshold
                return round(min(ratio / (1 + ratio), 0.97), 3)
        return 0.5

    def prob_wind(self, lat: float, lon: float, mph_threshold: float, target_date: datetime.date) -> float:
        data = self.get_forecast(lat, lon)
        if not data:
            return 0.5
        daily = data.get("daily", {})
        dates = daily.get("time", [])
        gusts = daily.get("windgusts_10m_max", [])
        for d, g in zip(dates, gusts):
            if datetime.strptime(d, "%Y-%m-%d").date() == target_date and g is not None:
                days_out = (target_date - datetime.now(timezone.utc).date()).days
                sigma = max(3.0, min(8.0, 3.0 + days_out * 0.5))
                z = (g - mph_threshold) / sigma
                return round(_normal_cdf(z), 4)
        return 0.5

    def max_temp_on_date(self, lat: float, lon: float, target_date: datetime.date) -> Optional[float]:
        data = self.get_forecast(lat, lon)
        if not data:
            return None
        daily = data.get("daily", {})
        dates = daily.get("time", [])
        maxes = daily.get("temperature_2m_max", [])
        for d, mx in zip(dates, maxes):
            if datetime.strptime(d, "%Y-%m-%d").date() == target_date:
                return mx
        return None


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
        # Extract inch threshold: "3+ inches", "at least 2 inches", "1 inch of snow"
        m = re.search(r'(\d+(?:\.\d+)?)\s*\+?\s*inch(?:es)?', q)
        result["threshold"] = float(m.group(1)) if m else 0.1
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
    # "this weekend"
    elif "this weekend" in q:
        days_until_sat = (5 - today.weekday()) % 7
        result["date"] = today + timedelta(days=days_until_sat if days_until_sat > 0 else 7)
    # "this week" / "current week"
    elif "this week" in q or "current week" in q:
        result["date"] = today + timedelta(days=2)  # mid-week estimate
    # "week of May 12", "week of may 9th"
    elif re.search(r'week\s+of\s+\w+\s+\d{1,2}', q):
        m = re.search(r'week\s+of\s+(\w+)\s+(\d{1,2})', q)
        if m:
            month_map2 = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
                          "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12}
            mon_str = m.group(1)[:3].lower()
            day_num = int(m.group(2))
            mon_num = month_map2.get(mon_str, today.month)
            year = today.year
            candidate = datetime(year, mon_num, day_num).date()
            if candidate < today:
                candidate = datetime(year + 1, mon_num, day_num).date()
            result["date"] = candidate + timedelta(days=3)  # mid-week
    # "next Monday/Tuesday/..."
    elif re.search(r'next\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)', q):
        dow_map = {"monday":0,"tuesday":1,"wednesday":2,"thursday":3,
                   "friday":4,"saturday":5,"sunday":6}
        m = re.search(r'next\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)', q)
        target_dow = dow_map[m.group(1)]
        days_ahead = (target_dow - today.weekday() + 7) % 7
        if days_ahead == 0:
            days_ahead = 7  # "next X" = the one after the coming one
        result["date"] = today + timedelta(days=days_ahead)
    # "on Monday/Tuesday/..." (without "next") = nearest upcoming
    elif re.search(r'\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b', q):
        dow_map = {"monday":0,"tuesday":1,"wednesday":2,"thursday":3,
                   "friday":4,"saturday":5,"sunday":6}
        m = re.search(r'\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b', q)
        target_dow = dow_map[m.group(1)]
        days_ahead = (target_dow - today.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7
        result["date"] = today + timedelta(days=days_ahead)
    # specific month/day: "may 8", "may 9th", "may 9", "9th", "the 12th"
    else:
        month_map = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
                     "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12}
        # "May 9th" / "may 12"
        matched = False
        for mon, num in month_map.items():
            m = re.search(rf'\b{mon}\w*\s+(\d{{1,2}})(?:st|nd|rd|th)?\b', q)
            if m:
                day = int(m.group(1))
                year = today.year
                candidate = datetime(year, num, day).date()
                if candidate < today:
                    candidate = datetime(year + 1, num, day).date()
                result["date"] = candidate
                matched = True
                break
        # Ordinal-only: "on the 12th", "by the 9th" — assume current or next month
        if not matched:
            m = re.search(r'\bthe\s+(\d{1,2})(?:st|nd|rd|th)\b', q)
            if m:
                day = int(m.group(1))
                year = today.year
                try:
                    candidate = datetime(year, today.month, day).date()
                    if candidate < today:
                        # Roll to next month
                        next_month = today.month % 12 + 1
                        next_year  = year if next_month > 1 else year + 1
                        candidate  = datetime(next_year, next_month, day).date()
                    result["date"] = candidate
                except ValueError:
                    pass  # invalid day for month — keep today default

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

        elif mtype == "snow":
            # threshold in inches — extracted from question or default 0.1"
            threshold = parsed.get("threshold")
            inches = float(threshold) if threshold else 0.1
            nws_prob = self.nws.prob_snow(city, date, inches_threshold=inches)
            if dirn == "below":
                nws_prob = 1.0 - nws_prob  # "will it NOT snow?"

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

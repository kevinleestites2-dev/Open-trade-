"""Quick integration test for the fixed KalshiClient."""
import sys
sys.path.insert(0, ".")
from kalshi_client import KalshiClient

c = KalshiClient()
print(f"Stub mode: {c._stub}")

bal = c.get_balance()
print(f"Balance: ${bal:.2f}")

markets = c.get_markets(limit=5)
print(f"Markets fetched: {len(markets)}")
for m in markets[:3]:
    print(f"  - {m.get('title','')[:60]}")

print("\nKalshiClient OK")

# ⚡ OpenTrade - Autonomous Polymarket Trading Bot

The most advanced self-contained trading bot for Polymarket. Runs 24/7 with zero human intervention. Learns and adapts continuously.

## Features

- **10 Trading Strategies** with take profit & stop loss
- **Fully Autonomous** - zero human intervention required
- **Multi-Layer Risk Management** - hard-coded, cannot be overridden
- **Self-Learning** - adapts parameters, detects regimes, extracts skills
- **Self-Healing** - auto-restart on crash, auto-update from GitHub
- **Telegram Integration** - real-time alerts and command control
- **One-Command Install** - deploy in under 5 minutes

---

## Quick Install

```bash
curl -fsSL https://raw.githubusercontent.com/kevinleestites2-dev/Open-trade-/main/install.sh | bash
```

---

## Strategies

| # | Strategy | Description | Take Profit | Stop Loss |
|---|----------|-------------|-------------|-----------|
| 1 | Dump-and-Hedge | Detect 15% drops in crypto 15-min markets, buy dumped side | 5% | 10% |
| 2 | YES+NO Arbitrage | Buy both sides when YES+NO < 0.99 | 1-3% | 5% |
| 3 | Yield Farming LP | Limit orders both sides, earn USDC rewards | Daily | 10% |
| 4 | Maker Rebate MM | Capture spread + 20-25% taker fee rebates | Per fill | 10% |
| 5 | Copy Trading | Follow top wallets (RN1, Domer, ColdMath) | 5-10% | 10% |
| 6 | Grid Trading | Laddered orders at 5-10 price levels | Per grid | 10% |
| 7 | Flash Loan Arb | Aave V3 flash loan for cross-platform arb | Atomic | None |
| 8 | Grind Trading | High-frequency small profits (0.5-2%) | 0.5-2% | 10% |
| 9 | Day Trading Momentum | Follow trend in first 2-3 minutes | 3-5% | 10% |
| 10 | Mean Reversion | Bet on reversal when price overextends | 2-4% | 10% |

---

## Risk Management (HARD CODED)

These limits **cannot be overridden** by any strategy, optimizer, or parameter:

| Limit | Value | Action |
|-------|-------|--------|
| Stop Loss per trade | 10% | Exit immediately |
| Daily Loss Limit | 5% | Pause 24 hours, auto-resume |
| Drawdown from Peak | 25% | Emergency halt, notify, wait 1 day |
| Total Loss Halt | 40% | Permanent stop, manual restart required |
| Position Size Risk | 5% max | Per-trade limit |
| Market Exposure | 20% max | Per-market cap |
| Strategy Exposure | 50% max | Per-strategy cap |
| Consecutive Losses | 3 | Pause strategy 10 minutes |

---

## Learning System

### Performance Tracking
Every trade is logged with: timestamp, strategy, P&L, market conditions (volatility, volume, time of day), execution latency, slippage, fee paid.

### Strategy Weighting
Every hour, the bot calculates rolling 24h P&L per strategy and reallocates capital proportionally.

### Parameter Optimization
After every 20 trades per strategy, tests small parameter variations and deploys the best-performing set.

### Market Regime Detection
Classifies market state every 15 minutes:
- **High/Low Volatility** (BTC move >2% = high)
- **Trending/Ranging** (momentum ratio proxy for ADX >25)
- **High/Low Liquidity** (order book depth)

Switches strategy weights based on detected regime.

### Skill Extraction
When a trade wins by >10%, extracts the exact setup (parameters, market conditions, entry signals) as a reusable skill saved to `skills/`.

### Failure Reflection
For every losing trade, logs the reason. If same reason appears 3+ times in 24 hours, automatically adjusts parameters or pauses the responsible strategy.

---

## Installation

### Prerequisites
- Python 3.10+
- Polygon wallet with USDC
- Polymarket API credentials (get from https://polymarket.com)
- Telegram bot token (optional, for alerts)

### Manual Install

```bash
git clone https://github.com/kevinleestites2-dev/Open-trade-.git
cd Open-trade-
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your credentials
python3 trade.py --simulate --check-config
```

### Configuration

Edit `.env` with your credentials:

```env
PRIVATE_KEY=your_polygon_private_key
PROXY_WALLET_ADDRESS=your_proxy_wallet
POLYMARKET_API_KEY=your_api_key
POLYMARKET_API_SECRET=your_api_secret
POLYMARKET_API_PASSPHRASE=your_passphrase
TELEGRAM_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
INITIAL_CAPITAL=1000
```

---

## Usage

### Run in Simulation Mode
```bash
python3 trade.py --simulate
```

### Run Live
```bash
python3 trade.py
```

### Check Configuration
```bash
python3 trade.py --check-config
```

### Custom Capital
```bash
python3 trade.py --capital 5000
```

### Service Management (systemd)
```bash
sudo systemctl start zeus-prime
sudo systemctl stop zeus-prime
sudo systemctl status zeus-prime
sudo journalctl -u zeus-prime -f
```

---

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/status` | Current equity, P&L, positions, regime |
| `/strategies` | All strategy weights, win rates, P&L |
| `/pause` | Pause all trading |
| `/resume` | Resume all trading |

### Alert Types
- Trade execution (strategy, size, price, P&L)
- Take profit hit
- Stop loss triggered
- Daily P&L summary (08:00 UTC)
- Weekly performance report (Monday 08:00 UTC)
- Circuit breaker triggers
- Bot start/stop/restart
- Errors
- Low balance (<$100)

---

## Architecture

```
trade.py                 # Main bot (all logic)
├── Config               # Environment configuration
├── Database             # SQLite trade logging
├── PolymarketClient     # CLOB API + order signing
├── WebSocketManager     # Real-time market data
├── RiskManager          # Multi-layer risk control
├── TelegramBot          # Alerts & commands
├── RegimeDetector       # Market state classification
├── ParameterOptimizer   # Adaptive parameter tuning
├── SkillExtractor       # Winning trade extraction
├── FailureReflector     # Loss analysis & adjustment
├── AutonomyEngine       # Strategy orchestration
├── 10 Strategy Classes  # Trading logic
└── OpenTradeBot         # Main orchestrator
```

---

## Technical Specs (2026 Polymarket)

- **WebSocket**: `wss://ws-subscriptions-clob.polymarket.com/ws/` (no REST polling)
- **Fee-aware signing**: `feeRateBps` included in all signed orders
- **Live fee query**: `GET /fee-rate?tokenID={token_id}` before every order
- **Sub-100ms cancel/replace loop**
- **Multi-asset**: BTC, ETH, SOL, XRP 15-min + all markets >$50k volume
- **Proxy wallet**: `signature_type=2` with funder address
- **Native USDC only**: `0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359` on Polygon

---

## File Structure

```
Open-trade-/
├── trade.py              # Main bot (all strategies + infrastructure)
├── install.sh            # One-command installer
├── requirements.txt      # Python dependencies
├── .env.example          # Configuration template
├── README.md             # This file
├── skills/               # Extracted winning skills (JSON)
│   └── .gitkeep
└── logs/                 # Trade logs and error logs
    └── .gitkeep
```

---

## Troubleshooting

### Bot won't start
1. Check `.env` configuration: `python3 trade.py --check-config`
2. Ensure Python 3.10+: `python3 --version`
3. Check logs: `cat logs/open_trade_*.log`

### No trades executing
1. Verify USDC balance in proxy wallet
2. Check API credentials are correct
3. Run in simulate mode first: `python3 trade.py --simulate`
4. Check Telegram for error alerts

### High losses
1. The bot has hard-coded 10% stop loss per trade
2. Daily loss limit (5%) will auto-pause for 24h
3. 25% drawdown triggers emergency halt
4. Check if a strategy is underperforming: `/strategies` in Telegram

### WebSocket disconnects
The bot auto-reconnects with exponential backoff (1s → 60s max). Check your network connectivity and Polygon RPC endpoint.

### Auto-update not working
Ensure the git remote is accessible. The bot checks daily and pulls from `main` branch.

---

## Security

- Private keys are stored in `.env` (never committed)
- All orders are signed locally before submission
- Proxy wallet architecture (funds in proxy, not EOA)
- No external dependencies that phone home
- All risk limits are hard-coded and immutable at runtime

---

## License

MIT License. Use at your own risk. This is experimental trading software. Past performance does not guarantee future results. Always start with simulation mode.

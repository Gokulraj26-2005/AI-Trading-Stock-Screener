Intraday AI Trading Desk

VIA-Agent7 is an NSE intraday equity analysis, order-flow screening, and semi-automated execution platform built with **FastAPI**, **React + TypeScript**, **Lightweight Charts**, **ZeroMQ**, and **DhanHQ v2 / MCP APIs**.

> [!WARNING]
> **Trading Risk & Safeguards:** Algorithmic trading carries financial risk. Always verify behavior in mock/sandbox mode (`USE_DHAN_MOCK=true`) before enabling live broker connectivity, and keep `PRODUCTION_MODE=true` + `FAIL_CLOSED_ON_LIVE_ERROR=true` when running live.

---

## Key Features

- **Multi-Timeframe Chart Analysis (`/analysis`):**
  - Resamples daily OHLCV candles into **1D**, **1W**, and **1M** timeframes with EMA (`20`/`50`), SMA (`50`/`200`), anchored VWAP (`week`/`month`/`multi_year`), Bollinger squeeze percentiles, ATR, consolidation zones, and MA touch/bounce detection.
  - Combines technical structure with an **Institutional Flow Profile (IFP)** and **FinBERT** (`ProsusAI/finbert`) 24-hour news sentiment alignment (with a rule-based fallback when PyTorch/Transformers are not installed).
- **Live Short Screener (`/short-screener`):**
  - Continuously scores and ranks NSE equities (`TRADING_SYMBOLS`: NIFTY 50, NIFTY Next 50, NIFTY Midcap 100, NIFTY Smallcap 250) by **Wilder 14 RSI**, RSI velocity (`is_rapid_rsi`), FinBERT news catalyst score, and IFP score over REST and WebSocket (`/ws/short-screener`).
- **Directional Live Desks & Watchdogs (`/shortlisted`, `/shortlisted-shorts`):**
  - Isolated **Long** (`PromotionManager` + `AlgoLiveWatchdog`) and **Short** (`PromotionManagerShorts` + `AlgoLiveWatchdogShorts`) tracking desks with automated promotion gates (potential score, sustained ticks, 5m RSI bounds, RVOL, trigger proximity, and `15:00 IST` MIS cutoff) plus auto-purge invalidation.
  - **Capital & Risk Controls (`CapitalSlotManager`):** Default ₹1,80,000 total capital, ₹60,000 per-trade allocation, max 3 concurrent slots with priority queuing, 1.0% hard stop-loss floor, +0.8% breakeven lock, structural VWAP/EMA20 trailing, RSI order-flow exhaustion exits, and an emergency `kill-all` endpoint.
- **Charts & Order Flow Terminal (`/charts`):**
  - Interactive Lightweight Charts workspace (`1m`, `5m`, `15m`) with symbol autocomplete (`/api/chart/search`) and live tick/depth aggregation.
- **High-Throughput Market Data Pipeline:**
  - Parses Dhan binary WebSocket packets (`DhanWebSocket` standard feed + `DhanDepthWebSocket` 20-level or optional 200-level order book) and distributes ticks over ZeroMQ (`ZMQPublisher`) to `CandleAggregator`, `CVDEngine`, `VolVWAPEngine`, `CompressionEngine`, and `RegimeEngine`.
- **Telegram Human-in-the-Loop (HITL) Approval:**
  - Sends interactive `APPROVE` / `DECLINE` trade proposals to Telegram before routing orders through `DhanMCPClient` (`https://mcp.dhan.co/mcp` and `https://api.dhan.co/v2`), paired with startup broker reconciliation.

---

## DhanHQ API Cost & Requirements

| DhanHQ Plan / API | Cost | What It Covers | Required For |
|---|---|---|---|
| **Trading APIs** | **₹0 (Free)** | Order placement/modification/cancellation, positions, holdings, funds, order postbacks | Executing & reconciling orders with a Dhan account |
| **Data APIs** | **₹499 + GST / month (~₹500/mo)** | Live market WebSocket feed, historical daily & intraday candles, 20-level market depth (`DHAN_DEPTH_TIER=20`) | Live market streaming, chart history, and real-time screeners |
| **Pro Data APIs (Optional)** | **₹999 + GST / month** | Full **200-level** market depth (`DHAN_DEPTH_TIER=200`) and expanded feeds | Only needed if you switch `DHAN_DEPTH_TIER` from `20` to `200` |
| **Mock / Dev Mode** | **₹0 (Offline)** | Built-in `MockDhanWebSocket` synthetic tick simulator (`USE_DHAN_MOCK=true`) | Local testing, UI preview, and CI without a paid subscription |

> [!NOTE]
> Dhan bills the **Data API** subscription every 30 days directly from your Dhan trading ledger. You must maintain sufficient ledger balance in your Dhan account for the subscription to stay active.

---

## How to Connect the Dhan API

### 1. Generate Credentials on Dhan
1. Open [web.dhan.co](https://web.dhan.co) and log in to your Dhan account.
2. Go to **Profile** → **DhanHQ Trading & Data APIs**.
3. Subscribe to **Data APIs (₹499 + GST/month)** if you want live market data and historical candles.
4. Create an API App to obtain your **API Key** (`DHAN_API_KEY`) and **API Secret** (`DHAN_API_SECRET`), and note your numeric **Client ID** (`DHAN_USER_ID` / `DHAN_CLIENT_ID`).

### 2. Configure `.env`
Copy `.env.example` to `.env` in the repository root and fill in your credentials:

```env
TZ=Asia/Kolkata

# Numeric Dhan account/client ID (same value for both)
DHAN_USER_ID=your_numeric_client_id
DHAN_CLIENT_ID=your_numeric_client_id

# Dhan App credentials (from web.dhan.co -> DhanHQ APIs)
DHAN_API_KEY=your_dhan_app_id
DHAN_API_SECRET=your_dhan_app_secret

# Optional: paste a 24h access token directly, or leave empty and use UI login
DHAN_ACCESS_TOKEN=

# Live vs Sandbox/Mock
DHAN_ENV=live
DHAN_BASE_URL=https://api.dhan.co/v2
DHAN_ENABLED=true
USE_DHAN_MOCK=false
DHAN_DEPTH_TIER=20

# Security (generate a random 32+ character string)
JWT_SECRET=replace-with-a-long-random-32-plus-char-secret
PRODUCTION_MODE=false
FAIL_CLOSED_ON_LIVE_ERROR=false
FRONTEND_URL=http://localhost:5173
```

> [!IMPORTANT]
> Never commit `.env`, `backend/app/.dhan_token`, or `.db`/`.sqlite` files. They are excluded in `.gitignore`.

### 3. Authenticate (24-Hour Access Token Lifecycle)
Dhan access tokens expire after **24 hours**. You can connect in either of two ways:
- **Browser Consent Flow (Recommended):** Start the backend and frontend, click **Dhan Login** in the sidebar, and complete the Dhan browser login. The backend generates a consent session (`GET /api/dhan/login-url`), exchanges the returned `tokenId` (`POST /api/dhan/consume-token`), and automatically restarts the live pipeline.
- **Manual Token:** Generate a 24-hour Access Token in the Dhan web portal and set `DHAN_ACCESS_TOKEN` in `.env`.

Active tokens are persisted across restarts via `dhan_token_service.py` (SQLite `portfolio.db` → `backend/app/.dhan_token` → `.env`).

---

## Optional Integrations & Environment Variables

| Variable | Purpose |
|---|---|
| `OPENROUTER_API_KEY` | Enables AI-assisted shortlist ranking (`POST /api/shortlist/generate`) |
| `TELEGRAM_BOT_TOKEN` & `TELEGRAM_CHAT_ID` | Enables Telegram inline `APPROVE` / `DECLINE` trade notifications and audit reports |
| `TELEGRAM_WEBHOOK_SECRET` | Verifies incoming Telegram webhook requests via `X-Telegram-Bot-Api-Secret-Token` |
| `DHAN_WEBHOOK_SECRET` | Verifies Dhan order postbacks on `POST /api/broker/webhook` (`x-dhan-signature`) |
| `DISABLE_JWT_AUTH` | Local-only bypass toggle (must remain `false` in production) |
| `ALLOW_STREAM_TOKEN_QUERY` | Allows `?stream_token=...` on WebSocket endpoints |

---

## Project Structure

```text
├── .env.example                 # Environment template (copy to .env)
├── .github/workflows/ci.yml     # CI pipeline (backend pytest + frontend Vitest/Vite build)
├── docker-compose.yml           # Backend (port 8001) + Frontend (port 5173) + test profile
├── backend/
│   ├── Dockerfile               # Standard Python 3.11 backend image
│   ├── Dockerfile.slim          # CUDA 12.1 backend image for GPU FinBERT
│   ├── pytest.ini
│   ├── requirements.txt         # Full runtime dependencies (includes CUDA PyTorch/Transformers/Numba)
│   ├── requirements-ci.txt      # Lightweight CPU dependencies for CI & local dev without GPU
│   ├── app/
│   │   ├── app.py               # FastAPI assembly, security baseline checks & lifespan
│   │   ├── app_config.py        # Consolidated environment & quantitative configuration
│   │   ├── config/              # Symbol universe (TRADING_SYMBOLS) & capital/risk rules
│   │   ├── routers/             # auth, broker, charts, analysis, shortlist, watchdog, short_screener
│   │   ├── services/            # Dhan WS/historical/MCP, ZMQ, CVD, compression, watchdogs, FinBERT
│   │   └── state/               # ScanStateService & SessionService
│   ├── scripts/
│   │   └── migrate_to_duckdb.py # Optional SQLite -> DuckDB daily candle migration
│   └── tests/                   # Unit & integration test suites
└── frontend/
    ├── Dockerfile
    ├── vite.config.mjs          # Proxies /api and /ws to backend (default http://127.0.0.1:8001)
    └── src/
        ├── App.tsx              # Auth bootstrap, Dhan OAuth callback & route views
        ├── api.ts               # Typed API client & session token handling
        └── components/          # ShortScreenerLivePage, MultiTimeframeAnalysisPage, Desks, Charts
```

---

## Quick Start

### Option A: Docker Compose
1. Create your environment file:
   ```bash
   cp .env.example .env
   ```
2. Build and start the backend (`http://localhost:8001`) and frontend (`http://localhost:5173`):
   ```bash
   docker compose up --build
   ```
   *(Note: `docker-compose.yml` enables `gpus: all` on the `backend` service for CUDA FinBERT. Remove the `gpus: all` line or use `backend/Dockerfile` if deploying on a CPU-only host.)*

### Option B: Local Development (Python + Node.js)

**1. Backend (Python 3.11+):**
```bash
python -m venv .venv
# Windows: .venv\Scripts\activate  |  Linux/macOS: source .venv/bin/activate
pip install -r backend/requirements-ci.txt   # or backend/requirements.txt for GPU FinBERT/Numba
cd backend
PYTHONPATH=app uvicorn app.main:app --host 0.0.0.0 --port 8001
```

**2. Frontend (Node.js 20+):**
```bash
cd frontend
npm install
npm run dev
```
Open `http://localhost:5173` in your browser. Vite proxies `/api` and `/ws` requests to `http://127.0.0.1:8001` (override via `VITE_BACKEND_URL`).

---

## Runtime Data & Caches

Local databases, instrument masters, and logs are created automatically at runtime under `backend/app/` and `backend/logs/` (all gitignored):
- `backend/app/data/dhan_instruments.csv` — Auto-downloaded daily from Dhan (`api-scrip-master.csv`) by `DhanInstrumentResolver`.
- `backend/app/data/historical_candles.sqlite` (and optional `.duckdb`) — Cached historical daily OHLCV candles and news cache.
- `backend/app/portfolio.db`, `shortlist.sqlite`, `shortlist_shorts.sqlite`, `live_trigger_state.sqlite`, `watchdog_recordings.db`, `watchdog_settings.json` — Persistent local state.

---

## Running Tests

**Backend unit tests:**
```bash
PYTHONPATH=backend pytest -q backend/tests/unit
```

**Frontend tests & production build:**
```bash
cd frontend
npm run test -- --run
npm run build
```

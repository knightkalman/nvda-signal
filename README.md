# NVDA monitor (`nvda-signal`)

A small Python background worker that collects public NVIDIA Corporation (`NVDA`)
stock prices, plus NVIDIA/AI/semiconductor news, and appends one
machine-readable JSON object per collection cycle to a JSON Lines file.

## Run

Requires Python 3.10 or newer; there are no third-party dependencies.

```sh
python3 nvda_monitor.py
```

The worker runs continuously, polling every five minutes by default. Stop it
with Ctrl+C. To collect only one cycle:

```sh
python3 nvda_monitor.py --once
```

Choose a different interval or output location with:

```sh
python3 nvda_monitor.py --interval 600 --output data/nvda.jsonl
```

By default, it writes:

- `data/nvda_monitor.jsonl` — append-only collection records.
- `data/nvda_monitor_state.sqlite3` — IDs of news articles already emitted.

Each JSON Lines record has a UTC `collected_at` timestamp, `prices`, newly
published `news`, and an `errors` array. Price entries include a UTC market
timestamp, price, previous daily close, absolute and percentage change, and
`price_change_sentiment` (`bullish`, `bearish`, `neutral`, or `unknown`).
Sentiment describes the price move versus the previous daily close; it is not
an assessment of the news or investment advice.

### AI analysis of news

Each scheduled run passes newly collected headlines and feed summaries to
Google's managed **Antigravity agent** through the Gemini Interactions API.
At most once an hour, it also reassesses up to 5 older stories whose
assessments are at least 24 hours old, retaining the previous assessments.
For these reassessments, it provides observed `NVDA` price moves since
publication as context; the agent is instructed not to treat those moves as
proof that a story caused them. The schedule state is stored in
`data/nvda_analysis_state.json`.

To enable it, create a Gemini API key in [Google AI Studio](https://aistudio.google.com/app/apikey),
then add it to this public repository under **Settings** > **Secrets and
variables** > **Actions** > **New repository secret**, named exactly
`GEMINI_API_KEY`.

Price data comes from Yahoo Finance's public chart endpoint for `NVDA`,
combined with Massive's one-minute stock bars for `NVDA` when `MASSIVE_API_KEY`
is configured. News comes from Yahoo Finance NVDA RSS, Google News NVDA feed,
CNBC Technology & Business, and BBC Technology & Business. Broad technology and
business feeds are filtered for NVIDIA, GPU, AI chip, and semiconductor topics.

### Run on GitHub Actions and publish to GitHub Pages

The included `.github/workflows/nvda-monitor.yml` runs one collection every five
minutes and also supports manual runs from the repository's **Actions** tab.
Each run appends to that day's JSONL file under `data/nvda/`, saves the SQLite
news-deduplication database and reassessment schedule state, and commits them
to the repository.

The dashboard is deployed at
[`https://knightkalman.github.io/nvda-signal/`](https://knightkalman.github.io/nvda-signal/).

### Agent API (`https://knightkalman.github.io/nvda-signal/api/`)

The `NVDA monitor` workflow also builds an API-key-protected endpoint for
third-party agents at
[`https://knightkalman.github.io/nvda-signal/api/`](https://knightkalman.github.io/nvda-signal/api/)
(with the encrypted envelope at
`https://knightkalman.github.io/nvda-signal/api/index.json`).

During deployment, `build_api.py` aggregates `data/nvda/*.jsonl` and encrypts the
structured JSON payload using `AES-256-GCM` with a 256-bit key derived from the
repository secret `NVDA_SIGNAL_API_KEY` via `PBKDF2-HMAC-SHA256` (210,000
iterations). If `NVDA_SIGNAL_API_KEY` is not set, `build_api.py` fails closed and
writes an `unauthorized_stub` record without publishing any plaintext API data.

Once decrypted with `NVDA_SIGNAL_API_KEY`, the API payload provides:

- `prices` — latest `NVDA` price, change versus the previous daily close, quote
  validity (`valid`, `stale`, or `insufficient`), and provider observations.
- `price_direction_compression` — run-length encoded directional regimes
  (`regime_segments` and `current_regime`), directional `inflection_points`,
  point-to-point `step_changes` across deduplicated observations, and `24h`,
  `7d`, `30d`, and `all` OHLC `window_summaries`.
- `chart_data` — deduplicated time-series rows and New York-timezone (`America/New_York`)
  weekend market-closed carry-forward windows matching the interactive chart.
- `news_sentiment` — aggregate headline sentiment counts (`by_direction`,
  `recent_24h`, `net_sentiment_score`, and
  `confidence_weighted_sentiment_score`) plus the full list of collected
  articles and their latest Antigravity AI assessments (`direction`,
  `confidence`, `validity`, `summary`, `rationale`, and post-publication
  `market_context`).

#### Consuming the API from a Python agent

```sh
NVDA_SIGNAL_API_KEY="your-api-key" python3 nvda_signal_client.py
```

Or import `fetch_nvda_signal_api` directly:

```python
from nvda_signal_client import fetch_nvda_signal_api

data = fetch_nvda_signal_api(
    api_key="your-api-key",
    endpoint="https://knightkalman.github.io/nvda-signal/api/",
)
print(data["prices"]["nvda"])
print(data["price_direction_compression"]["nvda"]["current_regime"])
print(data["news_sentiment"]["summary"])
```

#### Consuming the API from a browser-capable agent

Open `https://knightkalman.github.io/nvda-signal/api/?api_key=<NVDA_SIGNAL_API_KEY>`
(or `#api_key=<NVDA_SIGNAL_API_KEY>`, or paste the key manually into the form on
`https://knightkalman.github.io/nvda-signal/api/`). The static gateway decrypts
`index.json` in memory via Web Crypto (`crypto.subtle`), strips the key from
the URL bar, exposes `window.NVDA_SIGNAL_API_DATA`, and renders the decrypted
JSON inside `<pre id="nvda-signal-api-json">`.

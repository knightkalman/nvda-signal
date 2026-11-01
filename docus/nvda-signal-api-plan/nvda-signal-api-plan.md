# NVDA Signal Agent API — Implementation Plan

## 1. Objective & Scope

Create a zero-infrastructure, API-key-protected API endpoint and public dashboard for **NVIDIA Corporation (`NVDA`)** hosted on GitHub Pages at:

- **Dashboard URL:** `https://knightkalman.github.io/nvda-signal/`
- **Endpoint URL:** `https://knightkalman.github.io/nvda-signal/api/` (and `https://knightkalman.github.io/nvda-signal/api/index.json`)

Modeled after `oil-signal`, the solution exposes the data powering the NVDA Signal chart (`site/index.html`) specifically structured and compressed for consumption by a 3rd-party autonomous agent. It provides:
1. **Current & Historical Price Information** (`NVDA` stock price in USD per share, quote validity, provider observations from Yahoo Finance and optional Massive Stocks API, and range summaries across `24h`, `7d`, `30d`, and `all`).
2. **Compressed Price Direction Changes** (run-length encoded / transition-compressed directional regimes, inflection points, point-to-point step changes, and NASDAQ weekend carry-forward windows matching the chart's logic: `Friday 16:00 America/New_York` to `Monday 09:30 America/New_York`).
3. **News Sentiments & AI Assessments** (aggregated sentiment counts + per-article Antigravity agent assessments: `direction`, `confidence`, `validity`, `summary`, `rationale`, and post-publication `market_context` NVDA price moves).
4. **API-Key-Only Access via Static Encrypted Envelope** (AES-256-GCM authenticated encryption derived from `NVDA_SIGNAL_API_KEY`, plus a Dual-Mode Web Gateway at `/api/` that either decrypts in-browser via manual key input or `?api_key=...` / `#api_key=...` or serves the encrypted JSON envelope for programmatic agent decryption).

---

## 2. Architecture Overview

```
┌────────────────────────┐     ┌─────────────────────────┐     ┌────────────────────────────┐
│   nvda_monitor.py      │────▶│    analyze_news.py      │────▶│     build_api.py           │
│ Collects NVDA quotes & │     │ Antigravity AI headline │     │ 1. Aggregates data/nvda/*.jsonl
│ AI/semiconductor news  │     │ sentiment & reassessment│     │ 2. Compresses direction    │
└────────────────────────┘     └─────────────────────────┘     │    changes & chart series  │
                                                               │ 3. Encrypts payload with   │
                                                               │    NVDA_SIGNAL_API_KEY     │
                                                               └──────────────┬─────────────┘
                                                                              │
                                                                              ▼
                                                               ┌────────────────────────────┐
                                                               │   GitHub Pages (site/api/) │
                                                               │ - /api/index.json          │
                                                               │   (AES-256-GCM Envelope)   │
                                                               │ - /api/index.html          │
                                                               │   (Agent Web/JS Gateway)   │
                                                               └────────────────────────────┘
```

### Cryptographic Envelope Design (`AES-256-GCM` + `PBKDF2-HMAC-SHA256`)
- **Secret Source:** Repository secret `NVDA_SIGNAL_API_KEY` passed to the GitHub Actions job during the `deploy-dashboard` build step (never committed to Git).
- **Key Derivation:**
  - Generate a random 16-byte `salt` and 12-byte `nonce` (IV) per build.
  - Derive a 32-byte (256-bit) key using `PBKDF2-HMAC-SHA256` (`iterations = 210_000`).
- **Encryption:**
  - Encrypt UTF-8 JSON payload using `AES-256-GCM` (producing ciphertext + 16-byte authentication tag).
  - Supports both Python `cryptography` and zero-dependency OpenSSL `libcrypto` via `ctypes`.

---

## 3. Data & Payload Schema Specification

### 3.1. Encrypted Envelope (`site/api/index.json`)
```json
{
  "schema_version": 1,
  "endpoint": "https://knightkalman.github.io/nvda-signal/api/",
  "auth_required": true,
  "status": "encrypted",
  "generated_at": "2026-11-01T16:15:00Z",
  "encryption": {
    "algorithm": "AES-256-GCM",
    "kdf": "PBKDF2-HMAC-SHA256",
    "iterations": 210000,
    "salt_b64": "<16-byte random salt base64>",
    "nonce_b64": "<12-byte random IV base64>",
    "tag_b64": "<16-byte GCM auth tag base64>",
    "payload_encoding": "json+utf8"
  },
  "ciphertext_b64": "<base64 encoded encrypted JSON payload>"
}
```

### 3.2. Decrypted Agent Payload Schema
Once decrypted with `NVDA_SIGNAL_API_KEY`, the plaintext JSON provides:
- `prices.nvda` — latest `NVDA` quote, change vs. previous daily close, quote validity (`valid`, `stale`, `insufficient`), and provider observations.
- `price_direction_compression.nvda` — `current_regime`, `regime_segments`, `inflection_points`, `step_changes`, and `window_summaries` (`24h`, `7d`, `30d`, `all`).
- `chart_data` — `time_range`, `weekend_windows` (NASDAQ `Friday 16:00 ET` to `Monday 09:30 ET`), and `deduplicated_series.nvda`.
- `news_sentiment` — `summary` (`total_stories`, `assessed_stories`, `by_direction`, `dominant_direction`, `net_sentiment_score`, `confidence_weighted_sentiment_score`, `recent_24h`) and `articles` list with Antigravity AI analysis.

---

## 4. Verification Checklist

- [x] `python3 -m unittest discover -s tests` passes all unit tests (`test_nvda_monitor.py`, `test_analyze_news.py`, `test_build_api.py`).
- [x] `NVDA_SIGNAL_API_KEY` encrypts `site/api/index.json` using `AES-256-GCM` without leaking plaintext prices or headlines.
- [x] `nvda_signal_client.py` fetches and decrypts `https://knightkalman.github.io/nvda-signal/api/index.json`.
- [x] `https://knightkalman.github.io/nvda-signal/` and `https://knightkalman.github.io/nvda-signal/api/` are deployed and live on GitHub Pages.

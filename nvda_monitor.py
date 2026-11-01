#!/usr/bin/env python3
"""Collect public NVIDIA (NVDA) stock prices and semiconductor/AI news into a JSON Lines file."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


LOGGER = logging.getLogger("nvda_monitor")
USER_AGENT = "nvda-monitor/1.0 (public data collector)"
HTTP_TIMEOUT_SECONDS = 20
PRICE_SYMBOLS = {
    "nvda": "NVDA",
}
MASSIVE_TICKERS = {
    "nvda": "NVDA",
}
NVDA_KEYWORDS = (
    "nvidia",
    "nvda",
    "geforce",
    "cuda",
    "tensorrt",
    "blackwell",
    "hopper",
    "h100",
    "h200",
    "b200",
    "gb200",
    "rubin",
    "jensen huang",
    "dgx",
    "nvlink",
    "mellanox",
    "omniverse",
    "ai chip",
    "ai gpu",
    "data center gpu",
)
NEWS_FEEDS = {
    "yahoo_nvda": {
        "url": "https://feeds.finance.yahoo.com/rss/2.0/headline?s=NVDA&region=US&lang=en-US",
        "keywords": (),
    },
    "google_news_nvda": {
        "url": "https://news.google.com/rss/search?q=NVIDIA+OR+NVDA+when:7d&hl=en-US&gl=US&ceid=US:en",
        "keywords": (),
    },
    "cnbc_technology": {
        "url": "https://www.cnbc.com/id/19854910/device/rss/rss.html",
        "keywords": NVDA_KEYWORDS,
    },
    "cnbc_business": {
        "url": "https://www.cnbc.com/id/10001147/device/rss/rss.html",
        "keywords": NVDA_KEYWORDS,
    },
    "bbc_technology": {
        "url": "https://feeds.bbci.co.uk/news/technology/rss.xml",
        "keywords": NVDA_KEYWORDS,
    },
    "bbc_business": {
        "url": "https://feeds.bbci.co.uk/news/business/rss.xml",
        "keywords": NVDA_KEYWORDS,
    },
}
PRICE_HISTORY_RANGE = "1d"
PRICE_HISTORY_INTERVAL = "1m"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def request_bytes(url: str, headers: dict[str, str] | None = None) -> bytes:
    request_headers = {"User-Agent": USER_AGENT}
    if headers:
        request_headers.update(headers)
    request = Request(url, headers=request_headers)
    with urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        return response.read()


def sentiment_for_change(
    price: float, previous_close: float | None
) -> tuple[float | None, float | None, str]:
    if previous_close is None or previous_close == 0:
        return None, None, "unknown"

    change = price - previous_close
    change_percent = change / previous_close * 100
    sentiment = "bullish" if change > 0 else "bearish" if change < 0 else "neutral"
    return round(change, 6), round(change_percent, 4), sentiment


def fetch_yahoo_price(name: str, symbol: str) -> dict[str, object]:
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{quote(symbol, safe='')}?range={PRICE_HISTORY_RANGE}"
        f"&interval={PRICE_HISTORY_INTERVAL}"
    )
    payload = json.loads(request_bytes(url))
    chart = payload.get("chart", {})
    if chart.get("error"):
        raise ValueError(f"price provider returned an error: {chart['error']}")
    results = chart.get("result") or []
    if not results:
        raise ValueError("price provider returned no chart data")

    result = results[0]
    metadata = result.get("meta", {})
    quote_data = (result.get("indicators", {}).get("quote") or [{}])[0]
    timestamps = result.get("timestamp") or []
    timestamp = metadata.get("regularMarketTime")
    if timestamp is None and timestamps:
        timestamp = timestamps[-1]
    closes = quote_data.get("close", [])
    history = [
        {
            "timestamp": datetime.fromtimestamp(ts, timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
            "price": float(close),
        }
        for ts, close in zip(timestamps, closes)
        if ts is not None and close is not None
    ]
    current_price = metadata.get("regularMarketPrice")
    if current_price is None and history:
        current_price = history[-1]["price"]
    if current_price is None:
        raise ValueError("price provider returned no current price")

    previous_close = metadata.get("chartPreviousClose")
    if previous_close is None and history:
        exchange_timezone = metadata.get("exchangeTimezoneName")
        try:
            market_timezone = (
                ZoneInfo(exchange_timezone) if exchange_timezone else timezone.utc
            )
        except ZoneInfoNotFoundError:
            LOGGER.warning(
                "Unknown exchange timezone %r; using UTC for close fallback",
                exchange_timezone,
            )
            market_timezone = timezone.utc
        current_market_date = (
            datetime.fromtimestamp(timestamp, timezone.utc)
            .astimezone(market_timezone)
            .date()
        )
        previous_day_closes = [
            point["price"]
            for point in history
            if datetime.fromisoformat(point["timestamp"].replace("Z", "+00:00"))
            .astimezone(market_timezone)
            .date()
            < current_market_date
        ]
        previous_close = previous_day_closes[-1] if previous_day_closes else None

    quote_timestamp = (
        datetime.fromtimestamp(timestamp, timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
        if timestamp is not None
        else None
    )
    history = (
        [{"timestamp": quote_timestamp, "price": float(current_price)}]
        if quote_timestamp
        else []
    )

    change, change_percent, sentiment = sentiment_for_change(
        float(current_price),
        float(previous_close) if previous_close is not None else None,
    )
    return {
        "name": name,
        "symbol": symbol,
        "price": float(current_price),
        "currency": metadata.get("currency", "USD"),
        "unit": "share",
        "price_timestamp": quote_timestamp,
        "history": history,
        "previous_close": (
            float(previous_close) if previous_close is not None else None
        ),
        "change": change,
        "change_percent": change_percent,
        "change_basis": "previous_daily_close",
        "price_change_sentiment": sentiment,
        "source": "Yahoo Finance chart API",
        "source_url": url,
    }


def fetch_massive_price(
    name: str, ticker: str, api_key: str
) -> dict[str, object]:
    headers = {"Authorization": f"Bearer {api_key}"}
    today = datetime.now(timezone.utc).date().isoformat()
    aggregates_url = (
        f"https://api.massive.com/v2/aggs/ticker/{quote(ticker, safe='')}"
        f"/range/1/minute/1970-01-01/{today}?adjusted=true&sort=desc&limit=1"
    )
    aggregate_payload = json.loads(request_bytes(aggregates_url, headers=headers))
    aggregates = aggregate_payload.get("results") or []
    valid_aggregates = [
        aggregate
        for aggregate in aggregates
        if isinstance(aggregate, dict)
        and isinstance(aggregate.get("c"), (int, float))
        and isinstance(aggregate.get("t"), (int, float))
    ]
    if not valid_aggregates:
        raise ValueError(f"Massive returned no one-minute bars for {ticker}")
    latest = max(valid_aggregates, key=lambda item: item["t"])
    raw_ts = float(latest["t"])
    # Support millisecond or nanosecond epoch timestamps
    epoch_seconds = raw_ts / 1_000_000_000 if raw_ts > 1e14 else raw_ts / 1_000
    timestamp = (
        datetime.fromtimestamp(epoch_seconds, timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    return {
        "name": name,
        "symbol": ticker,
        "price": float(latest["c"]),
        "currency": "USD",
        "unit": "share",
        "price_timestamp": timestamp,
        "history": [{"timestamp": timestamp, "price": float(latest["c"])}],
        "previous_close": None,
        "change": None,
        "change_percent": None,
        "change_basis": "previous_daily_close",
        "price_change_sentiment": "unknown",
        "source": "Massive Stocks API",
        "source_url": aggregates_url,
    }


def _minute_timestamp(value: str) -> datetime:
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    timestamp = timestamp.astimezone(timezone.utc)
    return timestamp.replace(second=0, microsecond=0)


def _quote_datetime(quote: dict[str, object]) -> datetime | None:
    timestamp = quote.get("price_timestamp")
    if not isinstance(timestamp, str):
        return None
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(
            timezone.utc
        )
    except ValueError:
        return None


def combine_price_quotes(
    name: str,
    symbol: str,
    yahoo_quote: dict[str, object] | None,
    massive_quote: dict[str, object] | None,
    provider_errors: list[str] | None = None,
) -> dict[str, object]:
    """Average providers only when their latest prices belong to the same minute."""
    quotes = [quote for quote in (yahoo_quote, massive_quote) if quote is not None]
    if not quotes:
        raise ValueError(f"No price providers returned a quote for {name}")

    selected_quotes = quotes
    if len(quotes) == 2:
        quote_times = [_quote_datetime(quote) for quote in quotes]
        if all(quote_times):
            if _minute_timestamp(quote_times[0].isoformat()) != _minute_timestamp(
                quote_times[1].isoformat()
            ):
                selected_quotes = [
                    max(zip(quotes, quote_times), key=lambda pair: pair[1])[0]
                ]
        else:
            timestamped_quotes = [
                (quote, timestamp)
                for quote, timestamp in zip(quotes, quote_times)
                if timestamp is not None
            ]
            if timestamped_quotes:
                selected_quotes = [
                    max(timestamped_quotes, key=lambda pair: pair[1])[0]
                ]
            else:
                selected_quotes = [quotes[0]]

    price = sum(float(quote["price"]) for quote in selected_quotes) / len(
        selected_quotes
    )
    selected_timestamps = [
        quote.get("price_timestamp")
        for quote in selected_quotes
        if isinstance(quote.get("price_timestamp"), str)
    ]
    if len(selected_quotes) == 2 and selected_timestamps:
        price_timestamp = (
            _minute_timestamp(selected_timestamps[0])
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
    elif selected_timestamps:
        price_timestamp = selected_timestamps[0]
    else:
        price_timestamp = None

    selected_sources = [
        {
            "source": quote["source"],
            "price": float(quote["price"]),
            "timestamp": quote.get("price_timestamp"),
        }
        for quote in selected_quotes
    ]
    source_names = list(
        dict.fromkeys(str(quote["source"]) for quote in selected_quotes)
    )
    source_urls = list(
        dict.fromkeys(
            str(quote["source_url"])
            for quote in selected_quotes
            if quote.get("source_url")
        )
    )
    previous_close = (
        float(yahoo_quote["previous_close"])
        if yahoo_quote is not None
        and yahoo_quote.get("previous_close") is not None
        else None
    )
    change, change_percent, sentiment = sentiment_for_change(price, previous_close)
    result: dict[str, object] = {
        "name": name,
        "symbol": symbol,
        "price": price,
        "currency": "USD",
        "unit": "share",
        "price_timestamp": price_timestamp,
        "history": (
            [{"timestamp": price_timestamp, "price": price}]
            if price_timestamp
            else []
        ),
        "previous_close": previous_close,
        "change": change,
        "change_percent": change_percent,
        "change_basis": "previous_daily_close",
        "price_change_sentiment": sentiment,
        "source": " + ".join(source_names),
        "source_url": source_urls[0] if source_urls else "",
        "source_urls": source_urls,
        "provider_observations": selected_sources,
    }
    if provider_errors:
        result["provider_errors"] = provider_errors
    return result


def fetch_price(name: str, symbol: str) -> dict[str, object]:
    """Fetch Yahoo and optional Massive prices, using Yahoo when Massive is unavailable."""
    yahoo_quote = None
    massive_quote = None
    provider_errors = []
    try:
        yahoo_quote = fetch_yahoo_price(name, symbol)
    except Exception as error:
        provider_errors.append(f"Yahoo Finance: {type(error).__name__}: {error}")

    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if api_key:
        try:
            massive_quote = fetch_massive_price(
                name, MASSIVE_TICKERS[name], api_key
            )
        except Exception as error:
            provider_errors.append(f"Massive: {type(error).__name__}: {error}")
    else:
        LOGGER.info("MASSIVE_API_KEY is not set; using Yahoo Finance for %s", name)

    if provider_errors:
        for error in provider_errors:
            LOGGER.warning("Price provider fallback for %s: %s", name, error)
    if yahoo_quote is None and massive_quote is None:
        raise RuntimeError(
            "; ".join(provider_errors) or f"No price quote for {name}"
        )
    return combine_price_quotes(
        name, symbol, yahoo_quote, massive_quote, provider_errors or None
    )


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]*>", " ", value))).strip()


def parse_published_at(value: str | None) -> str | None:
    if not value:
        return None
    try:
        published = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    return (
        published.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def parse_feed(
    source: str,
    feed_url: str,
    content: bytes,
    keywords: tuple[str, ...] = (),
) -> list[dict[str, str | None]]:
    root = ET.fromstring(content)
    articles = []
    for item in root.findall(".//item"):
        title = clean_text(item.findtext("title"))
        link = clean_text(item.findtext("link"))
        if not title or not link:
            continue
        summary = clean_text(item.findtext("description"))
        if keywords and not any(
            keyword in f"{title} {summary}".casefold() for keyword in keywords
        ):
            continue
        identifier = clean_text(item.findtext("guid")) or link
        articles.append(
            {
                "id": f"{source}:{identifier}",
                "source": source,
                "title": title,
                "url": urljoin(feed_url, link),
                "published_at": parse_published_at(item.findtext("pubDate")),
                "summary": summary,
                "feed_url": feed_url,
            }
        )
    return articles


def initialize_state(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS seen_news (
            id TEXT PRIMARY KEY,
            first_seen_at TEXT NOT NULL
        )
        """
    )
    connection.commit()


def collect_cycle(connection: sqlite3.Connection, output_path: Path) -> dict:
    record = {
        "schema_version": 1,
        "collected_at": utc_now(),
        "prices": {},
        "news": [],
        "errors": [],
    }

    for name, symbol in PRICE_SYMBOLS.items():
        try:
            record["prices"][name] = fetch_price(name, symbol)
        except Exception as error:
            LOGGER.error("Could not fetch %s price: %s", name, error)
            record["errors"].append(
                {
                    "source": f"price:{name}",
                    "error": f"{type(error).__name__}: {error}",
                }
            )

    news_ids = set()
    for source, feed_config in NEWS_FEEDS.items():
        feed_url = feed_config["url"]
        try:
            articles = parse_feed(
                source,
                feed_url,
                request_bytes(feed_url),
                feed_config["keywords"],
            )
            for article in articles:
                article_id = article["id"]
                if article_id in news_ids:
                    continue
                news_ids.add(article_id)
                existing = connection.execute(
                    "SELECT 1 FROM seen_news WHERE id = ?", (article_id,)
                ).fetchone()
                if existing is None:
                    record["news"].append(article)
        except Exception as error:
            LOGGER.error("Could not fetch %s news: %s", source, error)
            record["errors"].append(
                {
                    "source": f"news:{source}",
                    "error": f"{type(error).__name__}: {error}",
                }
            )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
        output.write("\n")

    if news_ids:
        connection.executemany(
            "INSERT OR IGNORE INTO seen_news (id, first_seen_at) VALUES (?, ?)",
            [(article_id, record["collected_at"]) for article_id in news_ids],
        )
        connection.commit()
    return record


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Continuously collect public NVDA stock prices and semiconductor/AI news."
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=300,
        help="Seconds between collection cycles (default: 300).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/nvda_monitor.jsonl"),
        help="Append-only JSON Lines output path.",
    )
    parser.add_argument(
        "--state-db",
        type=Path,
        default=Path("data/nvda_monitor_state.sqlite3"),
        help="SQLite file used to avoid emitting the same news article twice.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Collect one cycle and exit.",
    )
    args = parser.parse_args()
    if args.interval <= 0:
        parser.error("--interval must be a positive number of seconds")
    return args


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    args = parse_args()
    args.state_db.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(args.state_db) as connection:
        initialize_state(connection)
        while True:
            record = collect_cycle(connection, args.output)
            LOGGER.info(
                "Wrote cycle: %d prices, %d new articles, %d errors",
                len(record["prices"]),
                len(record["news"]),
                len(record["errors"]),
            )
            if args.once:
                return
            time.sleep(args.interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        LOGGER.info("Stopping NVDA monitor.")

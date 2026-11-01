import json
from unittest import TestCase
from unittest.mock import patch

from nvda_monitor import (
    combine_price_quotes,
    fetch_massive_price,
    fetch_price,
    parse_args,
    parse_feed,
)


class NvdaPriceHistoryTests(TestCase):
    def test_fetch_price_includes_latest_minute_observation_and_daily_sentiment(self):
        payload = {
            "chart": {
                "result": [
                    {
                        "meta": {
                            "currency": "USD",
                            "regularMarketPrice": 204.64,
                            "regularMarketTime": 1_793_390_400,
                            "chartPreviousClose": 200.0,
                            "exchangeTimezoneName": "America/New_York",
                        },
                        "timestamp": [1_793_386_800, 1_793_390_400],
                        "indicators": {
                            "quote": [
                                {
                                    "close": [202.0, 204.64],
                                }
                            ]
                        },
                    }
                ],
                "error": None,
            }
        }
        with patch("nvda_monitor.request_bytes", return_value=json.dumps(payload).encode()):
            result = fetch_price("nvda", "NVDA")

        self.assertEqual(result["previous_close"], 200.0)
        self.assertEqual(result["change"], 4.64)
        self.assertEqual(result["unit"], "share")
        self.assertEqual(result["price_change_sentiment"], "bullish")
        self.assertEqual([point["price"] for point in result["history"]], [204.64])
        self.assertEqual(result["price_timestamp"], result["history"][-1]["timestamp"])
        self.assertIn("interval=1m", result["source_url"])

    def test_worker_default_collection_interval_is_five_minutes(self):
        with patch("sys.argv", ["nvda_monitor.py", "--once"]):
            self.assertEqual(parse_args().interval, 300)

    def test_price_fallback_uses_latest_hour_when_market_quote_missing(self):
        payload = {
            "chart": {
                "result": [
                    {
                        "meta": {"currency": "USD", "chartPreviousClose": 200.0},
                        "timestamp": [1_793_386_800, 1_793_390_400],
                        "indicators": {
                            "quote": [{"close": [201.0, 202.0]}]
                        },
                    }
                ],
                "error": None,
            }
        }
        with patch("nvda_monitor.request_bytes", return_value=json.dumps(payload).encode()):
            result = fetch_price("nvda", "NVDA")

        self.assertEqual(result["price"], 202.0)
        self.assertEqual(result["price_timestamp"], result["history"][-1]["timestamp"])
        self.assertEqual(len(result["history"]), 1)

    def test_massive_fetch_selects_latest_one_minute_bar(self):
        bars = {
            "results": [
                {"c": 205.25, "t": 1_793_390_400_000}
            ]
        }
        requests = []

        def response_for_request(url, headers=None):
            requests.append((url, headers))
            return json.dumps(bars).encode()

        with patch("nvda_monitor.request_bytes", side_effect=response_for_request):
            result = fetch_massive_price("nvda", "NVDA", "test-key")

        self.assertEqual(result["symbol"], "NVDA")
        self.assertEqual(result["price"], 205.25)
        self.assertEqual(len(result["history"]), 1)
        self.assertIn("/range/1/minute/", requests[0][0])
        self.assertEqual(requests[0][1]["Authorization"], "Bearer test-key")
        self.assertNotIn("test-key", requests[0][0])

    def test_combines_prices_from_matching_minutes_using_the_average(self):
        yahoo = {
            "price": 204.5,
            "price_timestamp": "2026-10-30T19:59:45Z",
            "previous_close": 200.0,
            "source": "Yahoo Finance chart API",
            "source_url": "https://query1.finance.yahoo.com/chart?interval=1m",
        }
        massive = {
            "price": 205.5,
            "price_timestamp": "2026-10-30T19:59:00Z",
            "source": "Massive Stocks API",
            "source_url": "https://api.massive.com/v2/aggs/ticker/NVDA",
        }

        result = combine_price_quotes("nvda", "NVDA", yahoo, massive)

        self.assertEqual(result["price"], 205.0)
        self.assertEqual(result["price_timestamp"], "2026-10-30T19:59:00Z")
        self.assertEqual(result["change"], 5.0)
        self.assertEqual(result["source"], "Yahoo Finance chart API + Massive Stocks API")
        self.assertEqual(len(result["provider_observations"]), 2)

    def test_uses_newer_quote_when_provider_timestamps_do_not_match(self):
        yahoo = {
            "price": 204.5,
            "price_timestamp": "2026-10-30T19:58:00Z",
            "previous_close": 200.0,
            "source": "Yahoo Finance chart API",
            "source_url": "yahoo",
        }
        massive = {
            "price": 205.5,
            "price_timestamp": "2026-10-30T19:59:00Z",
            "source": "Massive Stocks API",
            "source_url": "massive",
        }

        result = combine_price_quotes("nvda", "NVDA", yahoo, massive)

        self.assertEqual(result["price"], 205.5)
        self.assertEqual(result["source"], "Massive Stocks API")
        self.assertEqual(result["source_urls"], ["massive"])
        self.assertEqual(len(result["provider_observations"]), 1)

    def test_fetch_price_falls_back_to_yahoo_and_records_massive_error(self):
        yahoo = {
            "name": "nvda",
            "symbol": "NVDA",
            "price": 204.5,
            "price_timestamp": "2026-10-30T19:59:00Z",
            "history": [{"timestamp": "2026-10-30T19:59:00Z", "price": 204.5}],
            "previous_close": 200.0,
            "change": 4.5,
            "change_percent": 2.25,
            "change_basis": "previous_daily_close",
            "price_change_sentiment": "bullish",
            "source": "Yahoo Finance chart API",
            "source_url": "yahoo",
        }
        with (
            patch.dict("os.environ", {"MASSIVE_API_KEY": "test-key"}),
            patch("nvda_monitor.fetch_yahoo_price", return_value=yahoo),
            patch("nvda_monitor.fetch_massive_price", side_effect=ValueError("provider unavailable")),
        ):
            result = fetch_price("nvda", "NVDA")

        self.assertEqual(result["price"], 204.5)
        self.assertEqual(result["source"], "Yahoo Finance chart API")
        self.assertIn("Massive: ValueError: provider unavailable", result["provider_errors"])


class NvdaNewsFeedTests(TestCase):
    def test_feed_filters_irrelevant_stories_and_resolves_links(self):
        feed = b"""<?xml version="1.0"?>
        <rss version="2.0"><channel>
          <item>
            <title>NVIDIA unveils next-gen Blackwell AI GPU cluster</title>
            <link>/tech/nvidia-blackwell</link>
            <guid>story-1</guid>
            <pubDate>Fri, 30 Oct 2026 12:00:00 GMT</pubDate>
            <description>Hyperscaler demand for NVDA accelerates.</description>
          </item>
          <item>
            <title>Beverage company reports quarterly sales</title>
            <link>https://example.com/beverage</link>
            <guid>story-2</guid>
            <description>Soft drink volumes rose 2%.</description>
          </item>
        </channel></rss>"""

        articles = parse_feed(
            "cnbc_technology",
            "https://feeds.example.com/tech/rss.xml",
            feed,
            ("nvidia", "nvda", "geforce"),
        )

        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0]["url"], "https://feeds.example.com/tech/nvidia-blackwell")
        self.assertEqual(articles[0]["id"], "cnbc_technology:story-1")


if __name__ == "__main__":
    import unittest

    unittest.main()

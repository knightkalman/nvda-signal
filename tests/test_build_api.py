from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from build_api import (
    ENDPOINT_URL,
    assess_price_validity,
    build_api_artifacts,
    build_api_payload,
    decrypt_envelope,
    encrypt_payload,
)
from nvda_signal_client import fetch_nvda_signal_api, normalize_envelope_url


def make_price_quote(
    name: str,
    symbol: str,
    price: float,
    previous_close: float,
    timestamp: str,
    sentiment: str,
) -> dict:
    change = round(price - previous_close, 6)
    change_percent = round(change / previous_close * 100, 4)
    return {
        "name": name,
        "symbol": symbol,
        "price": price,
        "currency": "USD",
        "unit": "share",
        "price_timestamp": timestamp,
        "history": [{"timestamp": timestamp, "price": price}],
        "previous_close": previous_close,
        "change": change,
        "change_percent": change_percent,
        "change_basis": "previous_daily_close",
        "price_change_sentiment": sentiment,
        "source": "Yahoo Finance chart API",
        "provider_observations": [
            {
                "source": "Yahoo Finance chart API",
                "price": price,
                "timestamp": timestamp,
            }
        ],
    }


def write_sample_archive(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    records_day_1 = [
        {
            "schema_version": 1,
            "collected_at": "2026-10-30T14:00:00Z",
            "prices": {
                "nvda": make_price_quote(
                    "nvda", "NVDA", 198.0, 200.0, "2026-10-30T13:59:00Z", "bearish"
                ),
            },
            "news": [
                {
                    "id": "cnbc_technology:story-1",
                    "source": "cnbc_technology",
                    "title": "Semiconductor index dips ahead of macro data",
                    "url": "https://example.com/story-1",
                    "published_at": "2026-10-30T13:30:00Z",
                    "summary": "Chipmakers trade slightly lower in early session.",
                    "analysis": {
                        "summary": "Short-term macro caution weighs on semiconductor stocks.",
                        "direction": "bearish",
                        "confidence": 65,
                        "validity": "supported",
                        "rationale": "Broad sector dip creates mild downward pressure on NVDA.",
                        "assessed_at": "2026-10-30T14:00:05Z",
                        "revision": 1,
                        "assessment_type": "new",
                        "market_context": {},
                    },
                }
            ],
            "errors": [],
            "news_analysis": {"status": "completed", "article_count": 1},
        },
        {
            "schema_version": 1,
            "collected_at": "2026-10-30T20:00:00Z",
            "prices": {
                "nvda": make_price_quote(
                    "nvda", "NVDA", 205.0, 200.0, "2026-10-30T19:59:00Z", "bullish"
                ),
            },
            "news": [],
            "errors": [],
            "news_analysis": {"status": "no_news"},
        },
        {
            "schema_version": 1,
            "collected_at": "2026-10-30T21:05:00Z",
            "prices": {
                "nvda": make_price_quote(
                    "nvda", "NVDA", 205.0, 200.0, "2026-10-30T19:59:00Z", "bullish"
                ),
            },
            "news": [
                {
                    "id": "yahoo_nvda:story-2",
                    "source": "yahoo_nvda",
                    "title": "Hyperscalers expand NVIDIA Blackwell GPU deployments",
                    "url": "https://example.com/story-2",
                    "published_at": "2026-10-30T21:00:00Z",
                    "summary": "Major cloud providers accelerate AI cluster rollouts.",
                    "analysis": {
                        "summary": "Cloud providers accelerate Blackwell GPU cluster rollouts.",
                        "direction": "bullish",
                        "confidence": 85,
                        "validity": "supported",
                        "rationale": "Stronger data center GPU orders directly support NVDA revenue.",
                        "assessed_at": "2026-10-30T21:05:10Z",
                        "revision": 1,
                        "assessment_type": "new",
                        "market_context": {
                            "nvda": {
                                "from": "2026-10-30T19:59:00Z",
                                "to": "2026-10-30T19:59:00Z",
                                "start_price": 205.0,
                                "latest_price": 205.0,
                                "change_percent": 0.0,
                            }
                        },
                    },
                },
                {
                    "id": "bbc_technology:story-3",
                    "source": "bbc_technology",
                    "title": "Unassessed AI semiconductor headline",
                    "url": "https://example.com/story-3",
                    "published_at": "2026-10-30T21:02:00Z",
                    "summary": "Awaiting AI assessment.",
                },
            ],
            "errors": [],
            "news_analysis": {"status": "skipped"},
        },
    ]
    (data_dir / "2026-10-30.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records_day_1) + "\n",
        encoding="utf-8",
    )


class BuildApiTests(unittest.TestCase):
    def test_compresses_price_directions_inflections_and_deduplicates_quotes(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory) / "nvda"
            write_sample_archive(data_dir)
            now = datetime(2026, 10, 31, 12, 0, 0, tzinfo=timezone.utc)

            payload = build_api_payload(data_dir, now=now)

            self.assertEqual(payload["endpoint"], ENDPOINT_URL)
            self.assertEqual(payload["total_collections_analyzed"], 3)
            self.assertTrue(payload["market_status"]["is_weekend_closed"])
            self.assertIsNotNone(payload["market_status"]["active_weekend_window"])

            nvda = payload["prices"]["nvda"]
            self.assertEqual(nvda["price"], 205.0)
            self.assertEqual(nvda["previous_close"], 200.0)
            self.assertEqual(nvda["unit"], "share")
            self.assertEqual(nvda["price_change_sentiment"], "bullish")
            self.assertEqual(nvda["validity"]["status"], "valid")

            nvda_comp = payload["price_direction_compression"]["nvda"]
            self.assertEqual(nvda_comp["current_regime"]["direction"], "bullish")
            self.assertEqual(len(nvda_comp["regime_segments"]), 2)
            self.assertEqual(nvda_comp["regime_segments"][0]["direction"], "bearish")
            self.assertEqual(nvda_comp["regime_segments"][1]["direction"], "bullish")
            self.assertEqual(len(nvda_comp["inflection_points"]), 1)
            inflection = nvda_comp["inflection_points"][0]
            self.assertEqual(inflection["from_direction"], "bearish")
            self.assertEqual(inflection["to_direction"], "bullish")
            self.assertEqual(inflection["price"], 205.0)

            self.assertEqual(len(nvda_comp["step_changes"]), 1)
            self.assertEqual(nvda_comp["step_changes"][0]["step_change"], 7.0)
            self.assertEqual(nvda_comp["step_changes"][0]["step_direction"], "up")

            dedup_nvda = payload["chart_data"]["deduplicated_series"]["nvda"]
            self.assertEqual(len(dedup_nvda), 2)
            self.assertEqual(dedup_nvda[0][1], 198.0)
            self.assertEqual(dedup_nvda[0][6], 1)
            self.assertEqual(dedup_nvda[1][1], 205.0)
            self.assertEqual(dedup_nvda[1][6], 2)

            news_summary = payload["news_sentiment"]["summary"]
            self.assertEqual(news_summary["total_stories"], 3)
            self.assertEqual(news_summary["assessed_stories"], 2)
            self.assertEqual(news_summary["by_direction"]["bullish"], 1)
            self.assertEqual(news_summary["by_direction"]["bearish"], 1)
            self.assertEqual(news_summary["by_direction"]["unassessed"], 1)
            self.assertEqual(news_summary["net_sentiment_score"], 0.0)
            self.assertGreater(news_summary["confidence_weighted_sentiment_score"], 0.0)

    def test_price_validity_detects_stale_and_insufficient_quotes(self):
        now = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)
        stale_quote = {
            "price": 204.0,
            "previous_close": 200.0,
            "price_timestamp": "2026-10-29T10:00:00Z",
        }
        stale_result = assess_price_validity(stale_quote, now)
        self.assertEqual(stale_result["status"], "stale")
        self.assertEqual(stale_result["direction"], "bullish")

        insufficient_quote = {
            "price": 204.0,
            "previous_close": None,
            "price_timestamp": "2026-11-01T11:55:00Z",
        }
        insufficient_result = assess_price_validity(insufficient_quote, now)
        self.assertEqual(insufficient_result["status"], "insufficient")
        self.assertEqual(insufficient_result["direction"], "unknown")

    def test_encrypt_and_decrypt_round_trip_and_wrong_key_rejection(self):
        sample_payload = {
            "schema_version": 1,
            "prices": {"nvda": {"price": 204.64}},
            "secret_marker": "sensitive-nvda-signal-payload",
        }
        envelope = encrypt_payload(
            sample_payload,
            "super-secret-agent-key",
            generated_at="2026-11-01T12:00:00Z",
            iterations=10_000,
        )
        serialized = json.dumps(envelope)
        self.assertNotIn("sensitive-nvda-signal-payload", serialized)
        self.assertNotIn("204.64", serialized)
        self.assertEqual(envelope["encryption"]["algorithm"], "AES-256-GCM")

        decrypted = decrypt_envelope(envelope, "super-secret-agent-key")
        self.assertEqual(decrypted, sample_payload)

        with self.assertRaisesRegex(ValueError, "authentication failed"):
            decrypt_envelope(envelope, "wrong-api-key")

    def test_openssl_libcrypto_fallback_round_trip(self):
        sample_payload = {"schema_version": 1, "prices": {"nvda": 204.64}}
        with patch("build_api._AESGCM", None):
            envelope = encrypt_payload(
                sample_payload,
                "fallback-test-key",
                iterations=10_000,
            )
            decrypted = decrypt_envelope(envelope, "fallback-test-key")
            self.assertEqual(decrypted, sample_payload)
            with self.assertRaisesRegex(ValueError, "authentication failed"):
                decrypt_envelope(envelope, "wrong-key")

    def test_build_api_artifacts_fails_closed_when_key_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_dir = root / "nvda"
            out_dir = root / "site" / "api"
            write_sample_archive(data_dir)

            stub = build_api_artifacts(data_dir, out_dir, None)
            self.assertEqual(stub["status"], "unauthorized_stub")
            saved_text = (out_dir / "index.json").read_text(encoding="utf-8")
            self.assertNotIn("205.0", saved_text)
            self.assertNotIn("Blackwell", saved_text)
            self.assertTrue((out_dir / "index.html").exists())

            with self.assertRaisesRegex(ValueError, "NVDA_SIGNAL_API_KEY is required"):
                build_api_artifacts(data_dir, out_dir, "", require_key=True)

    def test_client_fetches_and_decrypts_built_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_dir = root / "nvda"
            out_dir = root / "site" / "api"
            write_sample_archive(data_dir)

            build_api_artifacts(
                data_dir,
                out_dir,
                "agent-shared-key",
                now=datetime(2026, 10, 31, 12, 0, 0, tzinfo=timezone.utc),
                iterations=10_000,
            )
            envelope_bytes = (out_dir / "index.json").read_bytes()

            class FakeResponse:
                def __enter__(self):
                    return self

                def __exit__(self, *_):
                    return None

                def read(self):
                    return envelope_bytes

            captured_urls = []

            def fake_opener(request, timeout):
                captured_urls.append(request.full_url)
                return FakeResponse()

            result = fetch_nvda_signal_api(
                "agent-shared-key",
                "https://knightkalman.github.io/nvda-signal/api/",
                opener=fake_opener,
            )
            self.assertEqual(
                captured_urls[0],
                "https://knightkalman.github.io/nvda-signal/api/index.json",
            )
            self.assertEqual(
                normalize_envelope_url("https://knightkalman.github.io/nvda-signal/api"),
                "https://knightkalman.github.io/nvda-signal/api/index.json",
            )
            self.assertEqual(result["prices"]["nvda"]["price"], 205.0)
            self.assertEqual(result["news_sentiment"]["summary"]["total_stories"], 3)


if __name__ == "__main__":
    unittest.main()

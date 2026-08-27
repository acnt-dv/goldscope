import tempfile
import unittest
import re
from datetime import datetime
from pathlib import Path

import app
from app import (
    APP_VERSION, app_metadata, forecast, health_status,
    intraday_fallback_forecast, intraday_forecast,
    long_term_fallback_forecast, long_term_forecast, market_analysis,
)


class ForecastTests(unittest.TestCase):
    def test_version_is_semantic_and_exposed(self):
        self.assertRegex(
            APP_VERSION,
            re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
                       r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"),
        )
        self.assertEqual(app_metadata()["version"], APP_VERSION)

    def test_health_endpoint_initializes_database(self):
        original_path = app.DB_PATH
        original_seed_path = app.SEED_DB_PATH
        with tempfile.TemporaryDirectory() as temp_dir:
            app.DB_PATH = Path(temp_dir) / "data" / "test.db"
            app.SEED_DB_PATH = Path(temp_dir) / "missing-seed.db"
            try:
                status, payload = health_status()
            finally:
                app.DB_PATH = original_path
                app.SEED_DB_PATH = original_seed_path
        self.assertEqual(status, 200)
        self.assertEqual(payload["database"], "ok")
        self.assertEqual(payload["version"], APP_VERSION)
        self.assertEqual(payload["app"]["version"], APP_VERSION)
        self.assertEqual(payload["counts"]["intradayCandles"], 0)

    def test_forecast_scales_fineness(self):
        rows = []
        value = 10_000_000
        for i in range(260):
            value *= 1 + (0.002 if i % 3 else -0.001)
            rows.append({
                "open": value, "low": value * .99, "high": value * 1.01,
                "close": value, "date": f"2025-01-{i:03d}", "jalali": f"1404-{i:03d}",
            })
        standard = forecast(rows, 750, 240)
        custom = forecast(rows, 725, 240)
        self.assertAlmostEqual(custom["latest"]["price"] / standard["latest"]["price"], 725 / 750, places=5)
        self.assertGreaterEqual(standard["backtest"]["days"], 30)
        self.assertLessEqual(standard["backtest"]["days"], 90)

    def test_intraday_supports_only_18_and_24(self):
        series = []
        value = 17_000_000
        for i in range(240):
            value *= 1 + (0.0008 if i % 5 else -0.0005)
            series.append({"timestamp": 1_780_000_000_000 + i * 300_000, "price18": value, "mode": "direct"})
        meta = {"mode": "direct", "lastDirect": "2026-07-02T12:00:00+03:30", "lastOunce": "2026-07-02T12:00:00+03:30", "usdToman": 175000}
        gold18 = intraday_forecast(series, meta, 18, 60)
        gold24 = intraday_forecast(series, meta, 24, 60)
        self.assertAlmostEqual(gold24["latest"]["price"] / gold18["latest"]["price"], 24 / 18, places=5)
        self.assertNotEqual(gold18["model"]["id"], "neutral")
        self.assertIn("hasEdge", gold18["benchmark"])
        with self.assertRaises(ValueError):
            intraday_forecast(series, meta, 22, 60)

    def test_four_hour_forecast_accepts_current_session_size(self):
        series = []
        value = 17_000_000
        for i in range(108):
            value *= 1 + (0.0005 if i % 4 else -0.0002)
            series.append({
                "timestamp": 1_780_000_000_000 + i * 300_000,
                "price18": value, "mode": "direct",
            })
        meta = {
            "mode": "direct", "lastDirect": "2026-07-02T12:00:00+03:30",
            "lastOunce": "2026-07-02T12:00:00+03:30", "usdToman": 175000,
        }
        result = intraday_forecast(series, meta, 18, 240)
        self.assertEqual(result["horizonMinutes"], 240)
        self.assertGreaterEqual(result["backtest"]["samples"], 20)

    def test_short_current_session_uses_prior_sessions_without_crossing_gaps(self):
        series = []
        value = 17_000_000
        start = 1_780_000_000_000
        for session_index, candle_count in enumerate((120, 120, 39)):
            session_start = start + session_index * 24 * 60 * 60_000
            for candle_index in range(candle_count):
                value *= 1 + (0.0005 if candle_index % 4 else -0.0002)
                series.append({
                    "timestamp": session_start + candle_index * 300_000,
                    "price18": value,
                    "mode": "direct",
                })
            value *= 1.03
        meta = {
            "mode": "direct", "lastDirect": "2026-08-25T14:10:00+03:30",
            "lastOunce": "2026-08-25T14:10:00+03:30", "usdToman": 175000,
        }
        result = intraday_forecast(series, meta, 18, 240)
        self.assertEqual(result["horizonMinutes"], 240)
        self.assertEqual(result["dataQuality"]["currentSessionCandles"], 39)
        self.assertEqual(result["dataQuality"]["trainingSessions"], 2)
        self.assertGreaterEqual(result["dataQuality"]["trainingSamples"], 30)
        self.assertEqual(len(result["chart"]), 39)

    def test_intraday_fallback_always_returns_visible_prediction(self):
        series = [
            {
                "timestamp": 1_780_000_000_000 + index * 300_000,
                "price18": 17_000_000 * (1 + index * 0.0002),
                "mode": "direct",
            }
            for index in range(8)
        ]
        meta = {
            "mode": "direct", "dataSourceMode": "live",
            "lastDirect": "2026-08-25T10:00:00+03:30",
        }
        result = intraday_fallback_forecast(
            series, meta, 18, 60, "نمونه آموزشی کافی نیست"
        )
        self.assertTrue(result["fallback"])
        self.assertGreater(result["prediction"], 0)
        self.assertTrue(result["chart"])
        self.assertIsNone(result["backtest"]["maePercent"])

    def test_long_term_fallback_returns_persisted_scenario(self):
        rows = [
            {
                "date": f"2026/08/{index + 1:02d}",
                "gold18_toman": 20_000_000 + index * 20_000,
                "domestic_risk": 0.02,
                "news_tone": None,
            }
            for index in range(20)
        ]
        result = long_term_fallback_forecast(
            rows, 24, "weekly", "مدل اصلی آماده نیست"
        )
        self.assertTrue(result["fallback"])
        self.assertGreater(result["prediction"], 0)
        self.assertEqual(result["horizon"], "weekly")

    def test_long_term_horizons(self):
        rows = []
        gold, ounce, usd = 8_000_000.0, 2000.0, 100_000.0
        for i in range(500):
            ounce *= 1 + (0.001 if i % 4 else -0.0007)
            usd *= 1 + (0.0012 if i % 7 else -0.0004)
            gold = ounce * usd / 31.1034768 * .75 * (1 + .02 * ((i % 30) / 30))
            rows.append({"date": f"2025/{i//30+1:02d}/{i%30+1:02d}", "gold18_toman": gold, "ounce_usd": ounce, "usd_toman": usd, "domestic_risk": .02 * ((i % 30) / 30), "news_tone": None})
        result = long_term_forecast(rows, 24, "monthly")
        self.assertEqual(result["karat"], 24)
        self.assertEqual(result["horizon"], "monthly")
        self.assertGreater(result["observations"], 400)
        self.assertNotEqual(result["model"]["id"], "neutral")
        self.assertIn("neutralMaePercent", result["benchmark"])
        series = [{"timestamp": 1_780_000_000_000 + i * 300_000, "price18": rows[-1]["gold18_toman"] * (1 + i * .00001), "mode": "direct"} for i in range(100)]
        meta = {"mode": "direct", "lastDirect": "2026-07-02T12:00:00+03:30", "lastOunce": "2026-07-02T12:00:00+03:30", "usdToman": usd, "ounceUsd": ounce}
        short = intraday_forecast(series, meta, 18, 30)
        analysis = market_analysis(series, meta, 18, rows, short)
        self.assertEqual(len(analysis["resistances"]), 5)
        self.assertEqual(len(analysis["lowerResistances"]), 5)
        self.assertGreater(analysis["breakLevels"]["upside"], series[-1]["price18"])
        self.assertLess(analysis["breakLevels"]["downside"], series[-1]["price18"])
        self.assertIn(analysis["stance"]["id"], ("buy", "sell", "wait"))
        self.assertEqual(len(analysis["tradePlan"]["entries"]), 5)
        self.assertEqual(len(analysis["tradePlan"]["exits"]), 5)
        self.assertEqual(sum(level["allocationPercent"] for level in analysis["tradePlan"]["entries"]), 100)
        self.assertEqual(sum(level["allocationPercent"] for level in analysis["tradePlan"]["exits"]), 100)

    def test_latest_intraday_quote_is_merged_into_long_term_edge(self):
        rows = [{
            "date": "2026/08/22", "gold18_toman": 21_000_000,
            "ounce_usd": 3300, "usd_toman": 180_000,
            "domestic_risk": 0.1, "news_tone": None, "news_volume": None,
        }]
        timestamp = int(datetime.now().astimezone().timestamp() * 1000)
        series = [{"timestamp": timestamp, "price18": 21_750_000, "mode": "direct"}]
        meta = {
            "ounceUsd": 3350, "usdToman": 181_000, "dataSourceMode": "live",
            "fetchWarnings": {},
        }
        original_path = app.DB_PATH
        with tempfile.TemporaryDirectory() as temp_dir:
            app.DB_PATH = Path(temp_dir) / "test.db"
            try:
                merged, quality = app.merge_latest_daily_row(rows, series, meta)
            finally:
                app.DB_PATH = original_path
        self.assertEqual(merged[-1]["gold18_toman"], 21_750_000)
        self.assertEqual(quality["latestIncluded"], datetime.now().astimezone().strftime("%Y/%m/%d"))
        self.assertTrue(quality["mergedIntraday"])
        self.assertTrue(quality["validForForecast"])


if __name__ == "__main__":
    unittest.main()

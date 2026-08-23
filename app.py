#!/usr/bin/env python3
"""GoldScope — dependency-free gold market analysis application."""

from __future__ import annotations

import http.client
import json
import math
import os
import re
import shutil
import ssl
import sqlite3
import statistics
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse


ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("GOLD_DB_PATH", str(ROOT / "data" / "gold_model.db"))).expanduser()
SEED_DB_PATH = ROOT / "seed" / "gold_model.db"
TGJU_URL = (
    "https://api.tgju.org/v1/market/indicator/summary-table-data/"
    "geram18?lang=fa&order_dir=asc"
)
CACHE_SECONDS = 15 * 60
_cache: dict[str, object] = {"at": 0.0, "rows": None}
_intraday_cache: dict[str, object] = {"at": 0.0, "data": None}
_intraday_status: dict[str, object] = {
    "source": "none", "fetchedAt": None, "errors": {},
}
PROFILE_URLS = {
    "ounce": "https://www.tgju.org/profile/ons",
    "usd": "https://www.tgju.org/profile/price_dollar_rl",
    "gold18": "https://www.tgju.org/profile/geram18",
}
DAILY_URL = "https://api.tgju.org/v1/market/indicator/summary-table-data/{symbol}?lang=fa&order_dir=asc"
_daily_cache: dict[str, object] = {"at": 0.0, "rows": None}
SECONDARY_USD_URL = "https://raw.githubusercontent.com/rate-json/default/main/data.json"
_secondary_usd_cache: dict[str, object] = {"at": 0.0, "data": None}
_collection_lock = threading.Lock()
_collector_status: dict[str, object] = {
    "enabled": False,
    "intervalSeconds": None,
    "lastAttemptAt": None,
    "lastSuccessAt": None,
    "lastError": None,
    "lastQuoteAt": None,
    "sourceMode": "none",
    "intradayCandles": 0,
    "dailyRows": 0,
}

NETWORK_ERRORS = (
    urllib.error.URLError, TimeoutError, ssl.SSLError, ConnectionError,
    http.client.HTTPException, OSError,
)


def _read_url(request: urllib.request.Request, timeout: int, attempts: int = 3) -> bytes:
    """Read an HTTP response with bounded retries for transient TLS/network failures."""
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500:
                raise
            last_error = exc
        except NETWORK_ERRORS as exc:
            last_error = exc
        if attempt + 1 < attempts:
            time.sleep(0.4 * (attempt + 1))
    if last_error is not None:
        raise last_error
    raise RuntimeError("پاسخی از منبع داده دریافت نشد.")


def _read_json(request: urllib.request.Request, timeout: int, attempts: int = 3) -> object:
    return json.loads(_read_url(request, timeout, attempts).decode("utf-8"))


def _db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not DB_PATH.exists() and SEED_DB_PATH.exists():
        # A Liara disk is empty on first mount. Seed it with the bundled,
        # multi-year snapshot, then keep all subsequent writes on the disk.
        shutil.copy2(SEED_DB_PATH, DB_PATH)
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS intraday_candles (
            timestamp INTEGER PRIMARY KEY, gold18_toman REAL NOT NULL,
            mode TEXT NOT NULL, collected_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS daily_market (
            date TEXT PRIMARY KEY, gold18_toman REAL NOT NULL,
            ounce_usd REAL, usd_toman REAL, domestic_risk REAL,
            news_tone REAL, news_volume REAL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
            model_type TEXT NOT NULL, horizon TEXT NOT NULL, karat INTEGER NOT NULL,
            current_price REAL NOT NULL, predicted_price REAL NOT NULL,
            range_low REAL, range_high REAL, selected_model TEXT,
            mae_percent REAL, direction_accuracy REAL, source_mode TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_predictions_created ON predictions(created_at);
        """
    )
    return connection


def persist_intraday(series: list[dict[str, object]]) -> None:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    with _db() as connection:
        connection.executemany(
            "INSERT OR REPLACE INTO intraday_candles(timestamp,gold18_toman,mode,collected_at) VALUES(?,?,?,?)",
            [(int(row["timestamp"]), float(row["price18"]), str(row["mode"]), now) for row in series],
        )


def persist_prediction(result: dict[str, object], model_type: str) -> None:
    backtest = result.get("backtest", {})
    model = result.get("model", {})
    latest = result.get("latest", {})
    with _db() as connection:
        connection.execute(
            """INSERT INTO predictions(created_at,model_type,horizon,karat,current_price,predicted_price,
               range_low,range_high,selected_model,mae_percent,direction_accuracy,source_mode)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                datetime.now().astimezone().isoformat(timespec="seconds"), model_type,
                str(result.get("horizonMinutes", result.get("horizon", ""))), int(result["karat"]),
                float(latest["price"]), float(result["prediction"]), float(result["rangeLow"]),
                float(result["rangeHigh"]), str(model.get("name", "")), backtest.get("maePercent"),
                backtest.get("directionAccuracy"), str(result.get("mode", "daily")),
            ),
        )


def _number(value: str) -> float:
    return float(value.replace(",", "").strip())


def fetch_history() -> list[dict[str, object]]:
    """Fetch 18k/750 daily OHLC from TGJU and normalize it to toman."""
    now = time.time()
    if _cache["rows"] and now - float(_cache["at"]) < CACHE_SECONDS:
        return _cache["rows"]  # type: ignore[return-value]

    request = urllib.request.Request(
        TGJU_URL,
        headers={
            "User-Agent": "Mozilla/5.0 IranGoldForecast/1.0",
            "Accept": "application/json",
        },
    )
    try:
        payload = _read_json(request, timeout=20)
    except (*NETWORK_ERRORS, json.JSONDecodeError) as exc:
        if _cache["rows"]:
            return _cache["rows"]  # type: ignore[return-value]
        raise RuntimeError(f"دریافت داده از منبع با خطا روبه‌رو شد: {exc}") from exc

    rows = []
    for item in payload.get("data", []):
        if len(item) < 8:
            continue
        try:
            # Source order: open, low, high, close, change, %, Gregorian, Jalali.
            rows.append(
                {
                    "open": _number(item[0]) / 10,
                    "low": _number(item[1]) / 10,
                    "high": _number(item[2]) / 10,
                    "close": _number(item[3]) / 10,
                    "date": item[6],
                    "jalali": item[7],
                }
            )
        except (TypeError, ValueError):
            continue
    rows.sort(key=lambda row: str(row["date"]))
    if len(rows) < 100:
        raise RuntimeError("تعداد داده‌های معتبر برای پیش‌بینی کافی نیست.")
    _cache.update({"at": now, "rows": rows})
    return rows


def _fetch_daily_symbol(symbol: str, toman: bool = False) -> list[dict[str, object]]:
    request = urllib.request.Request(
        DAILY_URL.format(symbol=symbol),
        headers={"User-Agent": "Mozilla/5.0 IranGoldForecast/3.0", "Accept": "application/json"},
    )
    payload = _read_json(request, timeout=12, attempts=2)
    divisor = 10 if toman else 1
    result = []
    for item in payload.get("data", []):
        if len(item) < 8:
            continue
        try:
            result.append({"date": item[6], "close": _number(item[3]) / divisor})
        except (TypeError, ValueError):
            continue
    return result


def _fetch_news_tone() -> dict[str, float]:
    """Best-effort daily political/social news tone from GDELT (coverage can be sparse)."""
    params = urlencode(
        {
            "query": "Iran (sanctions OR war OR protest OR election OR conflict OR unrest)",
            "mode": "TimelineTone", "format": "json", "timespan": "3months", "timelinesmooth": "1",
        }
    )
    request = urllib.request.Request(
        f"https://api.gdeltproject.org/api/v2/doc/doc?{params}",
        headers={"User-Agent": "IranGoldForecast/3.0", "Accept": "application/json"},
    )
    try:
        payload = _read_json(request, timeout=5, attempts=1)
    except Exception:
        return {}
    result: dict[str, float] = {}
    for series in payload.get("timeline", []):
        for point in series.get("data", []):
            raw_date = str(point.get("date", ""))[:8]
            if len(raw_date) == 8:
                date = f"{raw_date[:4]}/{raw_date[4:6]}/{raw_date[6:8]}"
                try:
                    result[date] = float(point["value"])
                except (KeyError, TypeError, ValueError):
                    pass
    return result


def fetch_daily_markets() -> list[dict[str, object]]:
    """Fetch and persist aligned multi-year daily gold, USD and ounce history."""
    now = time.time()
    if _daily_cache["rows"] and now - float(_daily_cache["at"]) < 15 * 60:
        return _daily_cache["rows"]  # type: ignore[return-value]
    with _db() as connection:
        cached = [dict(row) for row in connection.execute("SELECT * FROM daily_market ORDER BY date")]
        latest_update = connection.execute("SELECT MAX(updated_at) FROM daily_market").fetchone()[0]
    if cached and latest_update:
        try:
            cache_age = datetime.now().astimezone() - datetime.fromisoformat(str(latest_update))
            if cache_age.total_seconds() < 6 * 3600:
                _daily_cache.update({"at": now, "rows": cached})
                return cached
        except ValueError:
            pass
    try:
        with ThreadPoolExecutor(max_workers=3) as executor:
            gold_job = executor.submit(_fetch_daily_symbol, "geram18", True)
            ounce_job = executor.submit(_fetch_daily_symbol, "ons")
            usd_job = executor.submit(_fetch_daily_symbol, "price_dollar_rl", True)
            gold, ounce, usd = gold_job.result(), ounce_job.result(), usd_job.result()
    except (*NETWORK_ERRORS, json.JSONDecodeError) as exc:
        if cached:
            _daily_cache.update({"at": now, "rows": cached})
            return cached
        raise RuntimeError(f"دریافت تاریخچه چندساله ناموفق بود: {exc}") from exc

    ounce_map = {str(row["date"]): float(row["close"]) for row in ounce}
    usd_map = {str(row["date"]): float(row["close"]) for row in usd}
    news_tone = _fetch_news_tone()
    rows: list[dict[str, object]] = []
    last_ounce = last_usd = None
    for row in gold:
        date = str(row["date"])
        last_ounce = ounce_map.get(date, last_ounce)
        last_usd = usd_map.get(date, last_usd)
        if last_ounce is None or last_usd is None:
            continue
        theoretical18 = last_ounce * last_usd / 31.1034768 * 0.75
        risk = math.log(float(row["close"]) / theoretical18) if theoretical18 > 0 else 0.0
        rows.append({"date": date, "gold18_toman": float(row["close"]), "ounce_usd": last_ounce, "usd_toman": last_usd, "domestic_risk": risk, "news_tone": news_tone.get(date), "news_volume": None})
    updated = datetime.now().astimezone().isoformat(timespec="seconds")
    with _db() as connection:
        connection.executemany(
            """INSERT INTO daily_market(date,gold18_toman,ounce_usd,usd_toman,domestic_risk,news_tone,news_volume,updated_at)
               VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(date) DO UPDATE SET gold18_toman=excluded.gold18_toman,
               ounce_usd=excluded.ounce_usd,usd_toman=excluded.usd_toman,domestic_risk=excluded.domestic_risk,
               news_tone=COALESCE(excluded.news_tone,daily_market.news_tone),updated_at=excluded.updated_at""",
            [(r["date"], r["gold18_toman"], r["ounce_usd"], r["usd_toman"], r["domestic_risk"], r["news_tone"], r["news_volume"], updated) for r in rows],
        )
    _daily_cache.update({"at": now, "rows": rows})
    return rows


def _fetch_profile_ticks(url: str) -> list[list[float]]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 IranGoldForecast/3.1",
            "Accept": "text/html", "Connection": "close",
        },
    )
    html = _read_url(request, timeout=20).decode("utf-8", errors="replace")
    match = re.search(r"chartData:\s*(\[\[.*?\]\])", html, re.DOTALL)
    if not match:
        raise RuntimeError("داده درون‌روزی در صفحه منبع پیدا نشد.")
    data = json.loads(match.group(1))
    return [[float(point[0]), float(point[1])] for point in data if len(point) >= 2]


def _fetch_secondary_usd() -> dict[str, object] | None:
    """Best-effort independent free-market USD validator; never replaces TGJU silently."""
    now = time.time()
    if _secondary_usd_cache["data"] and now - float(_secondary_usd_cache["at"]) < 5 * 60:
        return _secondary_usd_cache["data"]  # type: ignore[return-value]
    request = urllib.request.Request(
        SECONDARY_USD_URL,
        headers={"User-Agent": "IranGoldForecast/3.1", "Accept": "application/json"},
    )
    try:
        payload = _read_json(request, timeout=12, attempts=2)
        value = float(payload["values"]["USD"])
        result = {
            "usdToman": value,
            "asOf": str(payload.get("generated_by_tomanify_at") or ""),
            "source": "Tomanify/rate-json",
        }
    except Exception:
        return None
    _secondary_usd_cache.update({"at": now, "data": result})
    return result


def _intraday_ticks_from_db() -> dict[str, list[list[float]]] | None:
    """Rebuild a clearly-labelled emergency input from the last persisted candles."""
    with _db() as connection:
        candle_rows = list(
            connection.execute(
                "SELECT timestamp,gold18_toman FROM intraday_candles ORDER BY timestamp DESC LIMIT 600"
            )
        )
        daily = connection.execute(
            "SELECT ounce_usd,usd_toman FROM daily_market ORDER BY date DESC LIMIT 1"
        ).fetchone()
    if len(candle_rows) < 80 or daily is None or not daily["ounce_usd"] or not daily["usd_toman"]:
        return None
    candle_rows.reverse()
    gold = [[float(row["timestamp"]), float(row["gold18_toman"]) * 10] for row in candle_rows]
    ounce = [[point[0], float(daily["ounce_usd"])] for point in gold]
    usd = [[point[0], float(daily["usd_toman"]) * 10] for point in gold]
    return {"gold18": gold, "ounce": ounce, "usd": usd}


def fetch_intraday_ticks() -> dict[str, list[list[float]]]:
    """Get current-session ticks. Cached to avoid hammering TGJU profile pages."""
    now = time.time()
    if _intraday_cache["data"] and now - float(_intraday_cache["at"]) < 45:
        return _intraday_cache["data"]  # type: ignore[return-value]
    cached = _intraday_cache.get("data") or {}
    data: dict[str, list[list[float]]] = {}
    errors: dict[str, str] = {}
    for name, url in PROFILE_URLS.items():
        try:
            data[name] = _fetch_profile_ticks(url)
        except (*NETWORK_ERRORS, json.JSONDecodeError, RuntimeError) as exc:
            errors[name] = str(exc)
            if name in cached:
                data[name] = cached[name]  # type: ignore[index]
    fallback = _intraday_ticks_from_db() if len(data) < len(PROFILE_URLS) else None
    if fallback:
        for name in PROFILE_URLS:
            if name not in data:
                data[name] = fallback[name]
    if len(data) < len(PROFILE_URLS):
        detail = next(iter(errors.values()), "منبع داده در دسترس نیست")
        raise RuntimeError(f"دریافت داده لحظه‌ای پس از ۳ تلاش ناموفق بود: {detail}")
    if len(data["ounce"]) < 20 or len(data["gold18"]) < 20:
        raise RuntimeError("تعداد تیک‌های لحظه‌ای برای تحلیل کافی نیست.")
    _intraday_cache.update({"at": now, "data": data})
    source = (
        "live" if not errors
        else "database-cache" if len(errors) == len(PROFILE_URLS) and fallback
        else "mixed-cache"
    )
    _intraday_status.update({
        "source": source,
        "fetchedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "errors": errors,
        "secondaryUsd": _fetch_secondary_usd(),
    })
    return data


def _bucket_closes(ticks: list[list[float]], minutes: int = 5) -> dict[int, float]:
    size = minutes * 60_000
    result: dict[int, float] = {}
    for timestamp, price in ticks:
        result[int(timestamp) // size * size] = float(price)
    return result


def build_iran_intraday(ticks: dict[str, list[list[float]]]) -> tuple[list[dict[str, object]], dict[str, object]]:
    """Join direct Iran quotes with an after-hours ounce-adjusted theoretical series."""
    direct = _bucket_closes(ticks["gold18"])
    ounce = _bucket_closes(ticks["ounce"])
    if not direct or not ounce:
        raise RuntimeError("ساخت سری زمانی درون‌روزی ممکن نشد.")
    anchor_time = max(direct)
    anchor_price = direct[anchor_time] / 10  # TGJU domestic quotes are rial.
    ounce_before = [timestamp for timestamp in ounce if timestamp <= anchor_time]
    if ounce_before:
        ounce_anchor_time = max(ounce_before)
        ounce_anchor = ounce[ounce_anchor_time]
    else:
        ounce_anchor = ounce[min(ounce)]

    combined = [{"timestamp": ts, "price18": price / 10, "mode": "direct"} for ts, price in direct.items()]
    for timestamp, ounce_price in ounce.items():
        if timestamp > anchor_time:
            combined.append(
                {
                    "timestamp": timestamp,
                    "price18": anchor_price * ounce_price / ounce_anchor,
                    "mode": "theoretical",
                }
            )
    combined.sort(key=lambda row: int(row["timestamp"]))
    # Remove duplicate buckets if source sessions overlap.
    unique = {int(row["timestamp"]): row for row in combined}
    combined = [unique[key] for key in sorted(unique)]

    last_direct_ms = max(point[0] for point in ticks["gold18"])
    last_ounce_ms = max(point[0] for point in ticks["ounce"])
    is_direct = last_ounce_ms - last_direct_ms <= 20 * 60_000
    latest_usd = ticks["usd"][-1][1] / 10 if ticks.get("usd") else None
    secondary_usd = _intraday_status.get("secondaryUsd")
    source_consensus: dict[str, object] = {"available": False, "valid": True}
    if latest_usd and secondary_usd:
        secondary_value = float(secondary_usd["usdToman"])
        disagreement = abs(float(latest_usd) / secondary_value - 1) * 100
        source_consensus = {
            "available": True, "primary": "TGJU",
            "secondary": secondary_usd["source"],
            "secondaryAsOf": secondary_usd["asOf"],
            "primaryUsdToman": round(latest_usd),
            "secondaryUsdToman": round(secondary_value),
            "usdDisagreementPercent": round(disagreement, 2),
            "warning": disagreement > 1,
            "valid": disagreement <= 3,
        }
    source_mode = str(_intraday_status.get("source") or "live")
    quote_mode = "cached" if source_mode == "database-cache" else ("direct" if is_direct else "theoretical")
    meta = {
        "mode": quote_mode,
        "lastDirect": datetime.fromtimestamp(last_direct_ms / 1000).astimezone().isoformat(timespec="seconds"),
        "lastOunce": datetime.fromtimestamp(last_ounce_ms / 1000).astimezone().isoformat(timespec="seconds"),
        "usdToman": round(latest_usd) if latest_usd else None,
        "ounceUsd": ticks["ounce"][-1][1] if ticks.get("ounce") else None,
        "dataSourceMode": source_mode,
        "fetchWarnings": dict(_intraday_status.get("errors") or {}),
        "sourceConsensus": source_consensus,
    }
    return combined, meta


def merge_latest_daily_row(
    rows: list[dict[str, object]], series: list[dict[str, object]], meta: dict[str, object]
) -> tuple[list[dict[str, object]], dict[str, object]]:
    """Merge the newest valid intraday quote into the daily training edge."""
    merged_rows = [dict(row) for row in rows]
    if not merged_rows or not series:
        return merged_rows, {"validForForecast": False, "reason": "داده کافی نیست"}
    latest_point = series[-1]
    quote_time = datetime.fromtimestamp(float(latest_point["timestamp"]) / 1000).astimezone()
    quote_date = quote_time.strftime("%Y/%m/%d")
    historical_date = str(merged_rows[-1]["date"])
    age_hours = max(0.0, (datetime.now().astimezone() - quote_time).total_seconds() / 3600)
    source_mode = str(meta.get("dataSourceMode") or "live")
    is_fresh = source_mode == "live" and age_hours <= 30

    if quote_date >= historical_date:
        previous = merged_rows[-1]
        ounce = float(meta.get("ounceUsd") or previous.get("ounce_usd") or 0)
        usd = float(meta.get("usdToman") or previous.get("usd_toman") or 0)
        gold18 = float(latest_point["price18"])
        theoretical18 = ounce * usd / 31.1034768 * 0.75 if ounce and usd else gold18
        current_row = {
            "date": quote_date, "gold18_toman": gold18, "ounce_usd": ounce,
            "usd_toman": usd,
            "domestic_risk": math.log(gold18 / theoretical18) if theoretical18 else 0.0,
            "news_tone": previous.get("news_tone") if quote_date == historical_date else None,
            "news_volume": previous.get("news_volume") if quote_date == historical_date else None,
        }
        if quote_date == historical_date:
            merged_rows[-1] = current_row
        else:
            merged_rows.append(current_row)
        updated = datetime.now().astimezone().isoformat(timespec="seconds")
        with _db() as connection:
            connection.execute(
                """INSERT INTO daily_market(date,gold18_toman,ounce_usd,usd_toman,domestic_risk,news_tone,news_volume,updated_at)
                   VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(date) DO UPDATE SET
                   gold18_toman=excluded.gold18_toman,ounce_usd=excluded.ounce_usd,
                   usd_toman=excluded.usd_toman,domestic_risk=excluded.domestic_risk,
                   news_tone=COALESCE(excluded.news_tone,daily_market.news_tone),updated_at=excluded.updated_at""",
                (quote_date, gold18, ounce, usd, current_row["domestic_risk"],
                 current_row["news_tone"], current_row["news_volume"], updated),
            )
    return merged_rows, {
        "sourceMode": source_mode,
        "historicalLatest": historical_date,
        "latestIncluded": merged_rows[-1]["date"],
        "latestQuoteAt": quote_time.isoformat(timespec="seconds"),
        "ageHours": round(age_hours, 2),
        "isFresh": is_fresh,
        "validForForecast": age_hours <= 72,
        "mergedIntraday": quote_date >= historical_date,
        "warnings": meta.get("fetchWarnings") or {},
        "sourceConsensus": meta.get("sourceConsensus") or {},
    }


def _features(returns: list[float], target_index: int) -> list[float]:
    """Features known immediately before returns[target_index]."""
    past = returns[:target_index]
    mean = lambda n: statistics.fmean(past[-n:])
    std = lambda n: statistics.pstdev(past[-n:]) if len(past[-n:]) > 1 else 0.0
    return [1.0, past[-1], mean(3), mean(7), mean(21), std(7), std(21)]


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float]:
    """Solve a small linear system with pivoted Gauss-Jordan elimination."""
    n = len(vector)
    augmented = [matrix[i][:] + [vector[i]] for i in range(n)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda row: abs(augmented[row][col]))
        augmented[col], augmented[pivot] = augmented[pivot], augmented[col]
        divisor = augmented[col][col]
        if abs(divisor) < 1e-15:
            continue
        augmented[col] = [value / divisor for value in augmented[col]]
        for row in range(n):
            if row == col:
                continue
            factor = augmented[row][col]
            augmented[row] = [
                augmented[row][j] - factor * augmented[col][j] for j in range(n + 1)
            ]
    return [augmented[i][-1] for i in range(n)]


def _recency_weights(count: int, half_life: int, floor: float = 0.08) -> list[float]:
    return [max(floor, 0.5 ** ((count - 1 - index) / half_life)) for index in range(count)]


def _ridge_fit(
    x: list[list[float]], y: list[float], alpha: float = 8.0,
    weights: list[float] | None = None,
) -> list[float]:
    columns = len(x[0])
    scales = [1.0]
    for col in range(1, columns):
        scale = statistics.pstdev(row[col] for row in x)
        scales.append(scale if scale > 1e-9 else 1.0)
    normalized = [[row[col] / scales[col] for col in range(columns)] for row in x]
    xtx = [[0.0] * columns for _ in range(columns)]
    xty = [0.0] * columns
    sample_weights = weights or [1.0] * len(normalized)
    for row, target, sample_weight in zip(normalized, y, sample_weights):
        for i in range(columns):
            xty[i] += sample_weight * row[i] * target
            for j in range(columns):
                xtx[i][j] += sample_weight * row[i] * row[j]
    for i in range(1, columns):  # Do not penalize the intercept.
        xtx[i][i] += alpha
    beta = _solve(xtx, xty)
    return [beta[col] / scales[col] for col in range(columns)]


def _predict(beta: list[float], features: list[float]) -> float:
    return sum(weight * value for weight, value in zip(beta, features))


def forecast(rows: list[dict[str, object]], fineness: int, window: int) -> dict[str, object]:
    ratio = fineness / 750
    selected = rows[-window:]
    prices = [float(row["close"]) * ratio for row in selected]
    returns = [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices))]
    if len(returns) < 90:
        raise ValueError("حداقل ۹۰ روز داده لازم است.")

    start = 21
    x = [_features(returns, i) for i in range(start, len(returns))]
    y = returns[start:]
    beta = _ridge_fit(x, y)
    next_features = _features(returns + [0.0], len(returns))
    predicted_return = _predict(beta, next_features)

    fitted = [_predict(beta, row) for row in x]
    residuals = [actual - estimate for actual, estimate in zip(y, fitted)]
    sigma = statistics.pstdev(residuals[-180:])
    current = prices[-1]
    prediction = current * math.exp(predicted_return)
    low = current * math.exp(predicted_return - 1.28 * sigma)
    high = current * math.exp(predicted_return + 1.28 * sigma)

    test_size = min(90, max(30, len(returns) // 6))
    errors: list[float] = []
    directions: list[bool] = []
    test_start = len(returns) - test_size
    # Refit at the start of each 10-day block. Every prediction remains
    # out-of-sample, while refreshes stay fast enough for an interactive app.
    for block_start in range(test_start, len(returns), 10):
        train_x = [_features(returns, j) for j in range(start, block_start)]
        train_y = returns[start:block_start]
        test_beta = _ridge_fit(train_x, train_y)
        for i in range(block_start, min(block_start + 10, len(returns))):
            estimate = _predict(test_beta, _features(returns, i))
            errors.append(abs(math.exp(estimate - returns[i]) - 1) * 100)
            directions.append((estimate >= 0) == (returns[i] >= 0))

    chart_rows = selected[-120:]
    chart = [
        {
            "date": row["date"],
            "jalali": row["jalali"],
            "price": round(float(row["close"]) * ratio),
        }
        for row in chart_rows
    ]
    return {
        "source": "TGJU",
        "unit": "تومان / گرم",
        "fineness": fineness,
        "karat": round(fineness * 24 / 1000, 2),
        "latest": {
            "date": selected[-1]["date"],
            "jalali": selected[-1]["jalali"],
            "price": round(current),
        },
        "prediction": round(prediction),
        "change": round(prediction - current),
        "changePercent": round((prediction / current - 1) * 100, 2),
        "rangeLow": round(low),
        "rangeHigh": round(high),
        "backtest": {
            "days": test_size,
            "maePercent": round(statistics.fmean(errors), 2),
            "directionAccuracy": round(100 * sum(directions) / len(directions), 1),
        },
        "observations": len(selected),
        "chart": chart,
        "generatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def intraday_forecast(
    series: list[dict[str, object]], meta: dict[str, object], karat: int, horizon_minutes: int
) -> dict[str, object]:
    if karat not in (18, 24):
        raise ValueError("فقط عیار ۱۸ و ۲۴ پشتیبانی می‌شود.")
    if horizon_minutes not in (15, 30, 60, 240):
        raise ValueError("بازه پیش‌بینی معتبر نیست.")
    multiplier = karat / 18
    prices = [float(row["price18"]) * multiplier for row in series]
    horizon = horizon_minutes // 5
    returns = [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices))]
    if len(prices) < max(80, horizon + 45):
        raise RuntimeError("داده ۵ دقیقه‌ای کافی برای این بازه وجود ندارد.")

    x: list[list[float]] = []
    y: list[float] = []
    for index in range(21, len(prices) - horizon):
        x.append(_features(returns, index))
        y.append(math.log(prices[index + horizon] / prices[index]))
    minimum_training_samples = 30 if horizon_minutes == 240 else 40
    if len(x) < minimum_training_samples:
        raise RuntimeError("نمونه‌های آموزشی درون‌روزی کافی نیست.")

    test_size = min(60, max(20, len(x) // 5))
    split = len(x) - test_size
    test_beta = _ridge_fit(
        x[:split], y[:split], weights=_recency_weights(split, half_life=96, floor=0.12)
    )
    ridge_estimates = [_predict(test_beta, row) for row in x[split:]]
    candidates = {
        "ridge": ridge_estimates,
        "contrarian": [-estimate for estimate in ridge_estimates],
        "momentum": [row[3] * horizon for row in x[split:]],
    }
    actual_holdout = y[split:]
    candidate_mae = {
        name: statistics.fmean(
            abs(math.exp(estimate - actual) - 1) * 100
            for estimate, actual in zip(estimates, actual_holdout)
        )
        for name, estimates in candidates.items()
    }
    neutral_mae = statistics.fmean(abs(math.exp(-actual) - 1) * 100 for actual in actual_holdout)
    selected_model = min(candidate_mae, key=candidate_mae.get)  # type: ignore[arg-type]
    estimates = candidates[selected_model]
    errors = [abs(math.exp(estimate - actual) - 1) * 100 for estimate, actual in zip(estimates, actual_holdout)]
    directions = [(estimate >= 0) == (actual >= 0) for estimate, actual in zip(estimates, actual_holdout)]

    beta = _ridge_fit(x, y, weights=_recency_weights(len(x), half_life=96, floor=0.12))
    current_features = _features(returns + [0.0], len(returns))
    ridge_prediction = _predict(beta, current_features)
    predicted_return = {
        "ridge": ridge_prediction,
        "contrarian": -ridge_prediction,
        "momentum": current_features[3] * horizon,
    }[selected_model]
    residuals = [actual - estimate for actual, estimate in zip(actual_holdout, estimates)]
    sigma = statistics.pstdev(residuals)
    current = prices[-1]
    prediction = current * math.exp(predicted_return)
    low = current * math.exp(predicted_return - 1.28 * sigma)
    high = current * math.exp(predicted_return + 1.28 * sigma)
    chart_rows = series[-144:]
    chart = [
        {
            "timestamp": row["timestamp"],
            "price": round(float(row["price18"]) * multiplier),
            "mode": row["mode"],
        }
        for row in chart_rows
    ]
    quote_time = datetime.fromtimestamp(float(series[-1]["timestamp"]) / 1000).astimezone()
    age_minutes = max(0.0, (datetime.now().astimezone() - quote_time).total_seconds() / 60)
    return {
        "source": "TGJU",
        "unit": "تومان / گرم",
        "karat": karat,
        "horizonMinutes": horizon_minutes,
        "mode": meta["mode"],
        "modeDescription": {
            "direct": "نرخ مستقیم بازار ایران",
            "theoretical": "برآورد نظری پس از تعطیلی بازار ایران بر پایه حرکت اونس جهانی",
            "cached": "آخرین داده معتبر ذخیره‌شده؛ اتصال زنده برقرار نیست",
        }.get(str(meta["mode"]), "داده بازار"),
        "latest": {"timestamp": quote_time.isoformat(timespec="seconds"), "price": round(current)},
        "prediction": round(prediction),
        "change": round(prediction - current),
        "changePercent": round((prediction / current - 1) * 100, 3),
        "rangeLow": round(low),
        "rangeHigh": round(high),
        "backtest": {
            "samples": test_size,
            "maePercent": round(statistics.fmean(errors), 3),
            "directionAccuracy": round(100 * sum(directions) / len(directions), 1),
        },
        "benchmark": {
            "neutralMaePercent": round(neutral_mae, 3),
            "activeMaePercent": round(candidate_mae[selected_model], 3),
            "hasEdge": candidate_mae[selected_model] < neutral_mae,
            "improvementPercent": round((neutral_mae - candidate_mae[selected_model]) / neutral_mae * 100, 1) if neutral_mae else 0,
        },
        "model": {
            "id": selected_model,
            "name": {
                "ridge": "رگرسیون روند",
                "contrarian": "بازگشت به میانگین",
                "momentum": "مومنتوم کوتاه‌مدت",
            }[selected_model],
        },
        "usdToman": meta.get("usdToman"),
        "lastDirect": meta["lastDirect"],
        "chart": chart,
        "observations": len(prices),
        "generatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "dataQuality": {
            "sourceMode": meta.get("dataSourceMode", "live"),
            "ageMinutes": round(age_minutes, 1),
            "isFresh": meta.get("dataSourceMode", "live") == "live" and age_minutes <= 45,
            "warnings": meta.get("fetchWarnings") or {},
            "sourceConsensus": meta.get("sourceConsensus") or {},
        },
    }


def long_term_forecast(rows: list[dict[str, object]], karat: int, horizon_name: str) -> dict[str, object]:
    if karat not in (18, 24):
        raise ValueError("فقط عیار ۱۸ و ۲۴ پشتیبانی می‌شود.")
    horizons = {"daily": 1, "weekly": 5, "monthly": 21}
    labels = {"daily": "روزانه", "weekly": "هفتگی", "monthly": "ماهانه"}
    if horizon_name not in horizons:
        raise ValueError("افق بلندمدت معتبر نیست.")
    horizon = horizons[horizon_name]
    selected = rows[-1800:]  # Roughly seven market years.
    multiplier = karat / 18
    prices = [float(row["gold18_toman"]) * multiplier for row in selected]
    ounce = [float(row["ounce_usd"]) for row in selected]
    usd = [float(row["usd_toman"]) for row in selected]
    risk = [float(row["domestic_risk"] or 0) for row in selected]
    news_tone = [float(row.get("news_tone") or 0) for row in selected]
    returns = [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices))]
    ounce_returns = [math.log(ounce[i] / ounce[i - 1]) for i in range(1, len(ounce))]
    usd_returns = [math.log(usd[i] / usd[i - 1]) for i in range(1, len(usd))]

    def features(index: int) -> list[float]:
        base = _features(returns, index)
        return base + [ounce_returns[index - 1], usd_returns[index - 1], risk[index], risk[index] - risk[index - 5], news_tone[index], news_tone[index] - news_tone[index - 5]]

    x: list[list[float]] = []
    y: list[float] = []
    for index in range(21, len(prices) - horizon):
        x.append(features(index))
        y.append(math.log(prices[index + horizon] / prices[index]))
    if len(x) < 250:
        raise RuntimeError("تاریخچه روزانه کافی برای مدل بلندمدت وجود ندارد.")
    test_size = min(180, max(60, len(x) // 8))
    split = len(x) - test_size
    train_weights = _recency_weights(split, half_life=252, floor=0.08)
    test_beta = _ridge_fit(x[:split], y[:split], alpha=12.0, weights=train_weights)
    ridge_test = [_predict(test_beta, row) for row in x[split:]]
    candidates = {
        "ridge": ridge_test,
        "contrarian": [-value for value in ridge_test],
        "momentum": [row[3] * horizon for row in x[split:]],
    }
    actual = y[split:]
    maes = {name: statistics.fmean(abs(math.exp(p - a) - 1) * 100 for p, a in zip(values, actual)) for name, values in candidates.items()}
    neutral_mae = statistics.fmean(abs(math.exp(-value) - 1) * 100 for value in actual)
    selected_model = min(maes, key=maes.get)  # type: ignore[arg-type]
    estimates = candidates[selected_model]
    final_weights = _recency_weights(len(x), half_life=252, floor=0.08)
    beta = _ridge_fit(x, y, alpha=12.0, weights=final_weights)
    current_features = features(len(prices) - 1)
    ridge_prediction = _predict(beta, current_features)
    predicted_return = {"ridge": ridge_prediction, "contrarian": -ridge_prediction, "momentum": current_features[3] * horizon}[selected_model]
    residuals = [a - p for a, p in zip(actual, estimates)]
    sigma = statistics.pstdev(residuals)
    current = prices[-1]
    prediction = current * math.exp(predicted_return)
    direction = round(100 * sum((p >= 0) == (a >= 0) for p, a in zip(estimates, actual)) / len(actual), 1)
    chart_rows = selected[-260:]
    return {
        "source": "TGJU multi-year daily",
        "unit": "تومان / گرم", "karat": karat, "horizon": horizon_name,
        "horizonLabel": labels[horizon_name], "mode": "historical",
        "modeDescription": "مدل چندساله طلا، دلار، اونس و ریسک داخلی",
        "latest": {"timestamp": selected[-1]["date"], "price": round(current)},
        "prediction": round(prediction), "change": round(prediction-current),
        "changePercent": round((prediction/current-1)*100, 3),
        "rangeLow": round(current*math.exp(predicted_return-1.28*sigma)),
        "rangeHigh": round(current*math.exp(predicted_return+1.28*sigma)),
        "backtest": {"samples": test_size, "maePercent": round(maes[selected_model], 3), "directionAccuracy": direction},
        "benchmark": {"neutralMaePercent": round(neutral_mae, 3), "activeMaePercent": round(maes[selected_model], 3), "hasEdge": maes[selected_model] < neutral_mae, "improvementPercent": round((neutral_mae-maes[selected_model])/neutral_mae*100, 1) if neutral_mae else 0},
        "model": {"id": selected_model, "name": {"ridge":"رگرسیون چندمتغیره","contrarian":"بازگشت به میانگین","momentum":"مومنتوم"}[selected_model]},
        "observations": len(selected),
        "risk": {"domesticPremium": round(risk[-1], 4), "newsCoverageDays": sum(row.get("news_tone") is not None for row in selected)},
        "chart": [{"timestamp": row["date"], "price": round(float(row["gold18_toman"])*multiplier), "mode":"historical"} for row in chart_rows],
        "generatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "training": {
            "latestIncluded": selected[-1]["date"],
            "historyRows": len(selected),
            "recentWeightHalfLifeDays": 252,
            "oldestSampleWeight": round(final_weights[0], 4),
        },
    }


def market_analysis(
    series: list[dict[str, object]], meta: dict[str, object], karat: int,
    daily_rows: list[dict[str, object]], short_result: dict[str, object],
) -> dict[str, object]:
    """Bubble, model-weighted market stance and five technical resistance levels."""
    multiplier = karat / 18
    current = float(series[-1]["price18"]) * multiplier
    ounce = float(meta.get("ounceUsd") or 0)
    usd = float(meta.get("usdToman") or 0)
    theoretical = ounce * usd / 31.1034768 * (karat / 24) if ounce and usd else current
    bubble_amount = current - theoretical
    bubble_percent = (current / theoretical - 1) * 100 if theoretical else 0.0

    daily_prices = [float(row["gold18_toman"]) * multiplier for row in daily_rows[-1800:]]
    recent = daily_prices[-260:]
    daily_returns = [math.log(daily_prices[i] / daily_prices[i - 1]) for i in range(1, len(daily_prices))]
    changes = [daily_prices[i] - daily_prices[i - 1] for i in range(1, len(daily_prices))]
    gains = [max(value, 0) for value in changes[-14:]]
    losses = [max(-value, 0) for value in changes[-14:]]
    avg_gain = statistics.fmean(gains) if gains else 0
    avg_loss = statistics.fmean(losses) if losses else 0
    rsi = 100 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    sma20 = statistics.fmean(daily_prices[-20:])

    daily_prediction = long_term_forecast(daily_rows, karat, "daily")
    weekly_prediction = long_term_forecast(daily_rows, karat, "weekly")
    score = 0.0
    reasons: list[str] = []
    if bubble_percent >= 3:
        score -= 2; reasons.append("حباب مثبت بیش از ۳٪، ریسک اصلاح را بالا می‌برد")
    elif bubble_percent >= 1.5:
        score -= 1; reasons.append("حباب مثبت بازار")
    elif bubble_percent <= -3:
        score += 2; reasons.append("قیمت بیش از ۳٪ زیر ارزش نظری است")
    elif bubble_percent <= -1.5:
        score += 1; reasons.append("حباب منفی بازار")
    else:
        reasons.append("حباب در محدوده متعادل است")
    if rsi < 30:
        score += 1.5; reasons.append("RSI روزانه در ناحیه اشباع فروش است")
    elif rsi > 70:
        score -= 1.5; reasons.append("RSI روزانه در ناحیه اشباع خرید است")
    score += 0.5 if current >= sma20 else -0.5
    reasons.append("قیمت بالاتر از میانگین ۲۰روزه است" if current >= sma20 else "قیمت پایین‌تر از میانگین ۲۰روزه است")
    for result, strong_weight, weak_weight, label in (
        (short_result, .6, .2, "کوتاه‌مدت"),
        (daily_prediction, 1.0, .3, "روزانه"),
        (weekly_prediction, 1.2, .4, "هفتگی"),
    ):
        direction = 1 if float(result["changePercent"]) > 0 else -1
        weight = strong_weight if result["benchmark"]["hasEdge"] else weak_weight
        score += direction * weight
        reasons.append(f"مدل {label} {'صعودی' if direction > 0 else 'نزولی'} است" + ("" if result["benchmark"]["hasEdge"] else "، اما برتری آماری ندارد"))

    if score >= 2:
        stance, title = "buy", "متمایل به خرید پله‌ای"
    elif score <= -2:
        stance, title = "sell", "متمایل به فروش / کاهش ریسک"
    else:
        stance, title = "wait", "صبر و مشاهده"
    consensus = dict(meta.get("sourceConsensus") or {})
    if consensus.get("available") and not consensus.get("valid", True):
        stance, title = "wait", "توقف تحلیل؛ اختلاف زیاد بین منابع"
        reasons.insert(
            0, "اختلاف نرخ دلار بین منابع بیش از ۳٪ است؛ سیگنال تا همگرایی منابع معتبر نیست"
        )
    confidence = min(90, round(35 + abs(score) * 12 + 8 * sum(bool(r["benchmark"]["hasEdge"]) for r in (short_result, daily_prediction, weekly_prediction))))

    peaks = [recent[i] for i in range(3, len(recent) - 3) if recent[i] == max(recent[i - 3:i + 4]) and recent[i] > current]
    volatility = statistics.pstdev(daily_returns[-60:]) if len(daily_returns) >= 60 else 0.01
    step = max(current * 0.0075, current * volatility)
    candidates = sorted(peaks + [current + step * level for level in range(1, 8)])
    levels: list[float] = []
    for value in candidates:
        if value <= current:
            continue
        if not levels or (value - levels[-1]) / levels[-1] >= 0.004:
            levels.append(value)
        if len(levels) == 5:
            break
    troughs = [recent[i] for i in range(3, len(recent) - 3) if recent[i] == min(recent[i - 3:i + 4]) and recent[i] < current]
    support_candidates = sorted(troughs + [current - step * level for level in range(1, 8)], reverse=True)
    supports: list[float] = []
    for value in support_candidates:
        if value <= 0 or value >= current:
            continue
        if not supports or (supports[-1] - value) / supports[-1] >= 0.004:
            supports.append(value)
        if len(supports) == 5:
            break
    entry_allocations = {
        "buy": [25, 25, 20, 15, 15], "wait": [15, 20, 20, 20, 25], "sell": [10, 15, 20, 25, 30],
    }[stance]
    exit_allocations = {
        "buy": [10, 15, 20, 25, 30], "wait": [15, 20, 20, 20, 25], "sell": [25, 25, 20, 15, 15],
    }[stance]
    stop_loss = supports[-1] - step * 0.75 if supports else current - step * 5.75
    upside_break = levels[0] + step * 0.20 if levels else current + step * 1.20
    downside_break = supports[0] - step * 0.20 if supports else current - step * 1.20
    return {
        "bubble": {
            "marketPrice": round(current), "theoreticalPrice": round(theoretical),
            "amount": round(bubble_amount), "percent": round(bubble_percent, 2),
            "type": "positive" if bubble_amount > 0 else "negative" if bubble_amount < 0 else "neutral",
        },
        "stance": {"id": stance, "title": title, "score": round(score, 2), "confidence": confidence, "reasons": reasons},
        "indicators": {"rsi14": round(rsi, 1), "sma20": round(sma20)},
        "resistances": [
            {"level": index + 1, "price": round(value), "distancePercent": round((value / current - 1) * 100, 2)}
            for index, value in enumerate(levels)
        ],
        "breakLevels": {
            "upside": round(upside_break),
            "upsideDistancePercent": round((upside_break / current - 1) * 100, 2),
            "downside": round(downside_break),
            "downsideDistancePercent": round((downside_break / current - 1) * 100, 2),
        },
        "lowerResistances": [
            {
                "level": index + 1, "price": round(value),
                "distancePercent": round((value / current - 1) * 100, 2),
                "activatesBelow": round(value - step * 0.20),
            }
            for index, value in enumerate(supports)
        ],
        "tradePlan": {
            "mode": stance,
            "summary": {
                "buy": "ورود پله‌ای با وزن بیشتر در پله‌های نزدیک",
                "wait": "ورود و خروج مشروط؛ بدون عجله در پله اول",
                "sell": "ورود محتاطانه و خروج سریع‌تر در مقاومت‌های نزدیک",
            }[stance],
            "entries": [
                {"level": index + 1, "price": round(value), "distancePercent": round((value / current - 1) * 100, 2), "allocationPercent": entry_allocations[index]}
                for index, value in enumerate(supports)
            ],
            "exits": [
                {"level": index + 1, "price": round(value), "distancePercent": round((value / current - 1) * 100, 2), "allocationPercent": exit_allocations[index]}
                for index, value in enumerate(levels)
            ],
            "stopLoss": round(stop_loss),
            "stopLossDistancePercent": round((stop_loss / current - 1) * 100, 2),
        },
    }


def collect_market_data(include_daily: bool = False) -> dict[str, object]:
    """Collect and persist market data without requiring an open browser tab."""
    if not _collection_lock.acquire(blocking=False):
        return {**_collector_status, "busy": True}
    attempted_at = datetime.now().astimezone().isoformat(timespec="seconds")
    _collector_status["lastAttemptAt"] = attempted_at
    try:
        series, meta = build_iran_intraday(fetch_intraday_ticks())
        persist_intraday(series)

        with _db() as connection:
            daily_rows = [
                dict(row)
                for row in connection.execute("SELECT * FROM daily_market ORDER BY date")
            ]
        if include_daily or len(daily_rows) < 250:
            daily_rows = fetch_daily_markets()
        if daily_rows:
            daily_rows, _ = merge_latest_daily_row(daily_rows, series, meta)

        quote_at = datetime.fromtimestamp(
            float(series[-1]["timestamp"]) / 1000
        ).astimezone().isoformat(timespec="seconds")
        _collector_status.update({
            "lastSuccessAt": datetime.now().astimezone().isoformat(timespec="seconds"),
            "lastError": None,
            "lastQuoteAt": quote_at,
            "sourceMode": meta.get("dataSourceMode", "unknown"),
            "intradayCandles": len(series),
            "dailyRows": len(daily_rows),
        })
    except Exception as exc:
        _collector_status["lastError"] = str(exc)
        print(f"[collector] collection failed: {exc}")
    finally:
        _collection_lock.release()
    return dict(_collector_status)


def _collector_loop(interval_seconds: int) -> None:
    """Run a best-effort collector and refresh daily history every six hours."""
    next_daily_refresh = 0.0
    while True:
        now = time.time()
        include_daily = now >= next_daily_refresh
        result = collect_market_data(include_daily=include_daily)
        if include_daily and not result.get("lastError"):
            next_daily_refresh = now + 6 * 3600
        time.sleep(interval_seconds)


def start_collector() -> threading.Thread | None:
    enabled = os.environ.get("COLLECTOR_ENABLED", "1").strip().lower() not in {
        "0", "false", "no", "off",
    }
    interval = max(60, int(os.environ.get("COLLECT_INTERVAL_SECONDS", "300")))
    _collector_status.update({"enabled": enabled, "intervalSeconds": interval})
    if not enabled:
        return None
    thread = threading.Thread(
        target=_collector_loop,
        args=(interval,),
        name="gold-market-collector",
        daemon=True,
    )
    thread.start()
    return thread


def health_status() -> tuple[int, dict[str, object]]:
    """Report process/database health and collector degradation separately."""
    try:
        with _db() as connection:
            connection.execute("SELECT 1").fetchone()
            counts = {
                "intradayCandles": connection.execute(
                    "SELECT COUNT(*) FROM intraday_candles"
                ).fetchone()[0],
                "dailyRows": connection.execute(
                    "SELECT COUNT(*) FROM daily_market"
                ).fetchone()[0],
                "predictions": connection.execute(
                    "SELECT COUNT(*) FROM predictions"
                ).fetchone()[0],
            }
    except sqlite3.Error as exc:
        return 503, {
            "status": "unhealthy", "database": "unavailable", "error": str(exc),
        }
    collector_state = "degraded" if _collector_status.get("lastError") else "ok"
    return 200, {
        "status": collector_state,
        "database": "ok",
        "databasePath": str(DB_PATH),
        "counts": counts,
        "collector": dict(_collector_status),
        "checkedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / "static"), **kwargs)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            status, payload = health_status()
            self._json(status, payload)
            return
        if parsed.path == "/api/intraday":
            try:
                query = parse_qs(parsed.query)
                karat = int(query.get("karat", ["18"])[0])
                horizon = int(query.get("horizon", ["60"])[0])
                series, meta = build_iran_intraday(fetch_intraday_ticks())
                persist_intraday(series)
                result = intraday_forecast(series, meta, karat, horizon)
                with _db() as connection:
                    daily_rows = [dict(row) for row in connection.execute("SELECT * FROM daily_market ORDER BY date")]
                if len(daily_rows) < 250:
                    daily_rows = fetch_daily_markets()
                daily_rows, _ = merge_latest_daily_row(daily_rows, series, meta)
                result["marketAnalysis"] = market_analysis(series, meta, karat, daily_rows, result)
                persist_prediction(result, "short")
                self._json(200, result)
            except (ValueError, RuntimeError) as exc:
                self._json(400, {"error": str(exc)})
            except Exception as exc:
                self._json(500, {"error": f"خطای تحلیل درون‌روزی: {exc}"})
            return
        if parsed.path == "/api/longterm":
            try:
                query = parse_qs(parsed.query)
                karat = int(query.get("karat", ["18"])[0])
                horizon = query.get("horizon", ["daily"])[0]
                daily_rows = fetch_daily_markets()
                series, meta = build_iran_intraday(fetch_intraday_ticks())
                persist_intraday(series)
                daily_rows, data_quality = merge_latest_daily_row(daily_rows, series, meta)
                result = long_term_forecast(daily_rows, karat, horizon)
                result["dataQuality"] = data_quality
                short_context = intraday_forecast(series, meta, karat, 60)
                result["marketAnalysis"] = market_analysis(
                    series, meta, karat, daily_rows, short_context
                )
                persist_prediction(result, "long")
                self._json(200, result)
            except (ValueError, RuntimeError) as exc:
                self._json(400, {"error": str(exc)})
            except Exception as exc:
                self._json(500, {"error": f"خطای مدل بلندمدت: {exc}"})
            return
        if parsed.path == "/api/data-status":
            with _db() as connection:
                status = {
                    "intradayCandles": connection.execute("SELECT COUNT(*) FROM intraday_candles").fetchone()[0],
                    "dailyRows": connection.execute("SELECT COUNT(*) FROM daily_market").fetchone()[0],
                    "predictions": connection.execute("SELECT COUNT(*) FROM predictions").fetchone()[0],
                    "database": str(DB_PATH),
                }
            self._json(200, status)
            return
        if parsed.path != "/api/forecast":
            return super().do_GET()
        try:
            query = parse_qs(parsed.query)
            fineness = int(query.get("fineness", ["750"])[0])
            window = int(query.get("window", ["730"])[0])
            if not 1 <= fineness <= 999:
                raise ValueError("عیار باید بین ۱ و ۹۹۹ باشد.")
            window = min(2500, max(120, window))
            result = forecast(fetch_history(), fineness, window)
            self._json(200, result)
        except (ValueError, RuntimeError) as exc:
            self._json(400, {"error": str(exc)})
        except Exception as exc:  # Keep the local UI informative.
            self._json(500, {"error": f"خطای پیش‌بینی: {exc}"})

    def end_headers(self) -> None:
        if not self.path.startswith("/api/") and urlparse(self.path).path != "/health":
            self.send_header("Cache-Control", "no-store, max-age=0")
        super().end_headers()

    def _json(self, status: int, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")


def run_server() -> None:
    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "0.0.0.0")
    # Initialize/seed the persistent database before accepting requests.
    with _db():
        pass
    start_collector()
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"GoldScope is running at http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    run_server()

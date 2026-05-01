"""Live decision analysis for collector email reports.

This module builds a best-effort feature set for a single live Telegram signal,
computes indicator snapshots from recent Binance candles, and runs the saved
decision model when available. Failures are returned as structured status data
so email delivery can continue.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from feature_builder import INDICATOR_COLS, INDICATOR_TIMEFRAMES
from indicator_engine import compute_snapshot
from model_manager import MODEL_DIR, load_model, predict_signal
from ohlcv_collector import OHLCV_DB, _fetch_binance_klines, get_candles_before_signal, timeframe_to_ms
from scraping.ticker_parser import extract_direction

log = logging.getLogger(__name__)

LIVE_CANDLE_COUNT = 300
DEFAULT_MODEL_DIR = MODEL_DIR


def analyze_live_signal(
    ticker: str,
    message_text: str,
    scraping_result: dict | None = None,
    volume_data: dict | None = None,
    timestamp: str | None = None,
    model_dir: str = DEFAULT_MODEL_DIR,
    ohlcv_db: str = OHLCV_DB,
) -> dict:
    """Return action, reliability, model prediction, and indicator snapshots."""
    signal_dt = _parse_timestamp(timestamp)
    direction = extract_direction(message_text or "") or "UNKNOWN"
    symbol = _resolve_symbol(ticker, volume_data)

    feature_dict: dict[str, Any] = {
        "hour": signal_dt.hour,
        "weekday": signal_dt.weekday(),
    }

    snapshots, snapshot_meta, raw_candles = _build_indicator_features(
        symbol=symbol,
        signal_dt=signal_dt,
        volume_data=volume_data,
        ohlcv_db=ohlcv_db,
        feature_dict=feature_dict,
    )
    _add_pre_signal_features(feature_dict, raw_candles.get("5m", []))
    _add_scraping_context(feature_dict, scraping_result, volume_data)

    model_data = load_model(model_dir)
    if model_data is None:
        return _unavailable(
            ticker=ticker,
            symbol=symbol,
            direction=direction,
            reason="Saved model not found. Run main.py to train models/decision_model.pkl.",
            snapshots=snapshots,
            snapshot_meta=snapshot_meta,
        )

    try:
        prediction = predict_signal(model_data, feature_dict)
    except Exception as exc:
        log.exception("[decision] Prediction failed for %s", ticker)
        return _unavailable(
            ticker=ticker,
            symbol=symbol,
            direction=direction,
            reason=f"Prediction failed: {exc}",
            snapshots=snapshots,
            snapshot_meta=snapshot_meta,
        )

    feature_names = model_data.get("features", [])
    available_features = [
        name for name in feature_names
        if _has_value(feature_dict.get(name))
    ]
    coverage_pct = round(len(available_features) / max(len(feature_names), 1) * 100, 1)

    reliability = _reliability(
        confidence=float(prediction.get("confidence", 0)),
        model_f1=float(model_data.get("f1", 0) or 0) * 100,
        coverage=coverage_pct,
    )

    action = _trade_action(prediction.get("decision"), direction)

    return {
        "status": "ok",
        "ticker": ticker,
        "symbol": symbol,
        "direction": direction,
        "action": action,
        "model_decision": prediction.get("decision"),
        "predicted": prediction.get("predicted"),
        "confidence": prediction.get("confidence"),
        "probabilities": prediction.get("probabilities", {}),
        "reliability": reliability,
        "feature_coverage": {
            "available": len(available_features),
            "total": len(feature_names),
            "percent": coverage_pct,
        },
        "model": {
            "f1": round(float(model_data.get("f1", 0) or 0) * 100, 1),
            "accuracy": round(float(model_data.get("accuracy", 0) or 0) * 100, 1),
            "n_samples": int(model_data.get("n_samples", 0) or 0),
            "trained_at": model_data.get("trained_at"),
            "horizon": model_data.get("horizon", "1h"),
        },
        "snapshots": snapshots,
        "snapshot_meta": snapshot_meta,
        "notes": _notes(prediction, direction, snapshots, coverage_pct),
    }


def _parse_timestamp(timestamp: str | None) -> datetime:
    if not timestamp:
        return datetime.now(timezone.utc)
    try:
        normalized = timestamp.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _resolve_symbol(ticker: str, volume_data: dict | None) -> str:
    if volume_data and volume_data.get("symbol"):
        return str(volume_data["symbol"]).upper()
    return f"{ticker.upper()}USDT"


def _build_indicator_features(
    symbol: str,
    signal_dt: datetime,
    volume_data: dict | None,
    ohlcv_db: str,
    feature_dict: dict,
) -> tuple[dict, dict, dict]:
    snapshots: dict[str, dict] = {}
    snapshot_meta: dict[str, dict] = {}
    raw_candles: dict[str, list[dict]] = {}
    signal_ms = int(signal_dt.timestamp() * 1000)
    use_futures_hint = (
        volume_data
        and volume_data.get("exchange") == "binance"
        and volume_data.get("market") in {"futures", "linear"}
    )

    for tf in INDICATOR_TIMEFRAMES:
        candles = _fetch_live_candles(symbol, tf, signal_ms, bool(use_futures_hint))
        source = "binance_futures" if use_futures_hint else "binance_spot"

        if not candles and not use_futures_hint:
            candles = _fetch_live_candles(symbol, tf, signal_ms, True)
            source = "binance_futures"

        if not candles:
            candles = _load_cached_candles(ohlcv_db, symbol, tf, signal_ms)
            source = "ohlcv_cache" if candles else "none"

        raw_candles[tf] = candles
        snapshot_meta[tf] = {"source": source, "candles": len(candles)}

        snapshot = compute_snapshot(candles) if candles else None
        if not snapshot:
            continue

        snapshots[tf] = snapshot
        for col in INDICATOR_COLS:
            feature_dict[f"ind_{col}_{tf}"] = snapshot.get(col)

    return snapshots, snapshot_meta, raw_candles


def _fetch_live_candles(symbol: str, timeframe: str, end_ms: int, use_futures: bool) -> list[dict]:
    tf_ms = timeframe_to_ms(timeframe)
    start_ms = end_ms - (tf_ms * LIVE_CANDLE_COUNT)
    klines = _fetch_binance_klines(
        symbol=symbol,
        interval=timeframe,
        start_ms=start_ms,
        end_ms=end_ms,
        limit=LIVE_CANDLE_COUNT,
        use_futures=use_futures,
    )
    return _klines_to_candles(klines or [])


def _load_cached_candles(ohlcv_db: str, symbol: str, timeframe: str, signal_ms: int) -> list[dict]:
    if not Path(ohlcv_db).exists():
        return []
    try:
        return get_candles_before_signal(ohlcv_db, symbol, timeframe, signal_ms, LIVE_CANDLE_COUNT)
    except Exception as exc:
        log.debug("[decision] OHLCV cache unavailable for %s %s: %s", symbol, timeframe, exc)
        return []


def _klines_to_candles(klines: list) -> list[dict]:
    candles = []
    for k in klines:
        try:
            candles.append({
                "open_time": int(k[0]),
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
                "close_time": int(k[6]) if len(k) > 6 else None,
                "quote_volume": float(k[7]) if len(k) > 7 else None,
                "trade_count": int(k[8]) if len(k) > 8 else None,
                "taker_buy_vol": float(k[9]) if len(k) > 9 else None,
            })
        except (TypeError, ValueError, IndexError):
            continue
    return sorted(candles, key=lambda item: item["open_time"])


def _add_pre_signal_features(feature_dict: dict, candles_5m: list[dict]) -> None:
    if len(candles_5m) < 13:
        return

    closes = np.array([c["close"] for c in candles_5m], dtype=float)
    highs = np.array([c["high"] for c in candles_5m], dtype=float)
    volumes = np.array([c["volume"] for c in candles_5m], dtype=float)
    taker = np.array([
        c["taker_buy_vol"] if c.get("taker_buy_vol") is not None else np.nan
        for c in candles_5m
    ], dtype=float)

    _add_window_features(feature_dict, closes, highs, volumes, taker, bars=12, suffix="1h")
    if len(candles_5m) >= 49:
        _add_window_features(feature_dict, closes, highs, volumes, taker, bars=48, suffix="4h")
        feature_dict["pump_before_4h"] = _pump_pct(closes, highs, bars=48)
    if len(candles_5m) >= 289:
        feature_dict["pump_before_24h"] = _pump_pct(closes, highs, bars=288)


def _add_window_features(
    feature_dict: dict,
    closes: np.ndarray,
    highs: np.ndarray,
    volumes: np.ndarray,
    taker: np.ndarray,
    bars: int,
    suffix: str,
) -> None:
    start = closes[-bars - 1]
    end = closes[-1]
    if start:
        feature_dict[f"pre_pct_{suffix}"] = round((end / start - 1) * 100, 4)

    window_closes = closes[-bars - 1:]
    returns = np.diff(window_closes) / window_closes[:-1]
    if len(returns):
        feature_dict[f"pre_volatility_{suffix}"] = round(float(np.std(returns) * 100), 4)

    window_volumes = volumes[-bars:]
    avg_vol = float(np.mean(window_volumes[:-1])) if len(window_volumes) > 1 else 0.0
    if avg_vol > 0:
        feature_dict[f"pre_volume_rel_{suffix}"] = round(float(window_volumes[-1] / avg_vol), 4)

    if suffix == "1h":
        path = float(np.sum(np.abs(np.diff(window_closes))))
        if path > 0:
            feature_dict["pre_efficiency_1h"] = round(float(abs(end - start) / path), 4)

        taker_window = taker[-bars:]
        volume_sum = float(np.nansum(window_volumes))
        taker_sum = float(np.nansum(taker_window))
        if volume_sum > 0 and not math.isnan(taker_sum):
            feature_dict["pre_taker_buy_ratio_1h"] = round(taker_sum / volume_sum, 4)


def _pump_pct(closes: np.ndarray, highs: np.ndarray, bars: int) -> float | None:
    if len(closes) < bars + 1:
        return None
    base = closes[-bars - 1]
    if not base:
        return None
    return round((float(np.max(highs[-bars:])) / base - 1) * 100, 4)


def _add_scraping_context(
    feature_dict: dict,
    scraping_result: dict | None,
    volume_data: dict | None,
) -> None:
    # The current saved model does not use raw news/event counts, but keeping
    # trend/volume hints here lets future retrains adopt them without collector changes.
    if scraping_result:
        trends = scraping_result.get("trends")
        if _has_value(trends):
            feature_dict.setdefault("trend_score", trends)
    if volume_data and _has_value(volume_data.get("volume_ratio")):
        feature_dict.setdefault("pre_volume_rel_1h", volume_data.get("volume_ratio"))


def _trade_action(model_decision: str | None, direction: str) -> str:
    if model_decision != "EXECUTE":
        return "TUT"
    if direction == "SHORT":
        return "SAT"
    if direction in {"LONG", "LONG_IMPL", "UNKNOWN"}:
        return "AL"
    return "TUT"


def _reliability(confidence: float, model_f1: float, coverage: float) -> dict:
    score = round((0.50 * confidence) + (0.30 * model_f1) + (0.20 * coverage), 1)
    if score >= 70:
        label = "YUKSEK"
    elif score >= 55:
        label = "ORTA"
    else:
        label = "DUSUK"
    return {
        "score": score,
        "label": label,
        "inputs": {
            "model_confidence": round(confidence, 1),
            "model_f1": round(model_f1, 1),
            "feature_coverage": round(coverage, 1),
        },
    }


def _notes(prediction: dict, direction: str, snapshots: dict, coverage_pct: float) -> list[str]:
    notes = []
    if direction == "UNKNOWN":
        notes.append("Mesaj yonu net degil; AL/SAT karari model sinyaline gore temkinli yorumlanmali.")
    if prediction.get("decision") == "CAUTION":
        notes.append("Model sonucu temkinli bolgede; TUT/izle olarak raporlandi.")
    if prediction.get("decision") == "SKIP":
        notes.append("Model kayip/dusek avantaj bolgesi gordu; TUT olarak raporlandi.")
    if len(snapshots) < len(INDICATOR_TIMEFRAMES):
        notes.append("Tum timeframe indikatorleri hesaplanamadi; eksikler model medyani ile dolduruldu.")
    if coverage_pct < 60:
        notes.append("Feature kapsami dusuk; guvenilirlik bu nedenle sinirli.")
    return notes


def _unavailable(
    ticker: str,
    symbol: str,
    direction: str,
    reason: str,
    snapshots: dict | None = None,
    snapshot_meta: dict | None = None,
) -> dict:
    return {
        "status": "unavailable",
        "ticker": ticker,
        "symbol": symbol,
        "direction": direction,
        "action": "TUT",
        "reason": reason,
        "snapshots": snapshots or {},
        "snapshot_meta": snapshot_meta or {},
        "reliability": {"score": 0, "label": "YOK"},
        "notes": [reason],
    }


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, float) and math.isnan(value):
        return False
    return True

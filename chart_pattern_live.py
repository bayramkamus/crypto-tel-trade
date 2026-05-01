"""Live 15m chart pattern analysis for collector emails.

This module is only used by the live collector path. It fetches recent 15m
Binance candles, renders a candlestick PNG, and runs the exported chart pattern
model against that image.
"""

from __future__ import annotations

import importlib.util
import logging
import math
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ohlcv_collector import OHLCV_DB, _fetch_binance_klines, get_candles_before_signal, timeframe_to_ms

log = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parent
_MODEL_DIR_ENV = os.environ.get("CHART_PATTERN_MODEL_DIR")
if _MODEL_DIR_ENV:
    MODEL_DIR = Path(_MODEL_DIR_ENV).expanduser()
    if not MODEL_DIR.is_absolute():
        MODEL_DIR = _BASE_DIR / MODEL_DIR
else:
    MODEL_DIR = _BASE_DIR / "models" / "chart_pattern_model_1777159444"
INFERENCE_PATH = MODEL_DIR / "inference.py"
CHART_DIR = _BASE_DIR / "generated_charts"

TIMEFRAME = "15m"
LIVE_CANDLE_COUNT = 96

_INFERENCE_MODULE = None


def analyze_chart_pattern(
    ticker: str,
    timestamp: str | None = None,
    volume_data: dict | None = None,
    ohlcv_db: str = OHLCV_DB,
) -> dict:
    """Return chart image metadata plus BUY/SELL/NEUTRAL model output.

    Bu fonksiyon HIC raise ETMEZ. Hata olursa _unavailable dict doner,
    boylece live pipeline'in email adimi her durumda calisabilir.
    """
    try:
        signal_dt = _parse_timestamp(timestamp)
        symbol = _resolve_symbol(ticker, volume_data)
        signal_ms = int(signal_dt.timestamp() * 1000)
    except Exception as exc:
        log.exception("[chart-pattern] Parametre cozumu basarisiz: %s", ticker)
        return _unavailable(
            ticker=ticker,
            symbol=ticker.upper(),
            source="none",
            reason=f"Parametre cozumu basarisiz: {exc}",
        )

    try:
        candles, source = _load_15m_candles(symbol, signal_ms, volume_data, ohlcv_db)
    except Exception as exc:
        log.exception("[chart-pattern] Mum verisi cekilemedi: %s", ticker)
        return _unavailable(
            ticker=ticker,
            symbol=symbol,
            source="none",
            reason=f"Mum verisi cekilemedi: {exc}",
        )

    if len(candles) < 20:
        return _unavailable(
            ticker=ticker,
            symbol=symbol,
            source=source,
            reason=f"15m chart icin yeterli mum yok ({len(candles)}).",
        )

    try:
        image_path = _render_chart_image(ticker, symbol, candles, signal_dt)
    except Exception as exc:
        log.exception("[chart-pattern] Chart goruntusu uretilemedi: %s", ticker)
        return _unavailable(
            ticker=ticker,
            symbol=symbol,
            source=source,
            reason=f"Chart goruntusu uretilemedi: {exc}",
            candles=len(candles),
        )

    try:
        raw = _predict_chart(image_path)
    except Exception as exc:
        log.exception("[chart-pattern] Prediction failed for %s", ticker)
        return _unavailable(
            ticker=ticker,
            symbol=symbol,
            source=source,
            reason=f"Chart pattern tahmini calismadi: {exc}",
            image_path=image_path,
            candles=len(candles),
        )

    try:
        signal = str(raw.get("signal", "NEUTRAL")).upper()
        action = _signal_to_action(signal)
        pattern = raw.get("detected_pattern", "-")

        return {
            "status": "ok",
            "ticker": ticker,
            "symbol": symbol,
            "timeframe": TIMEFRAME,
            "source": source,
            "candles": len(candles),
            "image_path": str(image_path),
            "image_cid": _image_cid(ticker, signal_ms),
            "action": action,
            "signal": signal,
            "signal_confidence": raw.get("signal_confidence"),
            "detected_pattern": pattern,
            "pattern_confidence": raw.get("pattern_confidence"),
            "signal_from_pattern_rule": raw.get("signal_from_pattern_rule"),
            "heads_agree": raw.get("heads_agree"),
            "pattern_probabilities": raw.get("pattern_probabilities", {}),
            "signal_probabilities": raw.get("signal_probabilities", {}),
        }
    except Exception as exc:
        log.exception("[chart-pattern] Sonuc paketlenemedi: %s", ticker)
        return _unavailable(
            ticker=ticker,
            symbol=symbol,
            source=source,
            reason=f"Sonuc paketlenemedi: {exc}",
            image_path=image_path,
            candles=len(candles),
        )


def _load_15m_candles(
    symbol: str,
    end_ms: int,
    volume_data: dict | None,
    ohlcv_db: str,
) -> tuple[list[dict], str]:
    futures_hint = (
        volume_data
        and volume_data.get("exchange") == "binance"
        and volume_data.get("market") in {"futures", "linear"}
    )
    market_order = [bool(futures_hint), not bool(futures_hint)]
    seen: set[bool] = set()

    for use_futures in market_order:
        if use_futures in seen:
            continue
        seen.add(use_futures)
        candles = _fetch_live_candles(symbol, end_ms, use_futures)
        if candles:
            return candles, "binance_futures" if use_futures else "binance_spot"

    cached = _load_cached_candles(ohlcv_db, symbol, end_ms)
    if cached:
        return cached, "ohlcv_cache"
    return [], "none"


def _fetch_live_candles(symbol: str, end_ms: int, use_futures: bool) -> list[dict]:
    tf_ms = timeframe_to_ms(TIMEFRAME)
    start_ms = end_ms - (tf_ms * LIVE_CANDLE_COUNT)
    klines = _fetch_binance_klines(
        symbol=symbol,
        interval=TIMEFRAME,
        start_ms=start_ms,
        end_ms=end_ms,
        limit=LIVE_CANDLE_COUNT,
        use_futures=use_futures,
    )
    return _klines_to_candles(klines or [])


def _load_cached_candles(ohlcv_db: str, symbol: str, signal_ms: int) -> list[dict]:
    if not Path(ohlcv_db).exists():
        return []
    try:
        return get_candles_before_signal(ohlcv_db, symbol, TIMEFRAME, signal_ms, LIVE_CANDLE_COUNT)
    except Exception as exc:
        log.debug("[chart-pattern] OHLCV cache unavailable for %s %s: %s", symbol, TIMEFRAME, exc)
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
            })
        except (TypeError, ValueError, IndexError):
            continue
    return sorted(candles, key=lambda item: item["open_time"])


def _render_chart_image(
    ticker: str,
    symbol: str,
    candles: list[dict],
    signal_dt: datetime,
) -> Path:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ModuleNotFoundError as exc:
        raise RuntimeError("Pillow gerekli. models/chart_pattern_model_1777159444/requirements.txt kurun.") from exc

    CHART_DIR.mkdir(exist_ok=True)
    safe_ticker = re.sub(r"[^A-Za-z0-9_-]+", "_", ticker.upper()).strip("_") or "COIN"
    filename = f"{safe_ticker}_{TIMEFRAME}_{int(signal_dt.timestamp())}.png"
    image_path = CHART_DIR / filename

    width, height = 900, 520
    left, top, right, bottom = 64, 44, 42, 64
    plot_w = width - left - right
    plot_h = height - top - bottom

    bg = "#0d1117"
    panel = "#111820"
    grid = "#263241"
    text = "#c9d1d9"
    up = "#22aa66"
    down = "#ef5350"
    wick = "#d0d7de"

    image = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()

    draw.rectangle([left, top, width - right, height - bottom], fill=panel, outline="#30363d")

    lows = [c["low"] for c in candles if _finite(c.get("low"))]
    highs = [c["high"] for c in candles if _finite(c.get("high"))]
    min_price = min(lows)
    max_price = max(highs)
    price_range = max(max_price - min_price, abs(max_price) * 0.002, 1e-12)
    pad = price_range * 0.08
    min_price -= pad
    max_price += pad
    price_range = max_price - min_price

    def y_for(price: float) -> int:
        return int(top + ((max_price - price) / price_range) * plot_h)

    for i in range(5):
        y = top + int(plot_h * i / 4)
        draw.line([left, y, width - right, y], fill=grid)
        label_price = max_price - (price_range * i / 4)
        draw.text((width - right + 6, y - 6), _price_label(label_price), fill=text, font=font)

    for i in range(0, len(candles), max(1, len(candles) // 6)):
        x = left + int(plot_w * i / max(len(candles) - 1, 1))
        draw.line([x, top, x, height - bottom], fill="#1b2633")

    slot = plot_w / max(len(candles), 1)
    candle_w = max(3, int(slot * 0.58))

    for index, candle in enumerate(candles):
        center_x = int(left + slot * index + slot / 2)
        open_y = y_for(candle["open"])
        close_y = y_for(candle["close"])
        high_y = y_for(candle["high"])
        low_y = y_for(candle["low"])
        color = up if candle["close"] >= candle["open"] else down

        draw.line([center_x, high_y, center_x, low_y], fill=wick)
        top_y = min(open_y, close_y)
        bottom_y = max(open_y, close_y)
        if bottom_y == top_y:
            bottom_y += 1
        draw.rectangle(
            [
                center_x - candle_w // 2,
                top_y,
                center_x + candle_w // 2,
                bottom_y,
            ],
            fill=color,
            outline=color,
        )

    title = f"{symbol} {TIMEFRAME} candlestick"
    last_price = candles[-1]["close"]
    subtitle = f"Last: {_price_label(last_price)} | Candles: {len(candles)}"
    draw.text((left, 16), title, fill="#f0f6fc", font=font)
    draw.text((left + 220, 16), subtitle, fill=text, font=font)
    image.save(image_path, format="PNG", optimize=True)
    return image_path


def _predict_chart(image_path: Path) -> dict[str, Any]:
    module = _load_inference_module()
    return module.predict_chart(image_path)


def _load_inference_module():
    global _INFERENCE_MODULE
    if _INFERENCE_MODULE is not None:
        return _INFERENCE_MODULE
    if not INFERENCE_PATH.exists():
        raise FileNotFoundError(f"Inference file not found: {INFERENCE_PATH}")

    spec = importlib.util.spec_from_file_location("chart_pattern_model_1777159444_inference", INFERENCE_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load inference module: {INFERENCE_PATH}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _INFERENCE_MODULE = module
    return module


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
    upper = ticker.upper()
    return upper if upper.endswith("USDT") else f"{upper}USDT"


def _signal_to_action(signal: str) -> str:
    return {
        "BUY": "AL",
        "SELL": "SAT",
        "NEUTRAL": "TUT",
    }.get(signal.upper(), "TUT")


def _image_cid(ticker: str, signal_ms: int) -> str:
    safe_ticker = re.sub(r"[^A-Za-z0-9_-]+", "_", ticker.upper()).strip("_") or "COIN"
    return f"chart_pattern_{safe_ticker}_{signal_ms}"


def _price_label(value: float) -> str:
    abs_value = abs(value)
    if abs_value >= 100:
        return f"{value:,.2f}"
    if abs_value >= 1:
        return f"{value:,.4f}"
    return f"{value:.8f}".rstrip("0").rstrip(".")


def _finite(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _unavailable(
    ticker: str,
    symbol: str,
    source: str,
    reason: str,
    image_path: Path | None = None,
    candles: int = 0,
) -> dict:
    result = {
        "status": "unavailable",
        "ticker": ticker,
        "symbol": symbol,
        "timeframe": TIMEFRAME,
        "source": source,
        "candles": candles,
        "action": "TUT",
        "reason": reason,
    }
    if image_path is not None:
        result["image_path"] = str(image_path)
        result["image_cid"] = _image_cid(ticker, int(datetime.now(timezone.utc).timestamp() * 1000))
    return result

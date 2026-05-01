"""
Multi-Exchange API Modülü
===========================
Binance, Bybit ve OKX üzerinden fiyat ve hacim verisi çeker.
Sırasıyla dener — ilk bulunan exchange kullanılır.

Kullanım:
    from exchanges import resolve_exchange, fetch_volume_anomaly, fetch_klines

    # Hangi exchange'te bu coin var?
    info = resolve_exchange("PEPE")
    # → {"exchange": "binance", "market": "spot", "symbol": "PEPEUSDT"}

    # Volume anomali kontrolü (Yaklaşım A — reaktif)
    vol = fetch_volume_anomaly("PEPE")
    # → {"volume_ratio": 3.2, "current_vol": 150000, "avg_vol_1h": 47000, ...}
"""

import time
import logging
import requests

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────
# EXCHANGE API ENDPOINTS
# ─────────────────────────────────────────────────────────────────

EXCHANGES = {
    "binance_spot": {
        "name":      "binance",
        "market":    "spot",
        "klines":    "https://api.binance.com/api/v3/klines",
        "ticker24h": "https://api.binance.com/api/v3/ticker/24hr",
    },
    "binance_futures": {
        "name":      "binance",
        "market":    "futures",
        "klines":    "https://fapi.binance.com/fapi/v1/klines",
        "ticker24h": "https://fapi.binance.com/fapi/v1/ticker/24hr",
    },
    "bybit_spot": {
        "name":    "bybit",
        "market":  "spot",
        "klines":  "https://api.bybit.com/v5/market/kline",
        "ticker":  "https://api.bybit.com/v5/market/tickers",
    },
    "bybit_linear": {
        "name":    "bybit",
        "market":  "linear",
        "klines":  "https://api.bybit.com/v5/market/kline",
        "ticker":  "https://api.bybit.com/v5/market/tickers",
    },
    "okx_spot": {
        "name":    "okx",
        "market":  "spot",
        "klines":  "https://www.okx.com/api/v5/market/candles",
        "ticker":  "https://www.okx.com/api/v5/market/ticker",
    },
}

# Exchange deneme sırası
EXCHANGE_ORDER = [
    "binance_spot", "binance_futures",
    "bybit_spot", "bybit_linear",
    "okx_spot",
]

_TIMEOUT  = 10
_HEADERS  = {"Accept": "application/json"}

# Exchange + symbol cache: {"PEPEUSDT": "binance_spot"}
_exchange_cache: dict[str, str | None] = {}


# ─────────────────────────────────────────────────────────────────
# BİNANCE
# ─────────────────────────────────────────────────────────────────

def _binance_klines(url: str, symbol: str, interval: str,
                    start_ms: int = None, limit: int = 60) -> list | None:
    """Binance klines endpoint'i. Dönüş: [[open_time, o, h, l, c, vol, ...], ...]"""
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    if start_ms:
        params["startTime"] = start_ms
    try:
        r = requests.get(url, params=params, headers=_HEADERS, timeout=_TIMEOUT)
        if r.status_code == 429:
            retry = int(r.headers.get("Retry-After", 30))
            log.warning(f"[binance] Rate limit — {retry}s bekleniyor...")
            time.sleep(retry)
            r = requests.get(url, params=params, headers=_HEADERS, timeout=_TIMEOUT)
        if r.status_code == 200:
            data = r.json()
            if data and isinstance(data, list) and len(data) > 0:
                return data
    except Exception as e:
        log.debug(f"[binance] Klines hatası: {e}")
    return None


def _binance_check(exchange_key: str, symbol: str) -> bool:
    """Binance'te bu symbol var mı?"""
    url = EXCHANGES[exchange_key]["klines"]
    result = _binance_klines(url, symbol, "1m", limit=1)
    return result is not None


def _binance_volume_data(exchange_key: str, symbol: str) -> list | None:
    """Binance'ten son 60 x 1m mum verisini çek (volume bilgisi dahil)."""
    url = EXCHANGES[exchange_key]["klines"]
    return _binance_klines(url, symbol, "1m", limit=60)


# ─────────────────────────────────────────────────────────────────
# BYBIT
# ─────────────────────────────────────────────────────────────────

def _bybit_klines(url: str, symbol: str, category: str,
                  interval: str = "1", limit: int = 60) -> list | None:
    """
    Bybit V5 klines. interval: "1" = 1 dakika.
    Dönüş: [[open_time, o, h, l, c, vol, turnover], ...] (en yeniden eskiye)
    """
    params = {
        "category": category,
        "symbol":   symbol,
        "interval": interval,
        "limit":    limit,
    }
    try:
        r = requests.get(url, params=params, headers=_HEADERS, timeout=_TIMEOUT)
        if r.status_code == 200:
            data = r.json()
            result = data.get("result", {})
            klines = result.get("list", [])
            if klines and len(klines) > 0:
                # Bybit en yeniden eskiye döner — ters çevir
                klines.reverse()
                return klines
    except Exception as e:
        log.debug(f"[bybit] Klines hatası: {e}")
    return None


def _bybit_check(exchange_key: str, symbol: str) -> bool:
    """Bybit'te bu symbol var mı?"""
    category = "spot" if "spot" in exchange_key else "linear"
    url = EXCHANGES[exchange_key]["klines"]
    result = _bybit_klines(url, symbol, category, limit=1)
    return result is not None


def _bybit_volume_data(exchange_key: str, symbol: str) -> list | None:
    """Bybit'ten son 60 x 1m mum verisini çek."""
    category = "spot" if "spot" in exchange_key else "linear"
    url = EXCHANGES[exchange_key]["klines"]
    return _bybit_klines(url, symbol, category, limit=60)


# ─────────────────────────────────────────────────────────────────
# OKX
# ─────────────────────────────────────────────────────────────────

def _okx_klines(url: str, inst_id: str,
                bar: str = "1m", limit: int = 60) -> list | None:
    """
    OKX candles. inst_id formatı: "PEPE-USDT"
    Dönüş: [[ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm], ...]
    """
    params = {"instId": inst_id, "bar": bar, "limit": str(limit)}
    try:
        r = requests.get(url, params=params, headers=_HEADERS, timeout=_TIMEOUT)
        if r.status_code == 200:
            data = r.json()
            klines = data.get("data", [])
            if klines and len(klines) > 0:
                # OKX de en yeniden eskiye döner
                klines.reverse()
                return klines
    except Exception as e:
        log.debug(f"[okx] Klines hatası: {e}")
    return None


def _okx_check(exchange_key: str, symbol_usdt: str) -> bool:
    """OKX'te bu symbol var mı?"""
    inst_id = symbol_usdt.replace("USDT", "-USDT")
    url = EXCHANGES[exchange_key]["klines"]
    result = _okx_klines(url, inst_id, limit=1)
    return result is not None


def _okx_volume_data(exchange_key: str, symbol_usdt: str) -> list | None:
    """OKX'ten son 60 x 1m mum verisini çek."""
    inst_id = symbol_usdt.replace("USDT", "-USDT")
    url = EXCHANGES[exchange_key]["klines"]
    return _okx_klines(url, inst_id, limit=60)


# ─────────────────────────────────────────────────────────────────
# EXCHANGE DISPATCHER
# ─────────────────────────────────────────────────────────────────

_CHECK_FN = {
    "binance_spot":    _binance_check,
    "binance_futures": _binance_check,
    "bybit_spot":      _bybit_check,
    "bybit_linear":    _bybit_check,
    "okx_spot":        _okx_check,
}

_VOLUME_FN = {
    "binance_spot":    _binance_volume_data,
    "binance_futures": _binance_volume_data,
    "bybit_spot":      _bybit_volume_data,
    "bybit_linear":    _bybit_volume_data,
    "okx_spot":        _okx_volume_data,
}


# ─────────────────────────────────────────────────────────────────
# ANA FONKSİYONLAR
# ─────────────────────────────────────────────────────────────────

def resolve_exchange(ticker: str) -> dict | None:
    """
    Ticker için ilk bulunan exchange'i döner.

    Dönüş:
      {
        "exchange":     "binance",
        "market":       "spot",
        "exchange_key": "binance_spot",
        "symbol":       "PEPEUSDT",
      }
    veya None (hiçbir exchange'te bulunamadı).
    """
    symbol = ticker.upper() + "USDT"

    # Cache kontrol
    if symbol in _exchange_cache:
        cached_key = _exchange_cache[symbol]
        if cached_key is None:
            return None
        ex = EXCHANGES[cached_key]
        return {
            "exchange":     ex["name"],
            "market":       ex["market"],
            "exchange_key": cached_key,
            "symbol":       symbol,
        }

    # Sırasıyla dene
    for ex_key in EXCHANGE_ORDER:
        check_fn = _CHECK_FN.get(ex_key)
        if not check_fn:
            continue

        log.debug(f"[exchange] {symbol} → {ex_key} deneniyor...")
        try:
            found = check_fn(ex_key, symbol)
        except Exception:
            found = False

        if found:
            _exchange_cache[symbol] = ex_key
            ex = EXCHANGES[ex_key]
            log.info(
                f"[exchange] {symbol} bulundu: "
                f"{ex['name']} {ex['market']}"
            )
            return {
                "exchange":     ex["name"],
                "market":       ex["market"],
                "exchange_key": ex_key,
                "symbol":       symbol,
            }

        time.sleep(0.2)

    # Hiçbirinde bulunamadı
    _exchange_cache[symbol] = None
    log.warning(f"[exchange] {symbol} hiçbir borsada bulunamadı.")
    return None


def fetch_volume_anomaly(ticker: str) -> dict | None:
    """
    Yaklaşım A — Reaktif volume anomali tespiti.

    Sinyal geldiğinde:
      1. Exchange çözümle (Binance → Bybit → OKX)
      2. Son 60 x 1 dakikalık mum verisini çek
      3. Son mumun hacmini, önceki 59 mumun ortalamasıyla karşılaştır
      4. volume_ratio = current_vol / avg_vol_1h

    Eşik: volume_ratio > 2.0 → anormal hacim

    Dönüş:
      {
        "ticker":        "PEPE",
        "exchange":      "binance",
        "market":        "spot",
        "symbol":        "PEPEUSDT",
        "current_vol":   150000.0,
        "avg_vol_1h":    47000.0,
        "volume_ratio":  3.19,
        "is_anomaly":    True,
        "current_price": 0.00001234,
      }
    veya None (veri çekilemedi).
    """
    # 1. Exchange çözümle
    ex_info = resolve_exchange(ticker)
    if not ex_info:
        return None

    ex_key = ex_info["exchange_key"]
    symbol = ex_info["symbol"]

    # 2. Son 60 x 1m kline çek
    volume_fn = _VOLUME_FN.get(ex_key)
    if not volume_fn:
        return None

    klines = volume_fn(ex_key, symbol)
    if not klines or len(klines) < 10:
        log.warning(f"[volume] {symbol} yeterli kline verisi yok.")
        return None

    # 3. Volume ve fiyat çıkar (exchange'e göre format farklı)
    volumes, prices = _extract_volumes_and_prices(ex_key, klines)

    if not volumes or len(volumes) < 2:
        return None

    # Son mum (şu anki)
    current_vol   = volumes[-1]
    current_price = prices[-1] if prices else None

    # Önceki mumların ortalaması (son mum hariç)
    prev_volumes = volumes[:-1]
    avg_vol = sum(prev_volumes) / len(prev_volumes) if prev_volumes else 0

    # Volume ratio
    volume_ratio = round(current_vol / avg_vol, 2) if avg_vol > 0 else 0.0
    is_anomaly = volume_ratio > 2.0

    result = {
        "ticker":        ticker,
        "exchange":      ex_info["exchange"],
        "market":        ex_info["market"],
        "symbol":        symbol,
        "current_vol":   round(current_vol, 2),
        "avg_vol_1h":    round(avg_vol, 2),
        "volume_ratio":  volume_ratio,
        "is_anomaly":    is_anomaly,
        "current_price": current_price,
    }

    level = "⚠️ ANOMALİ" if is_anomaly else "normal"
    log.info(
        f"[volume] {ticker} | ratio={volume_ratio}x | "
        f"vol={current_vol:,.0f} vs avg={avg_vol:,.0f} | {level}"
    )
    return result


def _extract_volumes_and_prices(exchange_key: str,
                                klines: list) -> tuple[list, list]:
    """Exchange formatına göre volume ve close price listesi çıkarır."""
    volumes = []
    prices  = []

    if "binance" in exchange_key:
        # Binance: [open_time, o, h, l, c, vol, close_time, quote_vol, ...]
        for k in klines:
            try:
                volumes.append(float(k[5]))    # base volume
                prices.append(float(k[4]))     # close price
            except (IndexError, ValueError):
                pass

    elif "bybit" in exchange_key:
        # Bybit V5: [startTime, open, high, low, close, volume, turnover]
        for k in klines:
            try:
                volumes.append(float(k[5]))
                prices.append(float(k[4]))
            except (IndexError, ValueError):
                pass

    elif "okx" in exchange_key:
        # OKX: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
        for k in klines:
            try:
                volumes.append(float(k[5]))
                prices.append(float(k[4]))
            except (IndexError, ValueError):
                pass

    return volumes, prices


# ─────────────────────────────────────────────────────────────────
# BACKTEST İÇİN KLINE YARDIMCISI
# ─────────────────────────────────────────────────────────────────

def fetch_klines_multi(ticker: str, signal_ts_ms: int,
                       intervals: dict = None) -> dict:
    """
    Backtest için multi-exchange kline verisi.
    Binance bulamazsa Bybit/OKX'ten çeker.

    intervals varsayılan: {"1m": 62, "1h": 26}

    Dönüş:
      {
        "exchange":     "binance",
        "market":       "spot",
        "symbol":       "PEPEUSDT",
        "klines_1m":    [[...], ...],
        "klines_1h":    [[...], ...],
      }
    """
    if intervals is None:
        intervals = {"1m": 62, "1h": 26}

    ex_info = resolve_exchange(ticker)
    if not ex_info:
        return {"exchange": "not_found", "market": "not_found",
                "symbol": ticker + "USDT"}

    ex_key = ex_info["exchange_key"]
    symbol = ex_info["symbol"]
    result = {
        "exchange": ex_info["exchange"],
        "market":   ex_info["market"],
        "symbol":   symbol,
    }

    for interval, limit in intervals.items():
        if "binance" in ex_key:
            kl = _binance_klines(
                EXCHANGES[ex_key]["klines"], symbol, interval,
                start_ms=signal_ts_ms, limit=limit
            )
        elif "bybit" in ex_key:
            category = "spot" if "spot" in ex_key else "linear"
            # Bybit interval mapping: "1m" → "1", "1h" → "60"
            bybit_interval = _bybit_interval(interval)
            kl = _bybit_klines(
                EXCHANGES[ex_key]["klines"], symbol, category,
                interval=bybit_interval, limit=limit
            )
        elif "okx" in ex_key:
            inst_id = symbol.replace("USDT", "-USDT")
            kl = _okx_klines(
                EXCHANGES[ex_key]["klines"], inst_id,
                bar=interval, limit=limit
            )
        else:
            kl = None

        result[f"klines_{interval}"] = kl
        time.sleep(0.15)

    return result


def _bybit_interval(interval: str) -> str:
    """Binance interval formatını Bybit'e çevirir."""
    mapping = {
        "1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30",
        "1h": "60", "2h": "120", "4h": "240",
        "1d": "D", "1w": "W",
    }
    return mapping.get(interval, interval)

#!/usr/bin/env python3
"""
Teknik İndikatör Hesaplama Motoru
====================================
OHLCV verisi üzerinden RSI, MACD, Bollinger Bands, EMA ve Volume
indikatörlerini hesaplar. Her sinyal anı için snapshot oluşturur.

Kullanım:
    python indicator_engine.py
    python indicator_engine.py --force       # Tüm snapshot'ları yeniden hesapla
    python indicator_engine.py --timeframes 1h 4h

Gereksinimler:
    pip install numpy
"""

import sqlite3
import logging
import argparse
import numpy as np
from datetime import datetime, timezone
from pathlib import Path

from ohlcv_collector import get_candles_before_signal, OHLCV_DB

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────
# SABİTLER
# ─────────────────────────────────────────────────────────────────

BT_DB_PATH  = "backtest_results.db"
INDICATOR_TIMEFRAMES = ["5m", "15m", "1h"]

# Minimum mum sayısı (EMA200 + biraz buffer)
MIN_CANDLES = 50
IDEAL_CANDLES = 250


# ─────────────────────────────────────────────────────────────────
# İNDİKATÖR HESAPLAMA FONKSİYONLARI (Pure NumPy)
# ─────────────────────────────────────────────────────────────────

def calc_ema(data: np.ndarray, period: int) -> np.ndarray:
    """Exponential Moving Average hesapla."""
    if len(data) < period:
        return np.full(len(data), np.nan)

    ema = np.full(len(data), np.nan)
    k = 2.0 / (period + 1)

    # SMA ile başlat
    ema[period - 1] = np.mean(data[:period])

    # EMA iterasyon
    for i in range(period, len(data)):
        ema[i] = data[i] * k + ema[i - 1] * (1 - k)

    return ema


def calc_sma(data: np.ndarray, period: int) -> np.ndarray:
    """Simple Moving Average hesapla."""
    if len(data) < period:
        return np.full(len(data), np.nan)

    sma = np.full(len(data), np.nan)
    cumsum = np.cumsum(data)
    sma[period - 1:] = (cumsum[period - 1:] - np.concatenate(([0], cumsum[:-period]))) / period
    return sma


def calc_rsi(closes: np.ndarray, period: int = 14) -> float | None:
    """
    RSI (Relative Strength Index) hesapla.
    Wilder's smoothing (EMA-based) yöntemi.
    Son değeri döner.
    """
    if len(closes) < period + 1:
        return None

    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    # İlk ortalama (SMA)
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])

    # Wilder's smoothing
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return round(rsi, 2)


def calc_macd(closes: np.ndarray,
              fast: int = 12, slow: int = 26, signal: int = 9) -> dict | None:
    """
    MACD hesapla. Döner: {line, signal, histogram, cross}
    cross: 1=bullish (MACD > Signal oldu), -1=bearish, 0=yok
    """
    if len(closes) < slow + signal:
        return None

    ema_fast = calc_ema(closes, fast)
    ema_slow = calc_ema(closes, slow)

    macd_line = ema_fast - ema_slow

    # Signal line: MACD'nin EMA'sı
    # MACD değerleri slow-1 indexten itibaren geçerli
    valid_start = slow - 1
    macd_valid = macd_line[valid_start:]

    signal_line_valid = calc_ema(macd_valid, signal)

    # Son geçerli değerler
    if np.isnan(signal_line_valid[-1]) or np.isnan(macd_valid[-1]):
        return None

    line_val = float(macd_valid[-1])
    signal_val = float(signal_line_valid[-1])
    histogram = line_val - signal_val

    # Cross detection (son 3 bar)
    cross = 0
    if len(macd_valid) >= 3 and len(signal_line_valid) >= 3:
        prev_diff = float(macd_valid[-2]) - float(signal_line_valid[-2])
        curr_diff = float(macd_valid[-1]) - float(signal_line_valid[-1])

        if prev_diff <= 0 and curr_diff > 0:
            cross = 1   # Bullish crossover
        elif prev_diff >= 0 and curr_diff < 0:
            cross = -1  # Bearish crossunder

    return {
        "macd_line": round(line_val, 8),
        "macd_signal": round(signal_val, 8),
        "macd_histogram": round(histogram, 8),
        "macd_cross": cross,
    }


def calc_bollinger(closes: np.ndarray,
                   period: int = 20, std_dev: float = 2.0) -> dict | None:
    """
    Bollinger Bands hesapla.
    Döner: {upper, lower, pctb, bandwidth, squeeze}
    """
    if len(closes) < period:
        return None

    sma = calc_sma(closes, period)
    if np.isnan(sma[-1]):
        return None

    middle = float(sma[-1])

    # Son 'period' kapanışın standart sapması
    window = closes[-period:]
    std = float(np.std(window, ddof=1))

    upper = middle + std_dev * std
    lower = middle - std_dev * std

    price = float(closes[-1])

    # %B: fiyatın band içindeki pozisyonu
    band_width = upper - lower
    pctb = (price - lower) / band_width if band_width > 0 else 0.5

    # Bandwidth: volatilite ölçüsü
    bandwidth = band_width / middle if middle > 0 else 0

    # Squeeze: bandwidth tarihsel ortalamanın altında mı?
    # Son 50 bar bandwidth'i hesapla
    squeeze = 0
    if len(closes) >= period + 30:
        recent_bws = []
        for i in range(30):
            idx = len(closes) - 1 - i
            if idx < period:
                break
            w = closes[idx - period + 1:idx + 1]
            s = float(np.std(w, ddof=1))
            m = float(np.mean(w))
            if m > 0:
                recent_bws.append((2 * std_dev * s) / m)

        if recent_bws:
            avg_bw = np.mean(recent_bws)
            if bandwidth < avg_bw * 0.75:
                squeeze = 1

    return {
        "bb_upper": round(upper, 8),
        "bb_lower": round(lower, 8),
        "bb_pctb": round(pctb, 4),
        "bb_bandwidth": round(bandwidth, 6),
        "bb_squeeze": squeeze,
    }


def calc_ema_set(closes: np.ndarray) -> dict | None:
    """
    EMA(9, 21, 50, 200) seti hesapla.
    Döner: {ema_9, ema_21, ema_50, ema_200, ema_alignment, price_vs_ema200}
    """
    periods = [9, 21, 50, 200]
    emas = {}

    for p in periods:
        if len(closes) >= p:
            e = calc_ema(closes, p)
            emas[p] = float(e[-1]) if not np.isnan(e[-1]) else None
        else:
            emas[p] = None

    price = float(closes[-1])

    # EMA alignment: kaç EMA doğru sırada?
    # Bullish: price > ema9 > ema21 > ema50 (> ema200)
    alignment = 0
    chain = [price] + [emas[p] for p in [9, 21, 50] if emas[p] is not None]
    if len(chain) >= 2:
        for i in range(len(chain) - 1):
            if chain[i] > chain[i + 1]:
                alignment += 1
            else:
                alignment -= 1

    # Price vs EMA200
    price_vs_200 = None
    if emas[200] and emas[200] > 0:
        price_vs_200 = round((price / emas[200] - 1) * 100, 2)

    return {
        "ema_9": round(emas[9], 8) if emas[9] else None,
        "ema_21": round(emas[21], 8) if emas[21] else None,
        "ema_50": round(emas[50], 8) if emas[50] else None,
        "ema_200": round(emas[200], 8) if emas[200] else None,
        "ema_alignment": alignment,
        "price_vs_ema200": price_vs_200,
    }


def calc_volume_indicators(closes: np.ndarray, volumes: np.ndarray) -> dict | None:
    """
    OBV + Volume MA + Volume Ratio hesapla.
    Döner: {obv, obv_slope, volume_ratio, volume_trend}
    """
    if len(closes) < 20 or len(volumes) < 20:
        return None

    # OBV (On-Balance Volume)
    obv = np.zeros(len(closes))
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            obv[i] = obv[i - 1] + volumes[i]
        elif closes[i] < closes[i - 1]:
            obv[i] = obv[i - 1] - volumes[i]
        else:
            obv[i] = obv[i - 1]

    # OBV slope (son 5 bar lineer regresyon eğimi)
    obv_recent = obv[-5:]
    if len(obv_recent) == 5:
        x = np.arange(5)
        slope = float(np.polyfit(x, obv_recent, 1)[0])
        # Normalize: slope / |ortalama OBV|
        avg_obv = np.mean(np.abs(obv_recent))
        obv_slope = slope / avg_obv if avg_obv > 0 else 0
    else:
        obv_slope = 0

    # Volume ratio: son mum / MA(20)
    vol_ma20 = float(np.mean(volumes[-20:]))
    vol_ratio = float(volumes[-1]) / vol_ma20 if vol_ma20 > 0 else 1.0

    # Volume trend: son 5 bar ortalaması vs son 20 bar ortalaması
    vol_recent = float(np.mean(volumes[-5:])) if len(volumes) >= 5 else float(volumes[-1])
    vol_trend = 1 if vol_recent > vol_ma20 else -1

    return {
        "obv": round(float(obv[-1]), 2),
        "obv_slope": round(obv_slope, 6),
        "volume_ratio": round(vol_ratio, 4),
        "volume_trend": vol_trend,
    }


# ─────────────────────────────────────────────────────────────────
# BİRLEŞİK İNDİKATÖR SNAPSHOT
# ─────────────────────────────────────────────────────────────────

def compute_snapshot(candles: list[dict]) -> dict | None:
    """
    Mum listesi (dict) alır, tüm indikatörleri hesaplar.
    Döner: tek bir snapshot dict.
    """
    if not candles or len(candles) < MIN_CANDLES:
        return None

    closes = np.array([c["close"] for c in candles], dtype=float)
    highs = np.array([c["high"] for c in candles], dtype=float)
    lows = np.array([c["low"] for c in candles], dtype=float)
    volumes = np.array([c["volume"] for c in candles], dtype=float)

    snapshot = {}

    # RSI
    rsi = calc_rsi(closes, 14)
    snapshot["rsi_14"] = rsi

    # MACD
    macd = calc_macd(closes)
    if macd:
        snapshot.update(macd)
    else:
        snapshot.update({
            "macd_line": None, "macd_signal": None,
            "macd_histogram": None, "macd_cross": None,
        })

    # Bollinger Bands
    bb = calc_bollinger(closes)
    if bb:
        snapshot.update(bb)
    else:
        snapshot.update({
            "bb_upper": None, "bb_lower": None,
            "bb_pctb": None, "bb_bandwidth": None, "bb_squeeze": None,
        })

    # EMA set
    emas = calc_ema_set(closes)
    if emas:
        snapshot.update(emas)
    else:
        snapshot.update({
            "ema_9": None, "ema_21": None, "ema_50": None, "ema_200": None,
            "ema_alignment": None, "price_vs_ema200": None,
        })

    # Volume
    vol = calc_volume_indicators(closes, volumes)
    if vol:
        snapshot.update(vol)
    else:
        snapshot.update({
            "obv": None, "obv_slope": None,
            "volume_ratio": None, "volume_trend": None,
        })

    return snapshot


# ─────────────────────────────────────────────────────────────────
# VERİTABANI — İndikatör Snapshot Tablosu
# ─────────────────────────────────────────────────────────────────

SNAPSHOT_COLUMNS = [
    "rsi_14",
    "macd_line", "macd_signal", "macd_histogram", "macd_cross",
    "bb_upper", "bb_lower", "bb_pctb", "bb_bandwidth", "bb_squeeze",
    "ema_9", "ema_21", "ema_50", "ema_200", "ema_alignment", "price_vs_ema200",
    "obv", "obv_slope", "volume_ratio", "volume_trend",
]


def init_indicator_table(bt_db: str = BT_DB_PATH):
    """indicator_snapshots tablosunu backtest DB'de oluştur."""
    conn = sqlite3.connect(bt_db)

    cols_sql = ",\n".join(f"    {col} REAL" for col in SNAPSHOT_COLUMNS)

    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS indicator_snapshots (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id   INTEGER NOT NULL,
            timeframe   TEXT    NOT NULL,
            {cols_sql},
            computed_at TEXT,
            UNIQUE(signal_id, timeframe)
        )
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_indicator_lookup
        ON indicator_snapshots(signal_id, timeframe)
    """)

    conn.commit()
    conn.close()
    log.info("[indicators] indicator_snapshots tablosu hazır")


def get_computed_snapshots(bt_db: str = BT_DB_PATH) -> set:
    """Daha önce hesaplanmış (signal_id, timeframe) çiftlerini döner."""
    conn = sqlite3.connect(bt_db)
    try:
        rows = conn.execute(
            "SELECT signal_id, timeframe FROM indicator_snapshots"
        ).fetchall()
        return {(r[0], r[1]) for r in rows}
    except sqlite3.OperationalError:
        return set()
    finally:
        conn.close()


def insert_snapshot(bt_conn, signal_id: int, timeframe: str, snapshot: dict):
    """Tek bir snapshot'ı DB'ye yaz."""
    now = datetime.now(timezone.utc).isoformat()
    values = [signal_id, timeframe]
    values.extend(snapshot.get(col) for col in SNAPSHOT_COLUMNS)
    values.append(now)

    placeholders = ",".join(["?"] * len(values))
    cols = "signal_id, timeframe, " + ", ".join(SNAPSHOT_COLUMNS) + ", computed_at"

    bt_conn.execute(
        f"INSERT OR REPLACE INTO indicator_snapshots ({cols}) VALUES ({placeholders})",
        values
    )


# ─────────────────────────────────────────────────────────────────
# ANA İŞLEM
# ─────────────────────────────────────────────────────────────────

def iso_to_ms(iso_str: str) -> int:
    """ISO format tarih → Unix timestamp milisaniye."""
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def compute_all_indicators(timeframes: list[str] = None,
                           bt_db: str = BT_DB_PATH,
                           ohlcv_db: str = OHLCV_DB,
                           force: bool = False):
    """
    Tüm sinyaller × timeframe'ler için indikatör snapshot hesapla.
    """
    if timeframes is None:
        timeframes = INDICATOR_TIMEFRAMES

    # Tablo oluştur
    init_indicator_table(bt_db)

    # Mevcut snapshot'lar (resume desteği)
    if force:
        conn = sqlite3.connect(bt_db)
        conn.execute("DELETE FROM indicator_snapshots")
        conn.commit()
        conn.close()
        computed = set()
        log.warning("[indicators] --force: Tüm snapshot'lar silindi")
    else:
        computed = get_computed_snapshots(bt_db)
        log.info(f"[indicators] Mevcut snapshot: {len(computed)}")

    # Sinyalleri yükle
    bt_conn = sqlite3.connect(bt_db)
    signals = bt_conn.execute("""
        SELECT id, ticker, symbol, signal_ts
        FROM signal_backtest
        WHERE symbol IS NOT NULL
        ORDER BY signal_ts
    """).fetchall()

    total = len(signals)
    total_tf = total * len(timeframes)
    skip_count = 0
    computed_count = 0
    error_count = 0

    log.info(f"[indicators] {total} sinyal × {len(timeframes)} TF = {total_tf} snapshot hesaplanacak")

    batch_size = 50
    batch_count = 0

    for i, (sig_id, ticker, symbol, signal_ts) in enumerate(signals, 1):
        signal_ms = iso_to_ms(signal_ts)

        for tf in timeframes:
            # Resume kontrolü
            if (sig_id, tf) in computed:
                skip_count += 1
                continue

            # OHLCV verisi çek
            candles = get_candles_before_signal(ohlcv_db, symbol, tf, signal_ms, IDEAL_CANDLES)

            if not candles or len(candles) < MIN_CANDLES:
                error_count += 1
                continue

            # Snapshot hesapla
            snapshot = compute_snapshot(candles)
            if snapshot:
                insert_snapshot(bt_conn, sig_id, tf, snapshot)
                computed_count += 1
                batch_count += 1
            else:
                error_count += 1

        # Batch commit
        if batch_count >= batch_size:
            bt_conn.commit()
            batch_count = 0

        # Progress
        if i % 100 == 0 or i == total:
            log.info(
                f"  [{i}/{total}] hesaplanan: {computed_count}, "
                f"atlanan: {skip_count}, hata: {error_count}"
            )

    # Final commit
    bt_conn.commit()
    bt_conn.close()

    log.info(f"\n{'='*60}")
    log.info(f"[indicators] HESAPLAMA TAMAMLANDI")
    log.info(f"  Yeni snapshot: {computed_count}")
    log.info(f"  Atlanan (mevcut): {skip_count}")
    log.info(f"  Yetersiz veri: {error_count}")
    log.info(f"{'='*60}")

    return {
        "computed": computed_count,
        "skipped": skip_count,
        "errors": error_count,
    }


# ─────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Teknik İndikatör Hesaplama")
    parser.add_argument("--bt-db", default=BT_DB_PATH, help="Backtest DB yolu")
    parser.add_argument("--ohlcv-db", default=OHLCV_DB, help="OHLCV DB yolu")
    parser.add_argument("--timeframes", nargs="+", default=INDICATOR_TIMEFRAMES,
                        help="Hesaplanacak timeframe'ler")
    parser.add_argument("--force", action="store_true",
                        help="Tüm snapshot'ları yeniden hesapla")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )

    compute_all_indicators(
        timeframes=args.timeframes,
        bt_db=args.bt_db,
        ohlcv_db=args.ohlcv_db,
        force=args.force,
    )


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
OHLCV Veri Toplama Modülü
============================
Backtest veritabanındaki tüm coinler için tarihsel OHLCV verisi toplar.
Binance API kullanır, resume desteklidir.

Kullanım:
    python ohlcv_collector.py
    python ohlcv_collector.py --force          # Cache'i sıfırla
    python ohlcv_collector.py --timeframes 1h 4h 1d   # Sadece belirli TF'ler

Gereksinimler:
    pip install requests
"""

import time
import sqlite3
import logging
import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────
# SABİTLER
# ─────────────────────────────────────────────────────────────────

BT_DB_PATH   = "backtest_results.db"
OHLCV_DB     = "ohlcv_data.db"

BINANCE_SPOT    = "https://api.binance.com/api/v3/klines"
BINANCE_FUTURES = "https://fapi.binance.com/fapi/v1/klines"

ALL_TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h", "1d"]

# Binance kline limit: max 1000 per request
BATCH_SIZE = 1000

# İndikatör warm-up için gereken ekstra mum sayısı
# EMA(200) en fazla warm-up gerektiren indikatör
WARMUP_CANDLES = 250

# Rate limiting
REQUEST_DELAY   = 0.1   # saniye (normal)
RATE_LIMIT_WAIT = 30     # 429 gelirse bekle

_HEADERS = {"Accept": "application/json"}
_TIMEOUT = 15


# ─────────────────────────────────────────────────────────────────
# VERİTABANI
# ─────────────────────────────────────────────────────────────────

def init_ohlcv_db(db_path: str = OHLCV_DB):
    """OHLCV veritabanı ve tablolarını oluştur."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS ohlcv (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol          TEXT    NOT NULL,
            timeframe       TEXT    NOT NULL,
            open_time       INTEGER NOT NULL,
            open            REAL,
            high            REAL,
            low             REAL,
            close           REAL,
            volume          REAL,
            close_time      INTEGER,
            quote_volume    REAL,
            trade_count     INTEGER,
            taker_buy_vol   REAL,
            taker_buy_quote REAL,
            UNIQUE(symbol, timeframe, open_time)
        )
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_ohlcv_lookup
        ON ohlcv(symbol, timeframe, open_time)
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS collection_status (
            symbol          TEXT NOT NULL,
            timeframe       TEXT NOT NULL,
            first_open_time INTEGER,
            last_open_time  INTEGER,
            total_candles   INTEGER DEFAULT 0,
            updated_at      TEXT,
            PRIMARY KEY(symbol, timeframe)
        )
    """)

    # Exchange çözümleme cache
    conn.execute("""
        CREATE TABLE IF NOT EXISTS exchange_map (
            ticker      TEXT PRIMARY KEY,
            symbol      TEXT NOT NULL,
            exchange    TEXT NOT NULL,
            market      TEXT NOT NULL,
            resolved_at TEXT
        )
    """)

    conn.commit()
    conn.close()
    log.info(f"[ohlcv] Veritabanı hazır: {db_path}")


def get_collection_status(conn, symbol: str, timeframe: str) -> dict | None:
    """Belirli sembol+TF için toplama durumunu döner."""
    row = conn.execute(
        "SELECT first_open_time, last_open_time, total_candles "
        "FROM collection_status WHERE symbol=? AND timeframe=?",
        (symbol, timeframe)
    ).fetchone()
    if row:
        return {"first": row[0], "last": row[1], "count": row[2]}
    return None


def update_collection_status(conn, symbol: str, timeframe: str,
                             first_time: int, last_time: int, count: int):
    """Toplama durumunu güncelle."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("""
        INSERT INTO collection_status (symbol, timeframe, first_open_time,
                                       last_open_time, total_candles, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol, timeframe) DO UPDATE SET
            first_open_time = MIN(first_open_time, excluded.first_open_time),
            last_open_time  = MAX(last_open_time, excluded.last_open_time),
            total_candles   = excluded.total_candles,
            updated_at      = excluded.updated_at
    """, (symbol, timeframe, first_time, last_time, count, now))
    conn.commit()


# ─────────────────────────────────────────────────────────────────
# YARDIMCI FONKSİYONLAR
# ─────────────────────────────────────────────────────────────────

def timeframe_to_ms(tf: str) -> int:
    """Timeframe string → milisaniye."""
    units = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}
    num = int(tf[:-1])
    unit = tf[-1]
    return num * units[unit]


def get_tickers_and_dates(bt_db: str = BT_DB_PATH) -> list[dict]:
    """
    Backtest DB'den benzersiz tickerları ve tarih aralıklarını çek.
    Döner: [{"ticker": "BTC", "symbol": "BTCUSDT", "first_ts": ..., "last_ts": ..., "count": ...}]
    """
    conn = sqlite3.connect(bt_db)
    rows = conn.execute("""
        SELECT ticker, symbol,
               MIN(signal_ts) as first_ts,
               MAX(signal_ts) as last_ts,
               COUNT(*) as cnt
        FROM signal_backtest
        WHERE ticker IS NOT NULL AND symbol IS NOT NULL
        GROUP BY ticker
        ORDER BY cnt DESC
    """).fetchall()
    conn.close()

    results = []
    for ticker, symbol, first_ts, last_ts, cnt in rows:
        if not symbol:
            continue
        results.append({
            "ticker": ticker,
            "symbol": symbol,
            "first_ts": first_ts,
            "last_ts": last_ts,
            "count": cnt,
        })

    log.info(f"[ohlcv] {len(results)} benzersiz coin bulundu")
    return results


def iso_to_ms(iso_str: str) -> int:
    """ISO format tarih → Unix timestamp milisaniye."""
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


# ─────────────────────────────────────────────────────────────────
# BİNANCE API
# ─────────────────────────────────────────────────────────────────

def _fetch_binance_klines(symbol: str, interval: str,
                          start_ms: int, end_ms: int = None,
                          limit: int = BATCH_SIZE,
                          use_futures: bool = False) -> list | None:
    """Binance'den kline verisi çek."""
    url = BINANCE_FUTURES if use_futures else BINANCE_SPOT
    params = {
        "symbol": symbol,
        "interval": interval,
        "startTime": start_ms,
        "limit": limit,
    }
    if end_ms:
        params["endTime"] = end_ms

    try:
        r = requests.get(url, params=params, headers=_HEADERS, timeout=_TIMEOUT)

        if r.status_code == 429:
            retry = int(r.headers.get("Retry-After", RATE_LIMIT_WAIT))
            log.warning(f"[binance] Rate limit — {retry}s bekleniyor...")
            time.sleep(retry)
            r = requests.get(url, params=params, headers=_HEADERS, timeout=_TIMEOUT)

        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list):
                return data
            return None

        if r.status_code == 400:
            # Symbol bulunamadı
            return None

        log.warning(f"[binance] HTTP {r.status_code} for {symbol} {interval}")
        return None

    except requests.exceptions.Timeout:
        log.warning(f"[binance] Timeout: {symbol} {interval}")
        return None
    except Exception as e:
        log.warning(f"[binance] Hata: {symbol} {interval} — {e}")
        return None


def detect_exchange(symbol: str, ohlcv_conn) -> dict | None:
    """
    Coin'in hangi Binance market'te olduğunu tespit et.
    Önce cache'e bak, yoksa spot → futures dene.
    """
    # Cache kontrol
    row = ohlcv_conn.execute(
        "SELECT symbol, exchange, market FROM exchange_map WHERE ticker=?",
        (symbol.replace("USDT", ""),)
    ).fetchone()
    if row:
        return {"symbol": row[0], "exchange": row[1], "market": row[2]}

    ticker = symbol.replace("USDT", "")

    # Spot dene
    data = _fetch_binance_klines(symbol, "1d", start_ms=0, limit=1, use_futures=False)
    if data and len(data) > 0:
        now = datetime.now(timezone.utc).isoformat()
        ohlcv_conn.execute(
            "INSERT OR REPLACE INTO exchange_map VALUES (?,?,?,?,?)",
            (ticker, symbol, "binance", "spot", now)
        )
        ohlcv_conn.commit()
        return {"symbol": symbol, "exchange": "binance", "market": "spot"}

    # Futures dene
    data = _fetch_binance_klines(symbol, "1d", start_ms=0, limit=1, use_futures=True)
    if data and len(data) > 0:
        now = datetime.now(timezone.utc).isoformat()
        ohlcv_conn.execute(
            "INSERT OR REPLACE INTO exchange_map VALUES (?,?,?,?,?)",
            (ticker, symbol, "binance", "futures", now)
        )
        ohlcv_conn.commit()
        return {"symbol": symbol, "exchange": "binance", "market": "futures"}

    log.warning(f"[ohlcv] {symbol} Binance'da bulunamadı — atlanıyor")
    return None


# ─────────────────────────────────────────────────────────────────
# ANA TOPLAMA FONKSİYONU
# ─────────────────────────────────────────────────────────────────

def collect_symbol_timeframe(ohlcv_conn, symbol: str, timeframe: str,
                             start_ms: int, end_ms: int,
                             use_futures: bool = False) -> int:
    """
    Belirli symbol + timeframe için tüm OHLCV verisini topla.
    Resume destekli: daha önce toplanan verinin kaldığı yerden devam eder.
    Döner: eklenen mum sayısı.
    """
    # Resume kontrolü
    status = get_collection_status(ohlcv_conn, symbol, timeframe)
    if status and status["last"]:
        # Kaldığı yerden devam
        resume_ms = status["last"] + timeframe_to_ms(timeframe)
        if resume_ms >= end_ms:
            log.debug(f"  {symbol} {timeframe}: zaten güncel ({status['count']} mum)")
            return 0
        if resume_ms > start_ms:
            start_ms = resume_ms

    tf_ms = timeframe_to_ms(timeframe)
    total_inserted = 0
    current_start = start_ms

    while current_start < end_ms:
        data = _fetch_binance_klines(
            symbol, timeframe,
            start_ms=current_start,
            end_ms=end_ms,
            limit=BATCH_SIZE,
            use_futures=use_futures,
        )

        if not data or len(data) == 0:
            break

        # Batch insert
        rows = []
        for k in data:
            try:
                rows.append((
                    symbol, timeframe,
                    int(k[0]),      # open_time
                    float(k[1]),    # open
                    float(k[2]),    # high
                    float(k[3]),    # low
                    float(k[4]),    # close
                    float(k[5]),    # volume
                    int(k[6]),      # close_time
                    float(k[7]),    # quote_volume
                    int(k[8]),      # trade_count
                    float(k[9]),    # taker_buy_vol
                    float(k[10]),   # taker_buy_quote
                ))
            except (IndexError, ValueError, TypeError):
                continue

        if rows:
            ohlcv_conn.executemany("""
                INSERT OR IGNORE INTO ohlcv
                (symbol, timeframe, open_time, open, high, low, close,
                 volume, close_time, quote_volume, trade_count,
                 taker_buy_vol, taker_buy_quote)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, rows)
            ohlcv_conn.commit()
            total_inserted += len(rows)

            first_t = rows[0][2]
            last_t = rows[-1][2]
            update_collection_status(
                ohlcv_conn, symbol, timeframe,
                first_t, last_t, total_inserted
            )

        # Sonraki batch
        last_open_time = int(data[-1][0])
        next_start = last_open_time + tf_ms

        if next_start <= current_start:
            break
        current_start = next_start

        # Rate limiting
        time.sleep(REQUEST_DELAY)

    return total_inserted


def collect_all(timeframes: list[str] = None,
                bt_db: str = BT_DB_PATH,
                ohlcv_db: str = OHLCV_DB,
                force: bool = False):
    """
    Tüm coinler ve timeframe'ler için OHLCV verisi topla.
    """
    if timeframes is None:
        timeframes = ALL_TIMEFRAMES

    # DB başlat
    init_ohlcv_db(ohlcv_db)
    ohlcv_conn = sqlite3.connect(ohlcv_db)

    if force:
        log.warning("[ohlcv] --force: Tüm veriler sıfırlanıyor!")
        ohlcv_conn.execute("DELETE FROM ohlcv")
        ohlcv_conn.execute("DELETE FROM collection_status")
        ohlcv_conn.commit()

    # Coin listesi
    tickers = get_tickers_and_dates(bt_db)
    if not tickers:
        log.error("[ohlcv] Backtest DB'de coin bulunamadı!")
        ohlcv_conn.close()
        return

    # Global tarih aralığı
    all_first = min(iso_to_ms(t["first_ts"]) for t in tickers)
    all_last  = max(iso_to_ms(t["last_ts"]) for t in tickers)

    # Buffer: indikatör warm-up (en uzun TF için)
    buffer_days = 10  # EMA200 on 1h = ~8.3 gün
    buffer_ms = buffer_days * 86_400_000
    global_start = all_first - buffer_ms

    # 1d için daha fazla buffer (EMA50 on daily = 50 gün)
    daily_buffer_ms = 55 * 86_400_000
    daily_start = all_first - daily_buffer_ms

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    global_end = min(all_last + 86_400_000, now_ms)  # son sinyal + 1 gün

    total_coins = len(tickers)
    total_candles = 0
    skipped = 0
    errors = 0

    log.info(f"[ohlcv] Toplama başlıyor: {total_coins} coin × {len(timeframes)} TF")
    log.info(f"[ohlcv] Tarih aralığı: {datetime.fromtimestamp(global_start/1000, tz=timezone.utc).strftime('%Y-%m-%d')} → {datetime.fromtimestamp(global_end/1000, tz=timezone.utc).strftime('%Y-%m-%d')}")

    for i, t in enumerate(tickers, 1):
        symbol = t["symbol"]
        ticker = t["ticker"]

        log.info(f"[{i}/{total_coins}] {ticker} ({symbol}) — {t['count']} sinyal")

        # Exchange tespit
        ex = detect_exchange(symbol, ohlcv_conn)
        if not ex:
            skipped += 1
            continue

        use_futures = ex["market"] == "futures"

        for tf in timeframes:
            start = daily_start if tf == "1d" else global_start
            try:
                n = collect_symbol_timeframe(
                    ohlcv_conn, symbol, tf,
                    start_ms=start,
                    end_ms=global_end,
                    use_futures=use_futures,
                )
                total_candles += n
                if n > 0:
                    log.info(f"  {tf}: +{n:,} mum")
            except Exception as e:
                log.error(f"  {tf}: HATA — {e}")
                errors += 1

        # Coin arası kısa bekleme
        time.sleep(0.2)

    ohlcv_conn.close()

    log.info(f"\n{'='*60}")
    log.info(f"[ohlcv] TOPLAMA TAMAMLANDI")
    log.info(f"  Toplam mum: {total_candles:,}")
    log.info(f"  Atlanan coin: {skipped}")
    log.info(f"  Hatalar: {errors}")
    log.info(f"{'='*60}")

    return {"total_candles": total_candles, "skipped": skipped, "errors": errors}


# ─────────────────────────────────────────────────────────────────
# YARDIMCI: Sinyal anı OHLCV çekme (indicator_engine tarafından kullanılır)
# ─────────────────────────────────────────────────────────────────

def get_candles_before_signal(ohlcv_db: str, symbol: str,
                              timeframe: str, signal_ts_ms: int,
                              count: int = 250) -> list[dict]:
    """
    Sinyal anından önceki `count` mum verisini veritabanından çek.
    Döner: [{"open_time", "open", "high", "low", "close", "volume", ...}, ...]
    """
    conn = sqlite3.connect(ohlcv_db)
    rows = conn.execute("""
        SELECT open_time, open, high, low, close, volume,
               quote_volume, trade_count, taker_buy_vol
        FROM ohlcv
        WHERE symbol = ? AND timeframe = ? AND open_time <= ?
        ORDER BY open_time DESC
        LIMIT ?
    """, (symbol, timeframe, signal_ts_ms, count)).fetchall()
    conn.close()

    if not rows:
        return []

    # Kronolojik sıra (eskiden yeniye)
    rows.reverse()
    return [
        {
            "open_time": r[0], "open": r[1], "high": r[2],
            "low": r[3], "close": r[4], "volume": r[5],
            "quote_volume": r[6], "trade_count": r[7],
            "taker_buy_vol": r[8],
        }
        for r in rows
    ]


# ─────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="OHLCV Veri Toplama")
    parser.add_argument("--bt-db", default=BT_DB_PATH, help="Backtest DB yolu")
    parser.add_argument("--ohlcv-db", default=OHLCV_DB, help="OHLCV DB yolu")
    parser.add_argument("--timeframes", nargs="+", default=ALL_TIMEFRAMES,
                        choices=ALL_TIMEFRAMES, help="Toplanacak timeframe'ler")
    parser.add_argument("--force", action="store_true",
                        help="Cache sıfırla, tüm veriyi yeniden topla")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Detaylı loglama")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )

    collect_all(
        timeframes=args.timeframes,
        bt_db=args.bt_db,
        ohlcv_db=args.ohlcv_db,
        force=args.force,
    )


if __name__ == "__main__":
    main()

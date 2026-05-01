#!/usr/bin/env python3
"""
Özellik Birleştirme Modülü
===========================
Telegram sinyal özelliklerini teknik indikatörlerle birleştirir.
Her indikatör 3 timeframe'den (5m, 15m, 1h) ayrı ayrı feature olarak eklenir.

Kullanım:
    python feature_builder.py
    python feature_builder.py --force

Gereksinimler:
    pip install pandas numpy
"""

import sqlite3
import logging
import argparse
import numpy as np
import pandas as pd
from datetime import datetime, timezone

log = logging.getLogger(__name__)

BT_DB_PATH = "backtest_results.db"
INDICATOR_TIMEFRAMES = ["5m", "15m", "1h"]

# Her TF için çıkarılacak indikatör kolonları
INDICATOR_COLS = [
    "rsi_14",
    "macd_histogram", "macd_cross",
    "bb_pctb", "bb_bandwidth", "bb_squeeze",
    "ema_alignment", "price_vs_ema200",
    "obv_slope", "volume_ratio",
]

# DB'ye yazılacak feature kolonları: ind_{col}_{tf} formatında
FEATURE_COLUMNS = []
for tf in INDICATOR_TIMEFRAMES:
    for col in INDICATOR_COLS:
        FEATURE_COLUMNS.append(f"ind_{col}_{tf}")


def init_feature_table(bt_db: str = BT_DB_PATH):
    conn = sqlite3.connect(bt_db)

    cols_sql = ",\n".join(f"    {col} REAL" for col in FEATURE_COLUMNS)
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS signal_features (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id       INTEGER UNIQUE NOT NULL,
            {cols_sql},
            computed_at     TEXT,
            FOREIGN KEY(signal_id) REFERENCES signal_backtest(id)
        )
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_feature_signal
        ON signal_features(signal_id)
    """)

    conn.commit()
    conn.close()
    log.info("[features] signal_features tablosu hazır")


def get_computed_features(bt_db: str = BT_DB_PATH) -> set:
    conn = sqlite3.connect(bt_db)
    try:
        rows = conn.execute("SELECT signal_id FROM signal_features").fetchall()
        return {r[0] for r in rows}
    except sqlite3.OperationalError:
        return set()
    finally:
        conn.close()


def build_all_features(bt_db: str = BT_DB_PATH, force: bool = False):
    """Tüm sinyaller için indikatör feature'larını 3 TF'den ayrı ayrı hesapla."""

    # Eski tablo varsa sil ve yeniden oluştur (şema değişti)
    conn = sqlite3.connect(bt_db)
    if force:
        conn.execute("DROP TABLE IF EXISTS signal_features")
        conn.commit()
        log.warning("[features] --force: signal_features tablosu silindi, yeniden oluşturuluyor")
    conn.close()

    init_feature_table(bt_db)

    if not force:
        computed = get_computed_features(bt_db)
        log.info(f"[features] Mevcut özellik: {len(computed)}")
    else:
        computed = set()

    conn = sqlite3.connect(bt_db)

    signals = conn.execute("""
        SELECT id, direction FROM signal_backtest
        WHERE symbol IS NOT NULL
    """).fetchall()

    # İndikatör snapshot'ları toplu yükle
    try:
        from indicator_engine import SNAPSHOT_COLUMNS
        snapshots = conn.execute("""
            SELECT signal_id, timeframe,
                   rsi_14, macd_line, macd_signal, macd_histogram, macd_cross,
                   bb_upper, bb_lower, bb_pctb, bb_bandwidth, bb_squeeze,
                   ema_9, ema_21, ema_50, ema_200, ema_alignment, price_vs_ema200,
                   obv, obv_slope, volume_ratio, volume_trend
            FROM indicator_snapshots
        """).fetchall()
    except sqlite3.OperationalError:
        log.error("[features] indicator_snapshots tablosu bulunamadı! Önce indicator_engine.py çalıştırın.")
        conn.close()
        return

    # Index: signal_id → {tf → dict}
    snap_index = {}
    for row in snapshots:
        sig_id = row[0]
        tf = row[1]
        ind_dict = dict(zip(SNAPSHOT_COLUMNS, row[2:]))
        if sig_id not in snap_index:
            snap_index[sig_id] = {}
        snap_index[sig_id][tf] = ind_dict

    total = len(signals)
    computed_count = 0
    skip_count = 0
    no_data_count = 0

    log.info(f"[features] {total} sinyal için özellik hesaplanacak ({len(FEATURE_COLUMNS)} feature × 3 TF)")

    now = datetime.now(timezone.utc).isoformat()

    for i, (sig_id, direction) in enumerate(signals, 1):
        if sig_id in computed:
            skip_count += 1
            continue

        tf_indicators = snap_index.get(sig_id, {})
        if not tf_indicators:
            no_data_count += 1
            continue

        values = {"signal_id": sig_id, "computed_at": now}

        # Her timeframe × her indikatör → ayrı feature
        for tf in INDICATOR_TIMEFRAMES:
            ind_data = tf_indicators.get(tf, {})
            for col in INDICATOR_COLS:
                feat_name = f"ind_{col}_{tf}"
                values[feat_name] = ind_data.get(col)

        cols = list(values.keys())
        placeholders = ",".join(["?"] * len(cols))
        col_str = ",".join(cols)

        conn.execute(
            f"INSERT OR REPLACE INTO signal_features ({col_str}) VALUES ({placeholders})",
            [values[c] for c in cols]
        )
        computed_count += 1

        if i % 200 == 0:
            conn.commit()
            log.info(f"  [{i}/{total}] hesaplanan: {computed_count}")

    conn.commit()
    conn.close()

    sep = "=" * 60
    log.info(f"\n{sep}")
    log.info(f"[features] ÖZELLİK HESAPLAMA TAMAMLANDI")
    log.info(f"  Yeni: {computed_count}")
    log.info(f"  Atlanan: {skip_count}")
    log.info(f"  Veri yok: {no_data_count}")
    log.info(f"  Feature sayısı: {len(FEATURE_COLUMNS)}")
    log.info(sep)

    return {"computed": computed_count, "skipped": skip_count, "no_data": no_data_count}


def load_full_dataset(bt_db: str = BT_DB_PATH) -> pd.DataFrame:
    """signal_backtest + signal_features birleştirip tam DataFrame döner."""
    conn = sqlite3.connect(bt_db)

    df_signals = pd.read_sql_query("SELECT * FROM signal_backtest", conn)

    try:
        df_features = pd.read_sql_query("SELECT * FROM signal_features", conn)
    except Exception:
        log.warning("[features] signal_features tablosu yok, sadece sinyal verileri döner")
        conn.close()
        return df_signals

    conn.close()

    if not df_features.empty:
        df_features = df_features.drop(columns=["id", "computed_at"], errors="ignore")
        df = df_signals.merge(df_features, left_on="id", right_on="signal_id", how="left")
        df.drop(columns=["signal_id"], errors="ignore", inplace=True)
    else:
        df = df_signals

    df["signal_ts"] = pd.to_datetime(df["signal_ts"])
    df["hour"] = df["signal_ts"].dt.hour
    df["weekday"] = df["signal_ts"].dt.weekday

    log.info(f"[features] Veri seti: {len(df)} satır × {len(df.columns)} kolon")
    return df


def main():
    parser = argparse.ArgumentParser(description="Özellik Birleştirme (3 TF × 10 İndikatör)")
    parser.add_argument("--bt-db", default=BT_DB_PATH, help="Backtest DB yolu")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )

    build_all_features(bt_db=args.bt_db, force=args.force)


if __name__ == "__main__":
    main()

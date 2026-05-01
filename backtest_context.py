#!/usr/bin/env python3
"""
Piyasa Bağlamı Zenginleştirici — backtest_results.db
======================================================
Fear & Greed Index verisiyle signal_backtest tablosunu zenginleştirir.

Kaynak: https://api.alternative.me/fng/
  - Ücretsiz, API key gerektirmez
  - Tarihsel veri destekler (?limit=365)
  - Günlük granülarite (gün başına 1 değer)
  - 0-100 arası: 0=Extreme Fear, 100=Extreme Greed

Eklenen kolonlar:
  fear_greed          INTEGER  — sinyal tarihindeki FnG değeri (0-100)
  fear_greed_label    TEXT     — "Extreme Fear" / "Fear" / "Neutral" / "Greed" / "Extreme Greed"
  fng_momentum_7d     REAL     — fear_greed / ort(son 7 gün)  →  >1 iyileşiyor, <1 kötüleşiyor
  fng_momentum_14d    REAL     — fear_greed / ort(son 14 gün) →  orta vadeli ivme

Kullanım:
    python backtest_context.py
    python backtest_context.py --force    # önbelleği yoksay
    python backtest_context.py --dry-run  # sadece plan göster

Gereksinim:
    pip install requests
"""

import time
import sqlite3
import argparse
import logging
from datetime import datetime, timezone, timedelta

import requests

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)

# ─────────────────────────────────────────────────────────────────
# SABITLER
# ─────────────────────────────────────────────────────────────────

BT_DB_PATH = "backtest_results.db"
FNG_API    = "https://api.alternative.me/fng/"

# ─────────────────────────────────────────────────────────────────
# VERİTABANI — schema & migration
# ─────────────────────────────────────────────────────────────────

def init_context_tables(db_path: str = BT_DB_PATH):
    """
    fng_cache tablosunu oluşturur ve signal_backtest'e
    fear_greed / fear_greed_label kolonlarını ekler.
    """
    conn = sqlite3.connect(db_path, timeout=30)

    # FnG günlük önbellek
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fng_cache (
            date         TEXT PRIMARY KEY,   -- YYYY-MM-DD
            value        INTEGER,             -- 0-100
            label        TEXT,                -- Extreme Fear / Fear / Neutral / ...
            fetched_at   TEXT
        )
    """)

    # signal_backtest'e yeni kolon migration
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    if "signal_backtest" in tables:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(signal_backtest)")}
        for col, typedef in [
            ("fear_greed",       "INTEGER"),
            ("fear_greed_label", "TEXT"),
            ("fng_momentum_7d",  "REAL"),
            ("fng_momentum_14d", "REAL"),
        ]:
            if col not in existing:
                conn.execute(f"ALTER TABLE signal_backtest ADD COLUMN {col} {typedef}")
                log.info(f"Migration: signal_backtest.{col} eklendi")

    conn.commit()
    conn.close()


# ─────────────────────────────────────────────────────────────────
# FNG CACHE — okuma & yazma
# ─────────────────────────────────────────────────────────────────

def load_fng_cache(db_path: str) -> dict[str, tuple[int, str]]:
    """Mevcut önbelleği {date: (value, label)} şeklinde döner."""
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT date, value, label FROM fng_cache").fetchall()
        conn.close()
        return {r[0]: (r[1], r[2]) for r in rows}
    except sqlite3.OperationalError:
        return {}


def save_fng_cache(db_path: str, entries: list[dict]):
    """
    FnG verilerini önbelleğe yazar.
    entries: [{"date": "YYYY-MM-DD", "value": 42, "label": "Fear"}, ...]
    """
    conn = sqlite3.connect(db_path, timeout=30)
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        """
        INSERT OR REPLACE INTO fng_cache (date, value, label, fetched_at)
        VALUES (?, ?, ?, ?)
        """,
        [(e["date"], e["value"], e["label"], now) for e in entries],
    )
    conn.commit()
    conn.close()


# ─────────────────────────────────────────────────────────────────
# FEAR & GREED API
# ─────────────────────────────────────────────────────────────────

def fetch_fng(limit: int = 365) -> list[dict]:
    """
    Alternative.me Fear & Greed Index'ten tarihsel veri çeker.

    Döner: [{"date": "2026-03-03", "value": 28, "label": "Fear"}, ...]
    Son limit gün verisi (varsayılan 365 gün).
    """
    log.info(f"[FnG] Son {limit} günlük veri çekiliyor...")

    try:
        r = requests.get(FNG_API, params={"limit": limit, "format": "json"}, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.error(f"[FnG] API hatası: {e}")
        return []

    entries = []
    for item in data.get("data", []):
        ts = int(item.get("timestamp", 0))
        if ts == 0:
            continue
        date_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        entries.append({
            "date":  date_str,
            "value": int(item.get("value", 0)),
            "label": item.get("value_classification", ""),
        })

    log.info(f"[FnG] {len(entries)} günlük veri noktası alındı")
    return entries


# ─────────────────────────────────────────────────────────────────
# FNG MOMENTUM HESAPLAMA
# ─────────────────────────────────────────────────────────────────

def compute_fng_momentum(
    cache: dict[str, tuple[int, str]],
    signal_date: str,
    lookback_days: int,
) -> float | None:
    """
    FnG ivmesini hesaplar: sinyal günündeki değer / son N günün ortalaması.

    Formül: fear_greed[t] / mean(fear_greed[t-N .. t-1])
      > 1.0  → duygu son N güne göre iyileşiyor (korku azalıyor)
      < 1.0  → duygu son N güne göre kötüleşiyor (korku artıyor)
      = 1.0  → yatay, değişim yok

    Args:
        cache:         {date: (value, label)} önbelleği
        signal_date:   "YYYY-MM-DD"
        lookback_days: kaç günlük ortalama (7 veya 14)

    Döner: momentum float veya None (yeterli veri yoksa)
    """
    try:
        sig_dt = datetime.strptime(signal_date, "%Y-%m-%d").date()
    except ValueError:
        return None

    # Sinyal günündeki değer
    if signal_date not in cache:
        return None
    current_value = cache[signal_date][0]
    if current_value is None:
        return None

    # Lookback penceresi: [sig_dt - lookback_days, sig_dt - 1]
    # (sinyal günü dahil edilmez — look-ahead bias önlemi)
    window_values = []
    for i in range(1, lookback_days + 1):
        d = (sig_dt - timedelta(days=i)).strftime("%Y-%m-%d")
        if d in cache and cache[d][0] is not None:
            window_values.append(cache[d][0])

    if not window_values:
        return None

    avg = sum(window_values) / len(window_values)
    return round(current_value / (avg + 1e-6), 4)


# ─────────────────────────────────────────────────────────────────
# SİNYALLERİ GÜNCELLE
# ─────────────────────────────────────────────────────────────────

def update_signals_with_fng(db_path: str, cache: dict[str, tuple[int, str]]):
    """
    signal_backtest satırlarını FnG değerleri ve momentum'larıyla günceller.

    Güncellenen kolonlar:
      fear_greed       — sinyal günündeki ham değer (0-100)
      fear_greed_label — metin etiketi
      fng_momentum_7d  — 7 günlük ivme
      fng_momentum_14d — 14 günlük ivme
    """
    conn = sqlite3.connect(db_path, timeout=30)

    # NULL olan sinyalleri al (fear_greed NULL → tüm FnG alanları eksik)
    rows = conn.execute(
        "SELECT id, signal_ts FROM signal_backtest WHERE fear_greed IS NULL"
    ).fetchall()

    # Momentum kolonları mevcut mu kontrol et (migration henüz çalışmadıysa)
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(signal_backtest)")}
    has_momentum = "fng_momentum_7d" in existing_cols and "fng_momentum_14d" in existing_cols

    updated = 0
    for row_id, signal_ts in rows:
        try:
            signal_date = signal_ts[:10]   # "YYYY-MM-DD"
        except (TypeError, IndexError):
            continue

        if signal_date not in cache:
            continue

        value, label = cache[signal_date]
        mom_7d  = compute_fng_momentum(cache, signal_date, lookback_days=7)
        mom_14d = compute_fng_momentum(cache, signal_date, lookback_days=14)

        if has_momentum:
            conn.execute(
                """UPDATE signal_backtest
                   SET fear_greed = ?, fear_greed_label = ?,
                       fng_momentum_7d = ?, fng_momentum_14d = ?
                   WHERE id = ?""",
                (value, label, mom_7d, mom_14d, row_id),
            )
        else:
            conn.execute(
                "UPDATE signal_backtest SET fear_greed = ?, fear_greed_label = ? WHERE id = ?",
                (value, label, row_id),
            )
        updated += 1

    conn.commit()
    conn.close()
    log.info(f"[Update] {updated} sinyal FnG + momentum değerleriyle güncellendi")


def update_fng_momentum_only(db_path: str, cache: dict[str, tuple[int, str]]):
    """
    fear_greed dolu ama fng_momentum_* NULL olan sinyalleri günceller.
    Mevcut veri setine geriye dönük momentum doldurmak için kullanılır.
    """
    conn = sqlite3.connect(db_path, timeout=30)

    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(signal_backtest)")}
    if "fng_momentum_7d" not in existing_cols:
        log.warning("fng_momentum_7d kolonu yok — önce migration çalıştırın")
        conn.close()
        return

    rows = conn.execute(
        """SELECT id, signal_ts FROM signal_backtest
           WHERE fear_greed IS NOT NULL AND fng_momentum_7d IS NULL"""
    ).fetchall()

    updated = 0
    for row_id, signal_ts in rows:
        try:
            signal_date = signal_ts[:10]
        except (TypeError, IndexError):
            continue

        mom_7d  = compute_fng_momentum(cache, signal_date, lookback_days=7)
        mom_14d = compute_fng_momentum(cache, signal_date, lookback_days=14)

        conn.execute(
            """UPDATE signal_backtest
               SET fng_momentum_7d = ?, fng_momentum_14d = ?
               WHERE id = ?""",
            (mom_7d, mom_14d, row_id),
        )
        updated += 1

    conn.commit()
    conn.close()
    log.info(f"[Momentum] {updated} sinyal için fng_momentum_7d/14d güncellendi")


# ─────────────────────────────────────────────────────────────────
# ANA PIPELINE
# ─────────────────────────────────────────────────────────────────

def run(bt_db: str = BT_DB_PATH, force: bool = False, dry_run: bool = False):
    """
    Fear & Greed zenginleştirme pipeline'ını çalıştırır.
    """
    if not dry_run:
        init_context_tables(bt_db)

    # Mevcut önbellek
    cache = load_fng_cache(bt_db) if not dry_run else {}

    # Sinyal tarih aralığını bul
    try:
        conn_uri = f"file:{bt_db}?mode=ro" if dry_run else bt_db
        conn_kw  = {"uri": True} if dry_run else {}
        conn = sqlite3.connect(conn_uri, timeout=30, **conn_kw)
        date_range = conn.execute(
            "SELECT MIN(signal_ts), MAX(signal_ts) FROM signal_backtest"
        ).fetchone()
        null_count = conn.execute(
            "SELECT COUNT(*) FROM signal_backtest WHERE fear_greed IS NULL"
        ).fetchone()[0] if not dry_run else "?"
        conn.close()
    except Exception as e:
        log.error(f"DB okuma hatası: {e}")
        return

    if not date_range or not date_range[0]:
        log.warning("signal_backtest boş.")
        return

    start_date = date_range[0][:10]
    end_date   = date_range[1][:10]

    if dry_run:
        print(f"\n── Dry-run planı ──")
        print(f"Sinyal aralığı : {start_date} → {end_date}")
        print(f"Önbellekte     : {len(cache)} gün")
        print(f"API isteği     : 1 adet (son 365 gün)")
        return

    # Momentum kolonları için NULL sayısı (geriye dönük doldurma kontrolü)
    try:
        conn_m = sqlite3.connect(bt_db, timeout=30)
        existing_cols = {r[1] for r in conn_m.execute("PRAGMA table_info(signal_backtest)")}
        momentum_null = conn_m.execute(
            "SELECT COUNT(*) FROM signal_backtest WHERE fear_greed IS NOT NULL AND fng_momentum_7d IS NULL"
        ).fetchone()[0] if "fng_momentum_7d" in existing_cols else -1
        conn_m.close()
    except Exception:
        momentum_null = 0

    # Veri çek (eğer önbellekte eksik gün varsa veya force)
    if force or null_count > 0:
        entries = fetch_fng(limit=365)
        if entries:
            save_fng_cache(bt_db, entries)
            cache = load_fng_cache(bt_db)
            log.info(f"Önbellek güncellendi: {len(cache)} gün")
        else:
            log.warning("FnG verisi alınamadı, mevcut önbellekle devam ediliyor")
        # Yeni sinyallere fear_greed + momentum yaz
        update_signals_with_fng(bt_db, cache)
    else:
        log.info("Tüm sinyallerde FnG verisi zaten dolu.")

    # Geriye dönük momentum doldurma:
    # fear_greed dolu ama fng_momentum_* NULL olan eski kayıtlar
    if momentum_null != 0:
        if not cache:
            cache = load_fng_cache(bt_db)
        log.info(f"Geriye dönük momentum doldurma: {momentum_null} sinyal...")
        update_fng_momentum_only(bt_db, cache)

    # Özet
    conn = sqlite3.connect(bt_db, timeout=30)
    total = conn.execute("SELECT COUNT(*) FROM signal_backtest").fetchone()[0]
    filled_fng = conn.execute(
        "SELECT COUNT(*) FROM signal_backtest WHERE fear_greed IS NOT NULL"
    ).fetchone()[0]
    filled_mom = conn.execute(
        "SELECT COUNT(*) FROM signal_backtest WHERE fng_momentum_7d IS NOT NULL"
    ).fetchone()[0] if "fng_momentum_7d" in existing_cols else 0
    conn.close()

    pct_fng = filled_fng / max(total, 1) * 100
    pct_mom = filled_mom / max(total, 1) * 100
    log.info(f"fear_greed    : {filled_fng}/{total} (%{pct_fng:.1f})")
    log.info(f"fng_momentum  : {filled_mom}/{total} (%{pct_mom:.1f})")


# ─────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Backtest sinyallerini Fear & Greed Index ile zenginleştirir."
    )
    parser.add_argument("--bt-db", default=BT_DB_PATH)
    parser.add_argument("--force", action="store_true",
                        help="Önbelleği yoksay, API'den yeniden çek")
    parser.add_argument("--dry-run", action="store_true",
                        help="Sadece planı göster")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    run(bt_db=args.bt_db, force=args.force, dry_run=args.dry_run)

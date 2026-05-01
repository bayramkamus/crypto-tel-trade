#!/usr/bin/env python3
"""
Trend Verisi Zenginleştirici — backtest_results.db
====================================================
Google Trends'ten geçmiş trend verisi çekerek signal_backtest
tablosunu zenginleştirir.

Çalışma mantığı:
  1. signal_backtest'teki benzersiz ticker'ları toplar
  2. Her ticker için sinyal tarih aralığını bulur
  3. trend_cache tablosundan önce kontrol eder (resume desteği)
  4. Eksik verileri Google Trends'ten çeker (rate-limited)
  5. signal_backtest'e trend_score ve trend_momentum yazar

Eklenen kolonlar:
  trend_score      REAL  — sinyal tarihindeki Trends değeri (0-100)
  trend_momentum   REAL  — trend_score / 30 günlük ort. (>1 yükselen, <1 azalan)

Kullanım:
    python backtest_trends.py
    python backtest_trends.py --bt-db backtest_results.db --dry-run
    python backtest_trends.py --force   # önbelleği yoksay, hepsini yeniden çek

Gereksinim:
    pip install pytrends
"""

import time
import random
import sqlite3
import argparse
import logging
from datetime import datetime, timezone, timedelta
from collections import defaultdict

# ── urllib3 >= 2.0 uyumluluk yamasi ──────────────────────────────────────────
# pytrends, Retry() icinde eski 'method_whitelist' parametresini kullaniyor.
# urllib3 2.0'da bu parametre 'allowed_methods' olarak yeniden adlandirildi.
try:
    from urllib3.util.retry import Retry as _Retry
    _orig_retry_init = _Retry.__init__

    def _patched_retry_init(self, *args, **kwargs):
        if "method_whitelist" in kwargs:
            kwargs["allowed_methods"] = kwargs.pop("method_whitelist")
        _orig_retry_init(self, *args, **kwargs)

    _Retry.__init__ = _patched_retry_init
except Exception:
    pass
# ─────────────────────────────────────────────────────────────────────────────

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

# pytrends rate-limit parametreleri
_KW_DELAY      = (8, 18)   # Keyword'ler arası rastgele bekleme aralığı

# Sinyal tarihinden önce/sonra kaç gün trend penceresi genişletilsin
_WINDOW_BEFORE_DAYS = 30   # 30 gün öncesi: trend_momentum hesabı için
_WINDOW_AFTER_DAYS  = 3    # 3 gün sonrası: buffer

# Cache kaç gün sonra "eski" sayılsın (tekrar çekilsin)
_STALE_DAYS = 7

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
}

# ─────────────────────────────────────────────────────────────────
# pytrends import (opsiyonel bağımlılık)
# ─────────────────────────────────────────────────────────────────

try:
    from pytrends.request import TrendReq
    PYTRENDS_OK = True
except ImportError:
    PYTRENDS_OK = False
    log.warning("pytrends bulunamadı. Kurmak için: pip install pytrends")


# ─────────────────────────────────────────────────────────────────
# VERİTABANI — schema & migration
# ─────────────────────────────────────────────────────────────────

def init_trend_tables(db_path: str = BT_DB_PATH):
    """
    trend_cache tablosunu oluşturur ve signal_backtest'e
    trend_score / trend_momentum kolonlarını ekler.
    """
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    # Trend önbellek tablosu
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trend_cache (
            ticker       TEXT NOT NULL,
            date         TEXT NOT NULL,   -- YYYY-MM-DD
            trend_value  INTEGER,          -- 0-100 Google Trends değeri
            keyword      TEXT,
            window_start TEXT,
            window_end   TEXT,
            fetched_at   TEXT,
            PRIMARY KEY (ticker, date)
        )
    """)

    # signal_backtest'e yeni kolon migration (tablo varsa)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "signal_backtest" in tables:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(signal_backtest)")}
        for col, typedef in [("trend_score", "REAL"), ("trend_momentum", "REAL")]:
            if col not in existing:
                conn.execute(f"ALTER TABLE signal_backtest ADD COLUMN {col} {typedef}")
                log.info(f"Migration: signal_backtest.{col} eklendi")

    conn.commit()
    conn.close()


# ─────────────────────────────────────────────────────────────────
# TREND CACHE — okuma & yazma
# ─────────────────────────────────────────────────────────────────

def load_cache(db_path: str, ticker: str) -> dict[str, int]:
    """Bir ticker için mevcut önbelleği {date: value} şeklinde döner."""
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT date, trend_value FROM trend_cache WHERE ticker = ?",
        (ticker,)
    ).fetchall()
    conn.close()
    return {row[0]: row[1] for row in rows if row[1] is not None}


def save_cache(db_path: str, ticker: str,
               points: list[dict],
               window_start: str, window_end: str):
    """
    Fetch edilen trend noktalarını önbelleğe yazar.
    points: [{"date": "2026-02-08", "value": 42, "keyword": "BTC coin"}, ...]
    """
    conn = sqlite3.connect(db_path)
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        """
        INSERT OR REPLACE INTO trend_cache
            (ticker, date, trend_value, keyword, window_start, window_end, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (ticker, p["date"], p["value"], p["keyword"], window_start, window_end, now)
            for p in points
        ],
    )
    conn.commit()
    conn.close()


def mark_ticker_fetched(db_path: str, ticker: str,
                        window_start: str, window_end: str):
    """
    Boş sonuç dönen ticker'lar için placeholder yazar
    (ikinci çalışmada gereksiz istek atmamak için).
    Ayrıca signal_backtest'te bu ticker'ın trend_score'unu 0 olarak
    işaretler (sentinel değer), böylece NULL olarak takılı kalmaz.
    """
    conn = sqlite3.connect(db_path)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT OR IGNORE INTO trend_cache
            (ticker, date, trend_value, keyword, window_start, window_end, fetched_at)
        VALUES (?, ?, NULL, 'EMPTY', ?, ?, ?)
        """,
        (ticker, window_start, window_start, window_end, now),
    )
    # EMPTY ticker'lar için signal_backtest'te sentinel değer yaz
    # Böylece NULL olarak kalmaz, analiz sırasında 0 olarak ayrışır
    conn.execute(
        """
        UPDATE signal_backtest
        SET trend_score = 0.0, trend_momentum = 0.0
        WHERE ticker = ? AND trend_score IS NULL
        """,
        (ticker,),
    )
    log.info(f"[EMPTY] {ticker}: Google Trends verisi yok, sentinel (0) yazıldı")
    conn.commit()
    conn.close()


def is_empty_ticker(db_path: str, ticker: str) -> bool:
    """
    Bu ticker daha önce Google Trends'ten boş sonuç almış mı?
    (keyword='EMPTY' placeholder'ı var mı)
    """
    try:
        conn = sqlite3.connect(db_path)
        count = conn.execute(
            "SELECT COUNT(*) FROM trend_cache "
            "WHERE ticker = ? AND keyword = 'EMPTY'",
            (ticker,),
        ).fetchone()[0]
        conn.close()
        return count > 0
    except sqlite3.OperationalError:
        return False


def get_missing_date_range(db_path: str, ticker: str) -> tuple[str | None, str | None]:
    """
    Bu ticker için cache'te eksik olan tarih aralığını döner.

    Döner:
      (fetch_start, fetch_end) — sadece eksik kısım
      (None, None) — fetch gerekmiyorsa

    Mantık:
      1. signal_backtest'te NULL trend_score yoksa → gerek yok
      2. Cache boşsa → tüm pencere gerekli
      3. Cache varsa → sadece cache_max'tan sonrasını çek (artımlı)
    """
    try:
        conn = sqlite3.connect(db_path)

        # 1. NULL var mı?
        null_count = conn.execute(
            "SELECT COUNT(*) FROM signal_backtest "
            "WHERE ticker = ? AND trend_score IS NULL",
            (ticker,),
        ).fetchone()[0]

        if null_count == 0:
            conn.close()
            return None, None

        # 2. NULL olan sinyallerin tarih aralığı
        null_range = conn.execute(
            "SELECT MIN(SUBSTR(signal_ts,1,10)), MAX(SUBSTR(signal_ts,1,10)) "
            "FROM signal_backtest WHERE ticker = ? AND trend_score IS NULL",
            (ticker,),
        ).fetchone()

        if not null_range or not null_range[0]:
            conn.close()
            return None, None

        null_min, null_max = null_range

        # 3. Cache'teki mevcut aralık (sadece gerçek değerler)
        cache_range = conn.execute(
            "SELECT MIN(date), MAX(date) FROM trend_cache "
            "WHERE ticker = ? AND trend_value IS NOT NULL",
            (ticker,),
        ).fetchone()
        conn.close()

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        if not cache_range or not cache_range[0]:
            # Cache tamamen boş → tüm pencere gerekli
            start_dt = datetime.strptime(null_min, "%Y-%m-%d") - timedelta(days=_WINDOW_BEFORE_DAYS)
            end_dt = datetime.strptime(null_max, "%Y-%m-%d") + timedelta(days=_WINDOW_AFTER_DAYS)
            end_str = min(end_dt.strftime("%Y-%m-%d"), today)
            return start_dt.strftime("%Y-%m-%d"), end_str

        cache_min, cache_max = cache_range

        # 4. NULL tarihler cache aralığında mı?
        if null_min >= cache_min and null_max <= cache_max:
            # Tüm NULL tarihler cache içinde — cache→signal güncelleme yetecek
            return None, None

        # 5. Artımlı fetch: cache_max'tan sonrasını çek
        #    (veya cache_min'den önce NULL varsa orayı da)
        fetch_start = cache_max  # son bilinen günden devam et
        if null_min < cache_min:
            # Cache'in öncesinde de NULL var — daha erken başla
            fetch_start = (datetime.strptime(null_min, "%Y-%m-%d")
                           - timedelta(days=_WINDOW_BEFORE_DAYS)).strftime("%Y-%m-%d")

        end_dt = datetime.strptime(null_max, "%Y-%m-%d") + timedelta(days=_WINDOW_AFTER_DAYS)
        fetch_end = min(end_dt.strftime("%Y-%m-%d"), today)

        return fetch_start, fetch_end

    except sqlite3.OperationalError:
        return None, None


# ─────────────────────────────────────────────────────────────────
# GOOGLE TRENDS FETCH
# ─────────────────────────────────────────────────────────────────

def _keyword_for(ticker: str) -> str:
    """Ticker sembolünden arama anahtar kelimesi üretir."""
    return f"{ticker.upper()} crypto"


_RATE_LIMITED = "__RATE_LIMITED__"


def fetch_trends(ticker: str, window_start: str, window_end: str) -> list[dict] | str:
    """
    Google Trends'ten belirtilen tarih aralığı için günlük veri çeker.

    Args:
        ticker:       Coin sembolü (örn. "BTC")
        window_start: "YYYY-MM-DD"
        window_end:   "YYYY-MM-DD"

    Döner:
        list[dict] — başarılı: [{"date": ..., "value": ..., "keyword": ...}, ...]
        list[]     — gerçekten veri yok (boş sonuç)
        _RATE_LIMITED — Google rate limit engeline takıldı, döngü kırılmalı
    """
    if not PYTRENDS_OK:
        return []

    keyword = _keyword_for(ticker)
    timeframe = f"{window_start} {window_end}"

    log.info(f"[Trends] {ticker}: '{keyword}' → {timeframe}")

    try:
        pytrends = TrendReq(
            hl="en-US",
            tz=0,
            timeout=(10, 60),
            retries=2,
            backoff_factor=1.5,
            requests_args={"headers": _HEADERS},
        )
        pytrends.build_payload(
            kw_list=[keyword],
            timeframe=timeframe,
            geo="",
            gprop="",
        )
        df = pytrends.interest_over_time()
    except Exception as e:
        err = str(e)
        if "429" in err or "Too Many" in err or "response" in err.lower():
            log.warning(
                f"[Trends] {ticker} rate limit — "
                f"kalan ticker'lar sonraki çalışmaya bırakılıyor."
            )
            return _RATE_LIMITED
        else:
            log.error(f"[Trends] {ticker} hatası: {e}")
            return []

    if df is None or df.empty:
        log.warning(f"[Trends] {ticker}: boş veri döndü.")
        return []

    if "isPartial" in df.columns:
        df = df.drop(columns=["isPartial"])

    points = []
    for ts, row in df.iterrows():
        date_str = ts.strftime("%Y-%m-%d")
        val = int(row[keyword]) if keyword in row else None
        if val is not None:
            points.append({"date": date_str, "value": val, "keyword": keyword})

    log.info(f"[Trends] {ticker}: {len(points)} günlük veri noktası")
    return points


# ─────────────────────────────────────────────────────────────────
# HESAPLAMA — trend_score & trend_momentum
# ─────────────────────────────────────────────────────────────────

def compute_trend_metrics(
    cache: dict[str, int],
    signal_date: str,        # "YYYY-MM-DD"
    lookback_days: int = 30,
) -> tuple[float | None, float | None]:
    """
    trend_score ve trend_momentum hesaplar.

    trend_score:    sinyal tarihindeki (veya en yakın) Trends değeri (0-100)
    trend_momentum: trend_score / (son {lookback_days} günlük ort + ε)
                    >1.2 = belirgin yükseliş, <0.8 = ilgi azalıyor

    Döner: (trend_score, trend_momentum)
    """
    if not cache:
        return None, None

    # Sinyal tarihinde veya ±3 gün içinde en yakın veri noktasını bul
    sig_dt = datetime.strptime(signal_date, "%Y-%m-%d").date()
    best_date = None
    best_diff = 99999
    for d in cache:
        try:
            cd = datetime.strptime(d, "%Y-%m-%d").date()
            diff = abs((cd - sig_dt).days)
            if diff < best_diff:
                best_diff = diff
                best_date = d
        except ValueError:
            continue

    if best_date is None or best_diff > 3:
        return None, None

    trend_score = float(cache[best_date])

    # Lookback window: signal_date - lookback_days → signal_date
    window_start_dt = sig_dt - timedelta(days=lookback_days)
    lookback_values = [
        v for d, v in cache.items()
        if window_start_dt <= datetime.strptime(d, "%Y-%m-%d").date() <= sig_dt
    ]

    if lookback_values:
        avg = sum(lookback_values) / len(lookback_values)
        trend_momentum = trend_score / (avg + 1e-6)
    else:
        trend_momentum = None

    return trend_score, trend_momentum


# ─────────────────────────────────────────────────────────────────
# SİNYALLERI GÜNCELLE
# ─────────────────────────────────────────────────────────────────

def update_signals_with_trends(db_path: str, ticker: str, cache: dict[str, int]):
    """
    Verilen ticker'ın tüm sinyallerini trend verileriyle günceller.
    """
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT id, signal_ts FROM signal_backtest WHERE ticker = ?",
        (ticker,),
    ).fetchall()

    updated = 0
    for row_id, signal_ts in rows:
        # signal_ts örnek: "2026-02-08T15:54:22+00:00"
        try:
            signal_date = signal_ts[:10]  # "YYYY-MM-DD"
        except (TypeError, IndexError):
            continue

        trend_score, trend_momentum = compute_trend_metrics(cache, signal_date)

        conn.execute(
            """
            UPDATE signal_backtest
            SET trend_score = ?, trend_momentum = ?
            WHERE id = ?
            """,
            (trend_score, trend_momentum, row_id),
        )
        updated += 1

    conn.commit()
    conn.close()
    log.info(f"[Update] {ticker}: {updated} sinyal güncellendi")


# ─────────────────────────────────────────────────────────────────
# ANA PIPELINE
# ─────────────────────────────────────────────────────────────────

def _is_cache_stale(db_path: str, ticker: str) -> bool:
    """
    Bu ticker'ın cache'i _STALE_DAYS'den eski mi?
    fetched_at tarihine bakarak kontrol eder.
    """
    try:
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT MAX(fetched_at) FROM trend_cache "
            "WHERE ticker = ? AND trend_value IS NOT NULL",
            (ticker,),
        ).fetchone()
        conn.close()

        if not row or not row[0]:
            return True

        fetched = datetime.fromisoformat(row[0])
        age = datetime.now(timezone.utc) - fetched
        return age.days >= _STALE_DAYS
    except (sqlite3.OperationalError, ValueError):
        return True


def run(bt_db: str = BT_DB_PATH, force: bool = False, dry_run: bool = False):
    """
    Trend zenginleştirme pipeline'ını çalıştırır.

    Çalışma mantığı (v2):
      1. Benzersiz ticker'ları topla
      2. EMPTY tickerlar → sentinel (0) yaz, API çağrısı yapma
      3. Cache'ten mevcut verilerle signal güncellemesi dene
      4. Hâlâ NULL kalan sinyaller için artımlı (incremental) fetch
      5. Stale cache kontrolü: _STALE_DAYS'den eski cache → yenile

    Args:
        bt_db:   backtest_results.db yolu
        force:   True ise önbelleği yoksay, hepsini yeniden çek
        dry_run: True ise sadece plan çıkar, istek atmaz
    """
    if not PYTRENDS_OK and not dry_run:
        log.error("pytrends yüklü değil. Kurun: pip install pytrends")
        return

    if not dry_run:
        init_trend_tables(bt_db)

    # ── 1. Benzersiz ticker'ları ve tarih aralıklarını topla ──
    open_kwargs = {"uri": True} if dry_run else {}
    bt_db_uri   = f"file:{bt_db}?mode=ro" if dry_run else bt_db
    conn = sqlite3.connect(bt_db_uri, timeout=30, **open_kwargs)
    rows = conn.execute(
        "SELECT ticker, MIN(signal_ts), MAX(signal_ts) FROM signal_backtest GROUP BY ticker"
    ).fetchall()
    conn.close()

    if not rows:
        log.warning("signal_backtest tablosunda veri yok.")
        return

    log.info(f"Toplam {len(rows)} benzersiz ticker bulundu.")

    # ── 2. EMPTY tickerlar → sentinel yaz, listeyi filtrele ──
    active_rows = []
    empty_count = 0
    for ticker, min_ts, max_ts in rows:
        if not force and is_empty_ticker(bt_db, ticker):
            if not dry_run:
                # Sentinel değerleri yaz (henüz yazılmamışsa)
                mark_ticker_fetched(bt_db, ticker,
                                    min_ts[:10], max_ts[:10])
            empty_count += 1
        else:
            active_rows.append((ticker, min_ts, max_ts))

    if empty_count:
        log.info(f"[EMPTY] {empty_count} ticker Google Trends verisi olmayan "
                 f"→ sentinel (0) yazıldı, atlanıyor")

    # ── 3. Cache'ten mevcut verilerle signal güncelle ──
    if not dry_run:
        log.info("Mevcut cache'ten signal güncellemesi deneniyor...")
        for ticker, _, _ in active_rows:
            cache = load_cache(bt_db, ticker)
            if cache:
                update_signals_with_trends(bt_db, ticker, cache)

    # ── 4. Artımlı fetch planı hazırla ──
    tasks_to_fetch = []
    tasks_stale    = []
    tasks_ok       = []

    for ticker, min_ts, max_ts in active_rows:
        # Artımlı: sadece eksik aralığı hesapla
        fetch_start, fetch_end = get_missing_date_range(bt_db, ticker)

        if force:
            # Force: tüm pencereyi çek
            start_dt = datetime.strptime(min_ts[:10], "%Y-%m-%d") - timedelta(days=_WINDOW_BEFORE_DAYS)
            end_dt   = datetime.strptime(max_ts[:10], "%Y-%m-%d") + timedelta(days=_WINDOW_AFTER_DAYS)
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            fetch_start = start_dt.strftime("%Y-%m-%d")
            fetch_end   = min(end_dt.strftime("%Y-%m-%d"), today)
            tasks_to_fetch.append({
                "ticker": ticker, "fetch_start": fetch_start,
                "fetch_end": fetch_end, "reason": "force",
            })
        elif fetch_start and fetch_end:
            tasks_to_fetch.append({
                "ticker": ticker, "fetch_start": fetch_start,
                "fetch_end": fetch_end, "reason": "missing_data",
            })
        elif _is_cache_stale(bt_db, ticker):
            # Cache var ama eski — yenilemek gerekebilir
            cache_max_row = None
            try:
                conn2 = sqlite3.connect(bt_db)
                cache_max_row = conn2.execute(
                    "SELECT MAX(date) FROM trend_cache "
                    "WHERE ticker = ? AND trend_value IS NOT NULL",
                    (ticker,),
                ).fetchone()
                conn2.close()
            except sqlite3.OperationalError:
                pass

            if cache_max_row and cache_max_row[0]:
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                tasks_stale.append({
                    "ticker": ticker,
                    "fetch_start": cache_max_row[0],
                    "fetch_end": today,
                    "reason": "stale_cache",
                })
        else:
            tasks_ok.append(ticker)

    # Stale olanları da fetch listesine ekle (öncelik: missing > stale)
    all_to_fetch = tasks_to_fetch + tasks_stale

    log.info(
        f"Plan: {len(tasks_ok)} tamam | "
        f"{len(tasks_to_fetch)} eksik veri | "
        f"{len(tasks_stale)} stale cache | "
        f"{empty_count} empty"
    )

    rate_limited = False   # fetch döngüsü atlanırsa da tanımlı olsun

    if dry_run:
        print(f"\n── Dry-run planı ──")
        print(f"Tamam (skip)     : {len(tasks_ok)} ticker")
        print(f"EMPTY (sentinel) : {empty_count} ticker")
        print(f"Eksik veri fetch : {len(tasks_to_fetch)} ticker")
        print(f"Stale cache      : {len(tasks_stale)} ticker")
        if all_to_fetch:
            est_sec = len(all_to_fetch) * 13
            print(f"Tahmini süre     : ~{est_sec // 60} dk {est_sec % 60} sn")
            print(f"\nFetch edilecekler:")
            for t in all_to_fetch:
                print(f"  {t['ticker']:<12} {t['fetch_start']} → {t['fetch_end']}  ({t['reason']})")
        return

    # ── 5. Fetch ──
    rate_limited = False
    fetched_count = 0

    for i, task in enumerate(all_to_fetch):
        ticker      = task["ticker"]
        fetch_start = task["fetch_start"]
        fetch_end   = task["fetch_end"]

        log.info(f"[{i + 1}/{len(all_to_fetch)}] {ticker} çekiliyor "
                 f"({task['reason']}: {fetch_start} → {fetch_end})...")

        result = fetch_trends(ticker, fetch_start, fetch_end)

        # Rate limit → döngüyü kır, kalan ticker'lar NULL kalır
        if result == _RATE_LIMITED:
            remaining = len(all_to_fetch) - i
            log.warning(
                f"[Trends] Rate limit! {fetched_count} ticker başarılı, "
                f"{remaining} ticker sonraki çalışmaya bırakıldı (NULL olarak kalacak)."
            )
            rate_limited = True
            break

        if result:
            save_cache(bt_db, ticker, result, fetch_start, fetch_end)
            fetched_count += 1
        else:
            # Gerçekten veri yok (boş sonuç) → EMPTY olarak işaretle
            if task["reason"] == "missing_data":
                mark_ticker_fetched(bt_db, ticker, fetch_start, fetch_end)
            fetched_count += 1

        # Son ticker değilse bekle
        if i < len(all_to_fetch) - 1:
            delay = random.uniform(*_KW_DELAY)
            log.debug(f"  {delay:.1f}s bekleniyor...")
            time.sleep(delay)

    # ── 6. Tüm ticker'lar için signal_backtest güncelle ──
    log.info("signal_backtest trend değerleriyle güncelleniyor...")
    for ticker, _, _ in active_rows:
        cache = load_cache(bt_db, ticker)
        if cache:
            update_signals_with_trends(bt_db, ticker, cache)

    # ── 7. Özet ──
    conn = sqlite3.connect(bt_db)
    total = conn.execute("SELECT COUNT(*) FROM signal_backtest").fetchone()[0]
    filled = conn.execute(
        "SELECT COUNT(*) FROM signal_backtest WHERE trend_score IS NOT NULL"
    ).fetchone()[0]
    still_null = total - filled
    conn.close()

    pct = filled / max(total, 1) * 100
    log.info(f"Tamamlandı: {filled}/{total} sinyal trend verisiyle dolduruldu (%{pct:.1f})")
    if still_null > 0:
        if rate_limited:
            log.info(
                f"Kalan NULL: {still_null} sinyal — Google rate limit nedeniyle bırakıldı. "
                f"Sonraki çalışmada otomatik devam edecek."
            )
        else:
            log.info(f"Kalan NULL: {still_null} sinyal (muhtemelen yeni coinler — sonraki çalışmada tamamlanır)")


# ─────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Backtest sinyallerini Google Trends verisiyle zenginleştirir."
    )
    parser.add_argument(
        "--bt-db", default=BT_DB_PATH,
        help=f"backtest_results.db yolu (varsayılan: {BT_DB_PATH})"
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Önbelleği yoksay, hepsini yeniden çek"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Sadece planı göster, istek atma"
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Debug log seviyesi"
    )
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    run(bt_db=args.bt_db, force=args.force, dry_run=args.dry_run)

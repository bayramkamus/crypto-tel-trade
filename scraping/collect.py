"""
Scraping Toplu Islemci (collect.py)
=====================================
Elimizdeki coinler icin tum scraping islemlerini sirayla calistirir.

Akis:
  1. Coin listesini al (pump_research.db'den veya --coins argumani)
  2. Her coin icin:
       a. CoinResolver  → kimlik + market cap
       b. SourceResolver → resmi kaynaklar (twitter, telegram, website, blog)
       c. NewsCollector  → CryptoPanic haberleri
       d. EventsCollector→ Telegram / Nitter / website duyurulari
       e. TrendsCollector→ Google Trends 7 gunluk veri
  3. Opsiyonel: --classify ile AI siniflandirma gecisi

Kullanim:
  python -m scraping.collect
  python -m scraping.collect --coins PEPE,NEAR,AVAX
  python -m scraping.collect --db my.db --out-db scraping_data.db --classify
  python -m scraping.collect --skip-news --skip-trends --classify
"""

import argparse
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# .env dosyasini otomatik yukle (proje koku veya calisma dizini)
try:
    from dotenv import load_dotenv
    _env_file = Path(__file__).parent.parent / ".env"
    if _env_file.exists():
        load_dotenv(_env_file)
    else:
        load_dotenv()  # calisma dizinindeki .env
except ImportError:
    pass  # python-dotenv yoksa env degiskenleri sistem tarafindan set edilmeli

# Scraping paket modullerini import et
from scraping import db as scrapdb
from scraping import coin_resolver, source_resolver, dedup
from scraping.ticker_parser import extract_ticker
from scraping.collectors import news as news_col
from scraping.collectors import events as events_col
from scraping.collectors import trends as trends_col
from scraping.ai_classify import classify_batch

# ─────────────────────────────────────────────────────────────────
# LOGGING AYARI
# ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("collect")


# ─────────────────────────────────────────────────────────────────
# COIN LİSTESİ
# ─────────────────────────────────────────────────────────────────


def get_tickers_from_db(src_db: str) -> list[str]:
    """
    pump_research.db'deki mesajlardan benzersiz ticker listesini ceker.

    Oncelik sirasi:
      1. backtest_results.db'deki signal_backtest tablosu (ticker kolonu var)
      2. pump_research.db'deki message_text'ten regex ile cikarim
    """
    if not Path(src_db).exists():
        log.error(f"[collect] Kaynak DB bulunamadi: {src_db}")
        return []

    # 1. backtest_results.db varsa oradan al (daha temiz veri)
    bt_db = Path(src_db).parent / "backtest_results.db"
    if bt_db.exists():
        try:
            conn = sqlite3.connect(str(bt_db))
            cur = conn.execute("""
                SELECT DISTINCT UPPER(TRIM(ticker))
                FROM signal_backtest
                WHERE ticker IS NOT NULL AND TRIM(ticker) != ''
                ORDER BY 1
            """)
            tickers = [row[0] for row in cur.fetchall()]
            conn.close()
            if tickers:
                log.info(f"[collect] backtest_results.db'den {len(tickers)} benzersiz ticker.")
                return tickers
        except sqlite3.OperationalError:
            pass

    # 2. message_text'ten regex ile cikar
    log.info("[collect] Ticker'lar message_text'ten cikariliyor...")
    conn = sqlite3.connect(src_db)
    try:
        cur = conn.execute(
            "SELECT message_text FROM messages WHERE message_text IS NOT NULL"
        )
        rows = cur.fetchall()
    except sqlite3.OperationalError as e:
        log.error(f"[collect] messages tablosu okunamadi: {e}")
        conn.close()
        return []
    conn.close()

    found = set()
    for (msg,) in rows:
        t = extract_ticker(msg)
        if t:
            found.add(t)

    tickers = sorted(found)
    log.info(f"[collect] {len(rows)} mesajdan {len(tickers)} benzersiz ticker cikarildi.")
    return tickers


# ─────────────────────────────────────────────────────────────────
# CANLI TELEGRAM MESAJI KAYDETME
# ─────────────────────────────────────────────────────────────────

def store_telegram_message(
    ticker: str,
    text: str,
    msg_url: str,
    out_db: str,
) -> int | None:
    """
    Canlı Telegram mesajını scraping DB'ye yazar.
    Coin çözümler, raw_content tablosuna kaydeder.

    Doner: content_id (yeni kayit) veya None (duplicate / coin cozumlenemedi)
    """
    coin = coin_resolver.resolve(ticker)
    if not coin:
        log.debug(f"[store_telegram] {ticker} cozumlenemedi, atlaniyor.")
        return None

    coin_id = scrapdb.upsert_coin(out_db, coin)
    hashes = dedup.compute_hashes(url=msg_url, title=text[:120])
    item = {
        "data_type":    "telegram_live",
        "source_name":  "telegram/live_collector",
        "url":          msg_url,
        "title":        text[:120].replace("\n", " "),
        "body":         text,
        "url_hash":     hashes["url_hash"],
        "title_hash":   hashes["title_hash"],
        "published_at": datetime.now(timezone.utc).isoformat(),
    }
    result = scrapdb.save_content(out_db, coin_id, item)
    if result:
        log.info(f"  → [scraping] {ticker} yazdildi (content_id={result})")
    return result


# ─────────────────────────────────────────────────────────────────
# İÇERİK KAYDETME (hash ekleyerek)
# ─────────────────────────────────────────────────────────────────

def _save_items(out_db: str, coin_id: int, items: list[dict]) -> int:
    """
    Collector'dan gelen ham itemlara dedup hash'leri ekler ve kaydeder.
    Doner: kaydedilen yeni kayit sayisi
    """
    saved = 0
    for item in items:
        # Hash hesapla (yoksa ekle)
        if "url_hash" not in item or "title_hash" not in item:
            hashes = dedup.compute_hashes(
                item.get("url", ""),
                item.get("title", ""),
            )
            item.update(hashes)

        result = scrapdb.save_content(out_db, coin_id, item)
        if result is not None:
            saved += 1

    return saved


# ─────────────────────────────────────────────────────────────────
# TEK COİN İŞLEMİ
# ─────────────────────────────────────────────────────────────────

def process_coin(
    symbol: str,
    out_db: str,
    cfg: dict,
) -> dict:
    """
    Tek bir coin icin tum scraping adimlarini calistirir.

    cfg dict anahtarlari:
      skip_news     (bool)
      skip_events   (bool)
      skip_trends   (bool)
      cryptopanic_token (str|None)
      telegram_api_id   (int|None)
      telegram_api_hash (str|None)
      news_pages    (int)

    Doner: ozet dict
    """
    result = {
        "symbol":    symbol,
        "resolved":  False,
        "coin_id":   None,
        "news":      0,
        "events":    0,
        "trends":    0,
        "error":     None,
    }

    # ── 1. Coin Resolver ──────────────────────────────────────────
    # store_telegram_message bu coin'i daha önce çözmüş olabilir;
    # önce DB'ye bak, yoksa API'ye git.
    existing = scrapdb.get_coin_by_symbol(out_db, symbol)
    if existing:
        coin    = existing          # coingecko_id, name vb. mevcut
        coin_id = existing["id"]
        log.info(f"[{symbol}] Coin DB'den alindi (id={coin_id})")
    else:
        log.info(f"[{symbol}] Coin cozumleniyor (CoinGecko)...")
        coin = coin_resolver.resolve(symbol)
        if not coin:
            log.warning(f"[{symbol}] Coin cozumlenemedi, atlaniyor.")
            result["error"] = "resolve_failed"
            return result
        coin_id = scrapdb.upsert_coin(out_db, coin)

    result["resolved"] = True
    result["coin_id"]  = coin_id
    log.info(f"[{symbol}] Coin ID={coin_id}, CoinGecko={coin.get('coingecko_id')}")

    # ── 2. Source Resolver ────────────────────────────────────────
    log.info(f"[{symbol}] Resmi kaynaklar cekiliyor...")
    sources = source_resolver.resolve_sources(coin["coingecko_id"])
    if sources:
        scrapdb.save_sources(out_db, coin_id, sources)
        log.info(
            f"[{symbol}] Kaynaklar: "
            f"twitter={sources.get('twitter')}, "
            f"telegram={sources.get('telegram')}, "
            f"website={sources.get('website')}, "
            f"blog={sources.get('blog')}"
        )
    else:
        log.warning(f"[{symbol}] Resmi kaynak bulunamadi.")

    name = coin.get("name") or symbol

    # ── 3. News Collector ─────────────────────────────────────────
    if not cfg.get("skip_news"):
        log.info(f"[{symbol}] Haberler cekiliyor (CryptoPanic)...")
        token = cfg.get("cryptopanic_token")
        try:
            news_items = news_col.fetch(
                symbol=symbol,
                auth_token=token,
                pages=cfg.get("news_pages", 3),
            )
            saved = _save_items(out_db, coin_id, news_items)
            result["news"] = saved
            log.info(f"[{symbol}] {saved}/{len(news_items)} haber kaydedildi.")
        except Exception as e:
            log.error(f"[{symbol}] News hatasi: {e}")

    # ── 4. Events Collector ───────────────────────────────────────
    if not cfg.get("skip_events"):
        log.info(f"[{symbol}] Etkinlikler/duyurular cekiliyor...")
        event_items = []

        # a) Telegram resmi kanalı
        tg_url = sources.get("telegram") if sources else None
        tg_api_id   = cfg.get("telegram_api_id")
        tg_api_hash = cfg.get("telegram_api_hash")

        if tg_url and tg_api_id and tg_api_hash:
            try:
                tg_items = events_col.fetch_telegram(
                    telegram_url=tg_url,
                    api_id=tg_api_id,
                    api_hash=tg_api_hash,
                )
                event_items.extend(tg_items)
                log.info(f"[{symbol}] Telegram: {len(tg_items)} mesaj")
            except Exception as e:
                log.error(f"[{symbol}] Telegram hatasi: {e}")
        elif tg_url:
            log.warning(
                f"[{symbol}] Telegram URL var ama TELEGRAM_API_ID/HASH "
                f"tanimlanmamis, atlaniyor."
            )

        # b) Website / Blog (Playwright + RSS)
        website = sources.get("website") if sources else None
        blog    = sources.get("blog")    if sources else None
        if website or blog:
            try:
                web_items = events_col.fetch_website(
                    website_url=website,
                    blog_url=blog,
                )
                event_items.extend(web_items)
                log.info(f"[{symbol}] Website/Blog: {len(web_items)} icerik")
            except Exception as e:
                log.error(f"[{symbol}] Website hatasi: {e}")

        if event_items:
            saved = _save_items(out_db, coin_id, event_items)
            result["events"] = saved
            log.info(f"[{symbol}] {saved}/{len(event_items)} etkinlik kaydedildi.")

    # ── 5. Trends Collector ───────────────────────────────────────
    if not cfg.get("skip_trends"):
        # Google rate limit: onceki coin'in trends isteklerinden sonra bekleme
        trends_wait = cfg.get("trends_pre_delay", 30)
        log.info(f"[{symbol}] Google Trends cekiliyor ({trends_wait}s on-bekleme)...")
        time.sleep(trends_wait)
        try:
            trend_points = trends_col.fetch(symbol=symbol, name=name)
            if trend_points:
                # keyword'e gore grupla ve kaydet
                kw_groups: dict[str, list] = {}
                for pt in trend_points:
                    kw_groups.setdefault(pt["keyword"], []).append(
                        {"date": pt["date"], "value": pt["value"]}
                    )
                for kw, series in kw_groups.items():
                    scrapdb.save_trends(out_db, coin_id, kw, series)
                result["trends"] = len(trend_points)
                log.info(f"[{symbol}] {len(trend_points)} trends noktasi kaydedildi.")
        except Exception as e:
            log.error(f"[{symbol}] Trends hatasi: {e}")

    return result


# ─────────────────────────────────────────────────────────────────
# AI SINIFLANDIRMA GECİŞİ
# ─────────────────────────────────────────────────────────────────

def run_classify(out_db: str, coin_ids: list[int] = None):
    """
    raw_content tablosundaki siniflandirilmamis icerikleri AI ile siniflandirir.
    Sonuclari raw_content tablosundaki yeni kolonlara yazar.

    NOT: Bu fonksiyon raw_content tablosuna ek kolonlar eklemez;
         is_relevant, content_type vb. kolonlar tabloda yoksa ALTER TABLE yapar.
    """
    conn = scrapdb.get_conn(out_db)

    # Gerekli kolonlari ekle (yoksa)
    classify_columns = [
        ("is_relevant",  "INTEGER"),
        ("content_type", "TEXT"),
        ("sentiment",    "TEXT"),
        ("event_type",   "TEXT"),
        ("event_date",   "TEXT"),
        ("ai_summary",   "TEXT"),
        ("importance",   "TEXT"),
    ]
    for col_name, col_type in classify_columns:
        try:
            conn.execute(
                f"ALTER TABLE raw_content ADD COLUMN {col_name} {col_type}"
            )
        except Exception:
            pass  # Kolon zaten var

    conn.commit()

    # Siniflandirilmamis satirlari cek
    query = """
        SELECT rc.id, rc.coin_id, rc.title, rc.body,
               c.symbol, c.name
        FROM raw_content rc
        JOIN coins c ON rc.coin_id = c.id
        WHERE rc.is_relevant IS NULL
    """
    params = []
    if coin_ids:
        placeholders = ",".join("?" * len(coin_ids))
        query += f" AND rc.coin_id IN ({placeholders})"
        params.extend(coin_ids)

    query += " ORDER BY rc.id"

    rows = conn.execute(query, params).fetchall()
    conn.close()

    if not rows:
        log.info("[classify] Siniflandirilacak icerik yok.")
        return

    log.info(f"[classify] {len(rows)} icerik siniflandirilacak...")

    # Coin'e gore grupla (ayni coin icin batch gonder)
    from collections import defaultdict
    coin_groups: dict = defaultdict(list)
    for row in rows:
        coin_groups[(row["coin_id"], row["symbol"], row["name"])].append(
            {"id": row["id"], "title": row["title"], "body": row["body"]}
        )

    total_classified = 0
    for (coin_id, symbol, name), items in coin_groups.items():
        log.info(f"[classify] {symbol}: {len(items)} icerik...")
        classifications = classify_batch(items, symbol=symbol, name=name)

        # Sonuclari DB'ye yaz
        conn2 = scrapdb.get_conn(out_db)
        for cls in classifications:
            content_id = cls.get("content_id")
            if not content_id:
                continue
            conn2.execute("""
                UPDATE raw_content SET
                    is_relevant  = ?,
                    content_type = ?,
                    sentiment    = ?,
                    event_type   = ?,
                    event_date   = ?,
                    ai_summary   = ?,
                    importance   = ?
                WHERE id = ?
            """, (
                1 if cls.get("is_relevant") else 0,
                cls.get("content_type"),
                cls.get("sentiment"),
                cls.get("event_type"),
                cls.get("event_date"),
                cls.get("summary"),
                cls.get("importance"),
                content_id,
            ))
            total_classified += 1
        conn2.commit()
        conn2.close()

    log.info(f"[classify] Toplam {total_classified} icerik siniflandirildi.")


# ─────────────────────────────────────────────────────────────────
# ANA AKIŞ
# ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Coin bazli scraping toplu islemcisi",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ornekler:
  python -m scraping.collect
  python -m scraping.collect --coins PEPE,NEAR,AVAX
  python -m scraping.collect --classify
  python -m scraping.collect --skip-news --skip-trends
        """,
    )
    parser.add_argument(
        "--coins", type=str, default=None,
        help="Virgülle ayrılmis ticker listesi (örn: PEPE,NEAR). "
             "Belirtilmezse pump_research.db'den okunur.",
    )
    parser.add_argument(
        "--db", type=str, default="pump_research.db",
        help="Kaynak veritabani (pump_research.db). Sadece --coins "
             "belirtilmemisse kullanilir.",
    )
    parser.add_argument(
        "--out-db", type=str, default="scraping_data.db",
        help="Cikti veritabani (varsayilan: scraping_data.db)",
    )
    parser.add_argument(
        "--classify", action="store_true",
        help="Scraping bittikten sonra AI siniflandirma gecisi calistir.",
    )
    parser.add_argument(
        "--classify-only", action="store_true",
        help="Scraping yapmadan sadece AI siniflandirma gecisini calistir.",
    )
    parser.add_argument(
        "--skip-news", action="store_true",
        help="CryptoPanic haber cekimini atla.",
    )
    parser.add_argument(
        "--skip-events", action="store_true",
        help="Event/duyuru cekimini atla (Telegram, Nitter, website).",
    )
    parser.add_argument(
        "--skip-trends", action="store_true",
        help="Google Trends cekimini atla.",
    )
    parser.add_argument(
        "--news-pages", type=int, default=3,
        help="CryptoPanic'ten cekilecek sayfa sayisi (varsayilan: 3).",
    )
    parser.add_argument(
        "--delay", type=float, default=2.0,
        help="Coinler arasi bekleme suresi saniye (varsayilan: 2.0).",
    )
    parser.add_argument(
        "--trends-delay", type=float, default=30.0,
        help="Her coin icin Google Trends oncesi bekleme (varsayilan: 30s).",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="DEBUG seviyesinde log.",
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # ── Cikti DB hazirla ──────────────────────────────────────────
    out_db = args.out_db
    scrapdb.init_db(out_db)

    # ── Sadece classify modunda calis ─────────────────────────────
    if args.classify_only:
        log.info("=== AI Siniflandirma modu ===")
        run_classify(out_db)
        return

    # ── Coin listesi ──────────────────────────────────────────────
    if args.coins:
        symbols = [s.strip().upper() for s in args.coins.split(",") if s.strip()]
        log.info(f"Argumandan {len(symbols)} ticker alindi.")
    else:
        symbols = get_tickers_from_db(args.db)
        if not symbols:
            log.error("Ticker bulunamadi. --coins ile manuel belirtin.")
            sys.exit(1)

    # ── Env degiskenleri ──────────────────────────────────────────
    cfg = {
        "skip_news":          args.skip_news,
        "skip_events":        args.skip_events,
        "skip_trends":        args.skip_trends,
        "news_pages":         args.news_pages,
        "trends_pre_delay":   args.trends_delay,
        "cryptopanic_token":  os.environ.get("CRYPTOPANIC_TOKEN"),
        "telegram_api_id":    _int_env("TELEGRAM_API_ID"),
        "telegram_api_hash":  os.environ.get("TELEGRAM_API_HASH"),
    }

    # Uyarilar
    if not cfg["skip_news"] and not cfg["cryptopanic_token"]:
        log.warning(
            "⚠  CRYPTOPANIC_TOKEN tanimlanmamis. "
            "Haberler auth olmadan cekilecek (sinirli)."
        )
    if not cfg["skip_events"]:
        if not cfg["telegram_api_id"] or not cfg["telegram_api_hash"]:
            log.warning(
                "⚠  TELEGRAM_API_ID / TELEGRAM_API_HASH tanimlanmamis. "
                "Telegram cekimi atlaniyor."
            )

    # ── Ana dongu ─────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  SCRAPING BASLADI  —  {len(symbols)} coin")
    print(f"{'='*60}\n")

    t_start = time.time()
    summaries = []
    processed_coin_ids = []

    for idx, symbol in enumerate(symbols, 1):
        print(f"\n[{idx}/{len(symbols)}] ── {symbol} ──")
        summary = process_coin(symbol, out_db, cfg)
        summaries.append(summary)

        if summary.get("coin_id"):
            processed_coin_ids.append(summary["coin_id"])

        # Coinler arasi bekleme (CoinGecko rate limit)
        if idx < len(symbols):
            time.sleep(args.delay)

    # ── AI Siniflandirma ──────────────────────────────────────────
    if args.classify and processed_coin_ids:
        print(f"\n{'='*60}")
        print("  AI SINIFLANDIRMA GECISI")
        print(f"{'='*60}\n")
        run_classify(out_db, coin_ids=processed_coin_ids)

    # ── Ozet ─────────────────────────────────────────────────────
    elapsed = time.time() - t_start
    _print_summary(summaries, elapsed, out_db)


def _int_env(key: str) -> int | None:
    val = os.environ.get(key)
    try:
        return int(val) if val else None
    except ValueError:
        return None


def _print_summary(summaries: list[dict], elapsed: float, out_db: str):
    resolved  = sum(1 for s in summaries if s["resolved"])
    failed    = sum(1 for s in summaries if not s["resolved"])
    total_news   = sum(s["news"]   for s in summaries)
    total_events = sum(s["events"] for s in summaries)
    total_trends = sum(s["trends"] for s in summaries)

    print(f"\n{'='*60}")
    print("  SCRAPING TAMAMLANDI")
    print(f"{'='*60}")
    print(f"  Toplam coin  : {len(summaries)}")
    print(f"  Cozumlendi   : {resolved}")
    print(f"  Basarisiz    : {failed}")
    print(f"  Haberler     : {total_news}")
    print(f"  Etkinlikler  : {total_events}")
    print(f"  Trends nokt. : {total_trends}")
    print(f"  Sure         : {elapsed:.1f}s")
    print(f"  Cikti DB     : {out_db}")

    if failed:
        print("\n  Basarisiz coinler:")
        for s in summaries:
            if not s["resolved"]:
                print(f"    - {s['symbol']} ({s.get('error', '?')})")

    print(f"{'='*60}\n")


# ─────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    main()

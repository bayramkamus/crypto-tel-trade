#!/usr/bin/env python3
"""
Telegram Canlı Mesaj Toplayıcı
================================
config.py'deki kanallari Telethon NewMessage event'i ile sürekli dinler.
Her gelen mesaj:
  1. pump_research.db'ye yazilir (channels + messages tablolari)
  2. Mesajdaki ticker cikarilir → scraping pipeline'a iletilir
  3. CANLI mesajlarda tam scraping pipeline tetiklenir (async kuyruk)
     → coin_resolver, source_resolver, haberler, events, trends

Kullanim:
    python run_collector.py          # canlı dinleme + backfill
    python run_collector.py --no-backfill   # sadece canli dinleme
"""

import asyncio
import logging
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

from telethon import TelegramClient, events

import config

# ─────────────────────────────────────────────────────────────────
# LIVE PIPELINE SABİTLERİ
# ─────────────────────────────────────────────────────────────────

# Aynı ticker bu süre (saniye) içinde tekrar gelirse pipeline atlanır
LIVE_DEDUP_SECS = 1800   # 30 dakika

# Scraping çıktı DB (scraping.collect ile aynı varsayılan)
SCRAPING_OUT_DB = "scraping_data.db"

# Live modda Google Trends atlanır (her coin ~30s gecikme yaratır)
LIVE_SKIP_TRENDS = True

# ─────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("collector")

# ─────────────────────────────────────────────────────────────────
# DB SCHEMA  (pump_research.db)
# ─────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    channel_id    INTEGER PRIMARY KEY,
    username      TEXT,
    title         TEXT,
    member_count  INTEGER,
    updated_at    TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id    INTEGER,
    channel_name  TEXT,
    message_id    INTEGER,
    timestamp     TEXT,
    message_text  TEXT,
    sender_id     INTEGER,
    reply_to      INTEGER,
    views         INTEGER,
    forwards      INTEGER,
    UNIQUE(channel_id, message_id)
);
"""


def _init_db(db_path: str) -> sqlite3.Connection:
    """DB'yi baslat, tablolari olustur, baglanti don."""
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    conn.commit()
    log.info(f"DB hazir: {db_path}")
    return conn


# ─────────────────────────────────────────────────────────────────
# DB YARDIMCILARI
# ─────────────────────────────────────────────────────────────────

def _upsert_channel(conn: sqlite3.Connection,
                    channel_id: int, username: str,
                    title: str, member_count: int):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("""
        INSERT INTO channels (channel_id, username, title, member_count, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(channel_id) DO UPDATE SET
            username     = excluded.username,
            title        = excluded.title,
            member_count = excluded.member_count,
            updated_at   = excluded.updated_at
    """, (channel_id, username, title, member_count, now))
    conn.commit()


def _insert_message(conn: sqlite3.Connection, msg: dict) -> bool:
    """Mesaji yaz. Daha once yazilmissa False, yeni yazildiysa True."""
    try:
        conn.execute("""
            INSERT INTO messages
                (channel_id, channel_name, message_id, timestamp,
                 message_text, sender_id, reply_to, views, forwards)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            msg["channel_id"], msg["channel_name"], msg["message_id"],
            msg["timestamp"], msg["message_text"], msg.get("sender_id"),
            msg.get("reply_to"), msg.get("views"), msg.get("forwards"),
        ))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False  # duplicate


def _get_last_message_ts(conn: sqlite3.Connection, channel_id: int) -> datetime | None:
    """
    Belirli bir kanal için DB'deki en son mesajın timestamp'ini döner.
    Kayıt yoksa None döner.
    """
    c = conn.execute(
        "SELECT MAX(timestamp) FROM messages WHERE channel_id = ?",
        (channel_id,)
    )
    row = c.fetchone()
    if row and row[0]:
        try:
            ts_str = row[0].replace("Z", "+00:00")
            return datetime.fromisoformat(ts_str)
        except (ValueError, TypeError):
            pass
    return None


# ─────────────────────────────────────────────────────────────────
# SCRAPING PIPELINE ENTEGRASYONU
# ─────────────────────────────────────────────────────────────────

_scraping_available = False
_pipeline_available = False

try:
    from scraping import db as scrapdb
    from scraping.ticker_parser import extract_ticker
    _scraping_available = True
except ImportError:
    log.warning("scraping paketi bulunamadi — sadece DB'ye yazma modu.")
    extract_ticker = None  # type: ignore

try:
    from scraping.collect import process_coin, store_telegram_message
    _pipeline_available = True
except ImportError:
    log.warning("scraping.collect import edilemedi — tam pipeline devre disi.")

# Volume anomali
_volume_available = False
try:
    from exchanges import fetch_volume_anomaly
    _volume_available = True
except ImportError:
    log.warning("exchanges modülü bulunamadı — volume anomali devre disi.")

# Anlık rapor + email
_report_available = False
try:
    from live_report import send_signal_report
    _report_available = True
except ImportError:
    log.warning("live_report modülü bulunamadı — email rapor devre disi.")

# Canli karar modeli + indikator snapshot
_decision_available = False
try:
    from live_decision import analyze_live_signal
    _decision_available = True
except ImportError as e:
    log.warning(f"live_decision modulu bulunamadi — karar analizi devre disi: {e}")


# Canli 15m chart pattern modeli
_chart_pattern_available = False
_chart_pattern_enabled = os.environ.get("ENABLE_CHART_PATTERN", "true").lower() not in {
    "0", "false", "no", "off"
}
try:
    if _chart_pattern_enabled:
        from chart_pattern_live import analyze_chart_pattern
        _chart_pattern_available = True
    else:
        log.info("chart pattern devre disi (ENABLE_CHART_PATTERN=false).")
except ImportError as e:
    log.warning(f"chart_pattern_live modulu bulunamadi - chart pattern devre disi: {e}")


def _forward_to_scraping(ticker: str, text: str, msg_url: str):
    """
    Cikarilan ticker'i scraping pipeline'a iletir.
    Tüm yazma mantığı scraping.collect.store_telegram_message'a devredilir.
    """
    if not _pipeline_available:
        return
    try:
        store_telegram_message(ticker, text, msg_url, SCRAPING_OUT_DB)
    except Exception as e:
        log.error(f"[scraping] {ticker} isleme hatasi: {e}")


# ─────────────────────────────────────────────────────────────────
# ANA COLLECTOR SINIFI
# ─────────────────────────────────────────────────────────────────

class LiveCollector:
    """
    Telethon ile kanallari canli dinler.
    Her yeni mesaj pump_research.db'ye yazilir ve
    scraping pipeline'a iletilir.

    Canli mesajlarda tam scraping pipeline async kuyruk ile tetiklenir:
      Telegram → ticker → kuyruk → worker → process_coin()
                                             (haberler, events, trends)

    relay_mode=True olduğunda:
      - Yerel SQLite'a YAZMAZ
      - Event'leri relay API'ye HTTP POST ile gönderir
      - Scraping pipeline tetiklenmez (sunucuda ağır iş yapılmaz)
    """

    def __init__(self, db_path: str = None, do_backfill: bool = True,
                 forward_scraping: bool = True,
                 relay_mode: bool = False,
                 relay_url: str = None,
                 relay_token: str = None,
                 relay_batch_size: int = None):
        self.db_path = db_path or config.DB_PATH
        self.do_backfill = do_backfill
        self.forward_scraping = forward_scraping
        self.relay_mode = relay_mode

        # ── Relay modu ────────────────────────────────────────────
        self._relay_client = None
        self._relay_buffer: list[dict] = []   # batch gönderim için tampon
        self._relay_flush_size = relay_batch_size or 50
        self._relay_flush_task = None
        self._relay_retry_count = 0           # ardışık hata sayacı
        self._relay_max_retries = 5           # max ardışık hata sonrası buffer dump

        if self.relay_mode:
            if not relay_url or not relay_token:
                raise ValueError(
                    "Relay modu için RELAY_BASE_URL ve RELAY_TOKEN gerekli"
                )
            from relay.client import RelayClient
            self._relay_client = RelayClient(
                base_url=relay_url, token=relay_token
            )
            log.info(f"RELAY MODU aktif → {relay_url}")
            # Relay modda yerel DB oluşturma (sunucuda DB tutulmayacak)
            self.conn = None
        else:
            self.conn = _init_db(self.db_path)

        if _scraping_available and not self.relay_mode:
            scrapdb.init_db(SCRAPING_OUT_DB)

        # Telethon creates a .session file after first login.
        # Treat it like a password and do not share it.
        self.client = TelegramClient(
            config.SESSION, config.API_ID, config.API_HASH
        )
        self._live_count = 0

        # ── Tam pipeline için async kuyruk ────────────────────────
        # Her eleman: dict (ticker, channel, message_text, timestamp)
        self._ticker_queue: asyncio.Queue = asyncio.Queue()

        # Dedup: {ticker: son_islenme_datetime}
        # Aynı ticker LIVE_DEDUP_SECS içinde tekrar gelirse atlanır
        self._dedup: dict[str, datetime] = {}

        # Worker task referansı (graceful shutdown için)
        self._worker_task: asyncio.Task | None = None

        # Thread pool — process_coin() blocking çağrılar için
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pipeline")

    async def start(self):
        """Client'i baslat, event handler kaydet, gerekirse backfill yap."""
        await self.client.start()
        me = await self.client.get_me()
        log.info(f"Telegram oturumu acildi: {me.first_name} (id={me.id})")

        # Relay modda health check
        if self.relay_mode and self._relay_client:
            health = await self._relay_client.health_check()
            if health.get("status") == "ok":
                log.info(f"Relay API bağlantısı OK — buffer: {health.get('buffer_events', 0)} event")
            else:
                log.warning(f"Relay API sağlık kontrolü başarısız: {health}")

        # Kanallari cozumle ve DB'ye kaydet
        resolved = await self._resolve_channels()
        if not resolved:
            log.error("Hicbir kanal cozumlenemedi. Cikiliyor.")
            return

        # Backfill (gecmis mesajlar)
        if self.do_backfill:
            await self._backfill(resolved)

        # Relay modda periyodik flush task'ı başlat
        if self.relay_mode:
            self._relay_flush_task = asyncio.create_task(
                self._relay_periodic_flush(), name="relay-flush"
            )

        # Tam pipeline worker'ı başlat (sadece yerel modda)
        if not self.relay_mode and _pipeline_available and self.forward_scraping:
            self._worker_task = asyncio.create_task(
                self._scraping_worker(), name="scraping-worker"
            )
            log.info("  ✓ Scraping pipeline worker baslatildi")
        elif not self.relay_mode and not _pipeline_available:
            log.warning("  ⚠  scraping.collect yok — tam pipeline devre disi")

        # Canlı dinleme event handler
        chat_entities = [ent for ent, _ in resolved.values()]
        @self.client.on(events.NewMessage(chats=chat_entities))
        async def _on_new_message(event):
            await self._handle_message(event.message, event.chat, live=True)

        mode_str = "RELAY" if self.relay_mode else "YEREL"
        log.info("")
        log.info("═" * 55)
        log.info(f"  CANLI DINLEME AKTIF [{mode_str} MOD]")
        log.info(f"  {len(resolved)} kanal izleniyor")
        if not self.relay_mode:
            pipeline_status = "AÇIK" if _pipeline_available else "KAPALI"
            log.info(f"  Tam scraping pipeline: {pipeline_status}")
        else:
            log.info("  Event'ler relay API'ye gönderiliyor")
        log.info("  Durdurmak icin Ctrl+C")
        log.info("═" * 55)
        log.info("")

        # Sonsuza dek calis
        await self.client.run_until_disconnected()

    async def _resolve_channels(self) -> dict:
        """
        config.CHANNELS listesindeki kanallari Telethon ile cozumler.
        Doner: {channel_id: (entity, username_str)}
        """
        resolved = {}
        for ch_name in config.CHANNELS:
            try:
                entity = await self.client.get_entity(ch_name)
                ch_id = config.normalize_channel_id(entity.id)
                title = getattr(entity, 'title', ch_name)
                members = getattr(entity, 'participants_count', None) or 0

                if self.conn:
                    _upsert_channel(self.conn, ch_id, ch_name, title, members)
                resolved[ch_id] = (entity, ch_name)

                log.info(f"  ✓ @{ch_name:30s}  id={ch_id}  «{title}»  "
                         f"[{members:,} uye]")
            except Exception as e:
                log.warning(f"  ✗ @{ch_name}: {e}")

        log.info(f"\n{len(resolved)}/{len(config.CHANNELS)} kanal cozumlendi.\n")
        return resolved

    async def _backfill(self, resolved: dict):
        """
        Her kanal icin eksik mesajlari tamamlar.

        Akıllı backfill:
          - Kanal DB'de varsa → son mesaj tarihinden itibaren çeker
          - Kanal DB'de yoksa → BACKFILL_DAYS günlük tam çekim yapar
          - Zaten mevcut mesajlara dokunmaz, gereksiz API çağrısı yapmaz

        Relay modda: tüm backfill mesajları relay API'ye gönderilir,
        dedup relay tarafında yapılır.
        """
        max_cutoff = datetime.now(timezone.utc) - timedelta(days=config.BACKFILL_DAYS)
        total_new = 0
        total_skipped = 0

        log.info(f"Akıllı backfill basliyor (max {config.BACKFILL_DAYS} gün)...")

        for ch_id, (entity, ch_name) in resolved.items():
            count = 0

            # Relay modda yerel DB yok → her zaman tam backfill
            if self.relay_mode:
                last_ts = None
            else:
                # Bu kanal için DB'deki en son mesaj tarihini bul
                last_ts = _get_last_message_ts(self.conn, ch_id)

            if last_ts:
                # Kanal zaten DB'de — sadece eksik günleri çek
                cutoff = last_ts
                gap_hours = (datetime.now(timezone.utc) - last_ts).total_seconds() / 3600

                if gap_hours < 0.5:
                    # Son mesaj 30 dakikadan yeni — bu kanal güncel
                    log.info(f"  @{ch_name}: güncel (son mesaj {gap_hours:.1f} saat önce), atlandi")
                    total_skipped += 1
                    continue

                gap_display = f"{gap_hours:.1f} saat" if gap_hours < 48 else f"{gap_hours/24:.1f} gün"
                log.info(f"  @{ch_name}: {gap_display} boşluk tespit edildi, tamamlaniyor...")
            else:
                # Bu kanal ilk kez — tam backfill
                cutoff = max_cutoff
                log.info(f"  @{ch_name}: ilk kez — {config.BACKFILL_DAYS} günlük tam çekim")

            try:
                async for msg in self.client.iter_messages(
                    entity, limit=config.BACKFILL_BATCH_SIZE * 10
                ):
                    msg_date = msg.date
                    if msg_date and msg_date.tzinfo is None:
                        msg_date = msg_date.replace(tzinfo=timezone.utc)

                    if msg_date and msg_date < cutoff:
                        break
                    if not msg.text:
                        continue

                    msg_dict = self._msg_to_dict(msg, ch_id, ch_name)

                    if self.relay_mode:
                        # Relay modda: ticker çıkar ve buffer'a ekle
                        ticker = extract_ticker(msg.text) if extract_ticker else None
                        relay_event = dict(msg_dict)
                        relay_event["extracted_ticker"] = ticker
                        self._relay_buffer.append(relay_event)
                        count += 1
                        # Periyodik flush
                        if len(self._relay_buffer) >= self._relay_flush_size:
                            await self._relay_flush()
                    else:
                        if _insert_message(self.conn, msg_dict):
                            count += 1

            except Exception as e:
                log.error(f"[backfill] @{ch_name} hata: {e}")

            # Relay modda kalan buffer'ı flush et
            if self.relay_mode and self._relay_buffer:
                await self._relay_flush()

            total_new += count
            if count > 0:
                dest = "relay'e gönderildi" if self.relay_mode else "yeni mesaj eklendi"
                log.info(f"  @{ch_name}: {count} {dest}")
            else:
                log.info(f"  @{ch_name}: yeni mesaj yok")

        log.info(
            f"\nBackfill tamamlandi: {total_new} yeni mesaj | "
            f"{total_skipped} kanal zaten güncel\n"
        )

    async def _handle_message(self, msg, chat, live: bool = False):
        """Tek bir mesaji isler: DB'ye yaz + scraping'e ilet."""
        if not msg.text:
            return

        ch_name = getattr(chat, 'username', None) or str(chat.id)
        ch_id = config.normalize_channel_id(chat.id)

        msg_dict = self._msg_to_dict(msg, ch_id, ch_name)

        # ── Ticker çıkar (her iki modda da lazım) ────────────────
        ticker = None
        if extract_ticker:
            ticker = extract_ticker(msg.text)

        prefix = "🔴 LIVE" if live else "📦 BACKFILL"
        short_text = msg.text[:80].replace("\n", " ")

        # ── RELAY MODU: event'i buffer'a ekle, API'ye gönder ────
        if self.relay_mode:
            relay_event = dict(msg_dict)
            relay_event["extracted_ticker"] = ticker
            self._relay_buffer.append(relay_event)
            self._live_count += 1
            log.info(f"[{prefix}→RELAY] @{ch_name} | #{msg.id} | {short_text}")

            # Flush boyutuna ulaştıysa hemen gönder
            if len(self._relay_buffer) >= self._relay_flush_size:
                await self._relay_flush()
            return

        # ── YEREL MOD: eski davranış ────────────────────────────
        is_new = _insert_message(self.conn, msg_dict)

        if is_new:
            self._live_count += 1
            log.info(f"[{prefix}] @{ch_name} | #{msg.id} | {short_text}")

            if self.forward_scraping and ticker:
                msg_url = f"https://t.me/{ch_name}/{msg.id}"

                # 1. Telegram mesajını raw_content'e hemen kaydet
                _forward_to_scraping(ticker, msg.text, msg_url)

                # 2. Sadece CANLI mesajlarda tam pipeline'ı kuyruğa ekle
                if live and _pipeline_available:
                    await self._enqueue_ticker(
                        ticker=ticker,
                        channel=ch_name,
                        message_text=msg.text,
                        timestamp=msg_dict["timestamp"],
                    )

    async def _enqueue_ticker(self, ticker: str, channel: str,
                              message_text: str, timestamp: str):
        """
        Ticker'i tam pipeline kuyruğuna ekler.
        Dedup: aynı ticker LIVE_DEDUP_SECS içinde tekrar gelirse atlanır.
        """
        now = datetime.now(timezone.utc)
        last = self._dedup.get(ticker)
        if last is not None:
            elapsed = (now - last).total_seconds()
            if elapsed < LIVE_DEDUP_SECS:
                remaining = int(LIVE_DEDUP_SECS - elapsed)
                log.debug(
                    f"  [pipeline] {ticker} dedup — "
                    f"{remaining}s sonra tekrar islenir"
                )
                return

        self._dedup[ticker] = now
        job = {
            "ticker":       ticker,
            "channel":      channel,
            "message_text": message_text,
            "timestamp":    timestamp,
        }
        await self._ticker_queue.put(job)
        qsize = self._ticker_queue.qsize()
        log.info(f"  → [pipeline] {ticker} kuyruğa eklendi (kuyruk: {qsize})")

    async def _scraping_worker(self):
        """
        Arka planda çalışan scraping worker.
        Kuyruktan job alır → 4 adımlı pipeline çalıştırır:
          1. process_coin()       → haberler, events, trends
          2. fetch_volume_anomaly → volume ratio hesapla
          3. analyze_live_signal  → karar + indikatör snapshot
          4. send_signal_report   → email ile rapor gönder
        """
        loop = asyncio.get_event_loop()

        # Scraping için gerekli config
        cfg = {
            "skip_news":          False,
            "skip_events":        False,
            "skip_trends":        LIVE_SKIP_TRENDS,
            "news_pages":         2,
            "trends_pre_delay":   0,
            "cryptopanic_token":  os.environ.get("CRYPTOPANIC_TOKEN"),
            "telegram_api_id":    config.API_ID,
            "telegram_api_hash":  config.API_HASH,
        }

        log.info("[pipeline] Worker hazir — ticker bekleniyor...")

        while True:
            job = await self._ticker_queue.get()
            ticker       = job["ticker"]
            channel      = job["channel"]
            message_text = job["message_text"]
            timestamp    = job["timestamp"]

            log.info(f"[pipeline] ── {ticker} işleniyor ──")
            t_start = datetime.now(timezone.utc)

            scraping_result = None
            volume_data = None
            decision_data = None
            chart_pattern_data = None

            try:
                # ── ADIM 1: Tam scraping pipeline ─────────────────
                log.info(f"[pipeline] {ticker} → scraping başlıyor...")
                scraping_result = await loop.run_in_executor(
                    self._executor,
                    process_coin,
                    ticker,
                    SCRAPING_OUT_DB,
                    cfg,
                )
                log.info(
                    f"[pipeline] {ticker} scraping OK — "
                    f"haber={scraping_result.get('news', 0)}  "
                    f"event={scraping_result.get('events', 0)}  "
                    f"trends={scraping_result.get('trends', 0)}"
                )

                # ── ADIM 2: Volume anomali kontrolü ───────────────
                if _volume_available:
                    log.info(f"[pipeline] {ticker} → volume kontrol...")
                    volume_data = await loop.run_in_executor(
                        self._executor,
                        fetch_volume_anomaly,
                        ticker,
                    )
                    if volume_data:
                        ratio = volume_data.get("volume_ratio", 0)
                        tag = "⚠️ ANOMALİ" if volume_data.get("is_anomaly") else "normal"
                        log.info(
                            f"[pipeline] {ticker} volume={ratio}x "
                            f"({volume_data.get('exchange', '?')}) [{tag}]"
                        )

                # ── ADIM 3: Karar modeli + indikatör snapshot ──────
                if _decision_available:
                    log.info(f"[pipeline] {ticker} → karar analizi...")
                    decision_data = await loop.run_in_executor(
                        self._executor,
                        analyze_live_signal,
                        ticker,
                        message_text,
                        scraping_result,
                        volume_data,
                        timestamp,
                    )
                    if decision_data:
                        log.info(
                            f"[pipeline] {ticker} karar={decision_data.get('action')} "
                            f"güven={decision_data.get('confidence', '-')}"
                        )

                # ── ADIM 4a: 15m chart pattern (izole) ────────────
                # Bu adim hata atsa bile email gonderilebilmeli.
                if _chart_pattern_available:
                    log.info(f"[pipeline] {ticker} -> 15m chart pattern...")
                    try:
                        chart_pattern_data = await loop.run_in_executor(
                            self._executor,
                            analyze_chart_pattern,
                            ticker,
                            timestamp,
                            volume_data,
                        )
                        if chart_pattern_data:
                            log.info(
                                f"[pipeline] {ticker} chart={chart_pattern_data.get('action')} "
                                f"pattern={chart_pattern_data.get('detected_pattern', '-')}"
                            )
                    except Exception as chart_exc:
                        log.error(
                            f"[pipeline] {ticker} chart pattern hata "
                            f"(email yine de gonderilecek): {chart_exc}",
                            exc_info=True,
                        )
                        chart_pattern_data = {
                            "status": "unavailable",
                            "ticker": ticker,
                            "action": "TUT",
                            "reason": f"Chart pattern adimi crashledi: {chart_exc}",
                        }

                # ── ADIM 4b: Anlık rapor + email (izole) ──────────
                # Email gonderimi de kendi try/except'ine alindi ki
                # SMTP/HTML hatalari pipeline'i kirmasin.
                if _report_available:
                    log.info(f"[pipeline] {ticker} → rapor gönderiliyor...")
                    try:
                        await loop.run_in_executor(
                            self._executor,
                            send_signal_report,
                            ticker,
                            channel,
                            message_text,
                            scraping_result,
                            volume_data,
                            timestamp,
                            decision_data,
                            chart_pattern_data,
                        )
                    except Exception as mail_exc:
                        log.error(
                            f"[pipeline] {ticker} email gonderim hatasi: {mail_exc}",
                            exc_info=True,
                        )

                # ── Özet ──────────────────────────────────────────
                elapsed = (datetime.now(timezone.utc) - t_start).total_seconds()
                log.info(f"[pipeline] ✅ {ticker} tamamlandi ({elapsed:.1f}s)")

            except asyncio.CancelledError:
                log.info("[pipeline] Worker durduruldu.")
                self._ticker_queue.task_done()
                return
            except Exception as e:
                log.error(f"[pipeline] {ticker} hata: {e}", exc_info=True)
            finally:
                self._ticker_queue.task_done()

    # ─────────────────────────────────────────────────────────────
    # RELAY FLUSH
    # ─────────────────────────────────────────────────────────────

    async def _relay_flush(self):
        """
        Buffer'daki event'leri relay API'ye gönderir.
        Başarısızlıkta buffer'a geri koyar ve retry sayacını artırır.
        Ardışık hata sayacı max'a ulaşırsa loglar ve buffer'ı korur (veri kaybı yok).
        """
        if not self._relay_buffer or not self._relay_client:
            return

        batch = self._relay_buffer[:]
        self._relay_buffer.clear()

        try:
            result = await self._relay_client.send_events(batch)
            accepted = result.get("accepted", 0)
            duplicates = result.get("duplicates", 0)
            self._relay_retry_count = 0  # başarılı → sıfırla

            log.info(
                f"[relay-flush] {accepted} kabul, "
                f"{duplicates} duplicate "
                f"({len(batch)} event gönderildi)"
            )
        except Exception as e:
            self._relay_retry_count += 1
            self._relay_buffer = batch + self._relay_buffer
            buf_size = len(self._relay_buffer)

            if self._relay_retry_count >= self._relay_max_retries:
                log.error(
                    f"[relay-flush] {self._relay_retry_count} ardışık hata! "
                    f"Buffer'da {buf_size} event bekliyor. "
                    f"Relay sunucu erişilebilir mi? Hata: {e}"
                )
            else:
                log.warning(
                    f"[relay-flush] Gönderim hatası (deneme {self._relay_retry_count}/"
                    f"{self._relay_max_retries}): {e} — "
                    f"{len(batch)} event buffer'a geri eklendi "
                    f"(toplam: {buf_size})"
                )

    async def _relay_periodic_flush(self):
        """
        Periyodik flush — buffer'da az event varken de gönderir.
        Normal: 10 saniyede bir. Hata durumunda: backoff ile 30 saniyeye kadar artar.
        """
        while True:
            try:
                # Hata durumunda backoff
                if self._relay_retry_count > 0:
                    wait = min(30, 10 + self._relay_retry_count * 5)
                else:
                    wait = 10
                await asyncio.sleep(wait)

                if self._relay_buffer:
                    await self._relay_flush()
            except asyncio.CancelledError:
                # Son flush — kapanmadan önce kalan event'leri gönder
                if self._relay_buffer:
                    await self._relay_flush()
                return
            except Exception as e:
                log.error(f"[relay-flush] Periyodik flush hatası: {e}")

    def _msg_to_dict(self, msg, ch_id: int, ch_name: str) -> dict:
        """Telethon mesajini dict'e donustur."""
        ts = msg.date
        if ts and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        return {
            "channel_id":   ch_id,
            "channel_name": ch_name,
            "message_id":   msg.id,
            "timestamp":    ts.isoformat() if ts else None,
            "message_text": msg.text,
            "sender_id":    getattr(msg, 'sender_id', None),
            "reply_to":     msg.reply_to.reply_to_msg_id if msg.reply_to else None,
            "views":        getattr(msg, 'views', None),
            "forwards":     getattr(msg, 'forwards', None),
        }

    def stop(self):
        """Graceful shutdown."""
        log.info(f"\nKapatiliyor... ({self._live_count} canli mesaj islendi)")

        # Relay flush task'ı durdur
        if self._relay_flush_task and not self._relay_flush_task.done():
            self._relay_flush_task.cancel()
            log.info("[relay] Flush task iptal edildi.")

        # Worker task'ı durdur
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            log.info("[pipeline] Worker task iptal edildi.")

        # Thread pool'u kapat
        self._executor.shutdown(wait=False)

        if self.conn:
            self.conn.close()
        self.client.disconnect()

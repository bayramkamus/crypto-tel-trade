"""
Events Collector
=================
Resmi kaynaklardan duyuru / event toplar.

2 alt kaynak (source-driven):
  1. Telegram  — resmi announcement kanallarindan Telethon ile mesajlar
  2. Website   — Playwright (headless browser) ile JS-rendered sayfalari dahil
                 RSS feed varsa once onu dener, yoksa browser ile scrape eder
"""

import re
import time
import logging
import asyncio
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse, urljoin

try:
    from bs4 import BeautifulSoup
    BS4_OK = True
except ImportError:
    BS4_OK = False
    print("[WARN] beautifulsoup4 bulunamadi: pip install beautifulsoup4")

try:
    from telethon import TelegramClient
    from telethon.tl.types import Channel
    TELETHON_OK = True
except ImportError:
    TELETHON_OK = False
    print("[WARN] telethon bulunamadi: pip install telethon")

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_OK = True
except ImportError:
    PLAYWRIGHT_OK = False
    print("[WARN] playwright bulunamadi: pip install playwright && playwright install chromium")

import requests
from scraping.dedup import compute_hashes

log = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
_HEADERS = {"User-Agent": _USER_AGENT}
_TIMEOUT = 15


# ─────────────────────────────────────────────────────────────────
# 1. TELEGRAM COLLECTOR
# ─────────────────────────────────────────────────────────────────

def fetch_telegram(telegram_url: str, api_id: int, api_hash: str,
                   session_name: str = "pump_research",
                   days_back: int = 7, limit: int = 50) -> list[dict]:
    """
    Resmi Telegram announcement kanalinden mesajlari ceker.
    telegram_url ornek: "https://t.me/pepecoinann"
    """
    if not TELETHON_OK:
        log.warning("[EventsCollector/Telegram] telethon yuklu degil.")
        return []

    if not telegram_url:
        return []

    channel_handle = telegram_url.rstrip("/").split("/")[-1]

    try:
        return asyncio.run(
            _fetch_telegram_async(
                channel_handle, api_id, api_hash,
                session_name, days_back, limit
            )
        )
    except Exception as e:
        log.error(f"[EventsCollector/Telegram] Hata ({channel_handle}): {e}")
        return []


async def _fetch_telegram_async(channel_handle, api_id, api_hash,
                                 session_name, days_back, limit):
    results = []
    cutoff  = datetime.now(timezone.utc) - timedelta(days=days_back)

    async with TelegramClient(session_name, api_id, api_hash) as client:
        try:
            entity = await client.get_entity(channel_handle)
        except Exception as e:
            log.error(f"[Telegram] Kanal bulunamadi ({channel_handle}): {e}")
            return []

        async for msg in client.iter_messages(entity, limit=limit):
            if not msg.date or msg.date < cutoff:
                break
            if not msg.text:
                continue

            url = f"https://t.me/{channel_handle}/{msg.id}"
            hashes = compute_hashes(url=url, title=msg.text[:120])

            results.append({
                "data_type":    "event",
                "source_name":  f"telegram/{channel_handle}",
                "url":          url,
                "title":        msg.text[:120].replace("\n", " "),
                "body":         msg.text,
                "url_hash":     hashes["url_hash"],
                "title_hash":   hashes["title_hash"],
                "published_at": msg.date.isoformat(),
            })

    log.info(f"[EventsCollector/Telegram] {channel_handle}: {len(results)} mesaj")
    return results


# ─────────────────────────────────────────────────────────────────
# 2. WEBSITE / BLOG COLLECTOR  (Playwright + RSS fallback)
# ─────────────────────────────────────────────────────────────────

def fetch_website(website_url: str, blog_url: str = None) -> list[dict]:
    """
    Resmi web sitesinden/blog'dan icerik ceker.

    Strateji:
      1. RSS feed dene (requests ile yeterli, JS gerektirmez)
      2. RSS bulunamazsa Playwright ile sayfayi render edip DOM'dan icerik cek
      3. Playwright yoksa requests ile fallback (eski yontem)
    """
    results = []
    targets = []

    if blog_url:
        targets.append(("blog", blog_url))
    if website_url:
        targets.append(("website", website_url))

    for source_label, url in targets:
        # 1. Once RSS dene (hafif, JS gerektirmez)
        rss_results = _try_rss(url, source_label)
        if rss_results:
            results.extend(rss_results)
            log.info(f"[EventsCollector/Website] RSS bulundu: {url} ({len(rss_results)} icerik)")
            continue

        # 2. Playwright ile browser scraping
        if PLAYWRIGHT_OK:
            pw_results = _scrape_with_playwright(url, source_label)
            if pw_results:
                results.extend(pw_results)
                continue

        # 3. Fallback: requests + BeautifulSoup (SPA'larda calismaz)
        if BS4_OK:
            html_results = _scrape_html_fallback(url, source_label)
            results.extend(html_results)

    log.info(f"[EventsCollector/Website] Toplam {len(results)} icerik bulundu.")
    return results


# ─────────────────────────────────────────────────────────────────
# RSS
# ─────────────────────────────────────────────────────────────────

def _try_rss(base_url: str, source_label: str) -> list[dict]:
    """Yaygin RSS URL yollarini dener."""
    rss_paths = [
        "/feed", "/rss", "/feed.xml", "/rss.xml", "/atom.xml",
        "/blog/feed", "/blog/rss", "/blog/feed.xml",
        "/news/feed", "/news/rss",
    ]
    for path in rss_paths:
        rss_url = base_url.rstrip("/") + path
        items = _parse_rss(rss_url, source_label)
        if items:
            return items
    return []


def _parse_rss(rss_url: str, source_label: str) -> list[dict]:
    try:
        r = requests.get(rss_url, headers=_HEADERS, timeout=_TIMEOUT)
        if r.status_code != 200:
            return []

        # Content-type kontrolu: XML olmayan cevaplari atla
        ct = r.headers.get("content-type", "")
        if "xml" not in ct and "rss" not in ct and "atom" not in ct:
            # Bazi siteler XML dondurur ama content-type text/html der
            if not r.text.strip().startswith("<?xml") and "<rss" not in r.text[:200]:
                return []

        if not BS4_OK:
            return []

        soup = BeautifulSoup(r.text, "xml")
        items = soup.find_all("item") or soup.find_all("entry")
        if not items:
            return []

        results = []
        for item in items[:20]:
            title = (item.find("title") or {})
            title = title.get_text(strip=True) if hasattr(title, "get_text") else ""
            url   = None
            for tag in ["link", "guid"]:
                el = item.find(tag)
                if el:
                    url = el.get_text(strip=True) or el.get("href")
                    if url and url.startswith("http"):
                        break

            body = None
            for tag in ["description", "summary", "content"]:
                el = item.find(tag)
                if el:
                    raw_text = el.get_text()
                    if BS4_OK:
                        body = BeautifulSoup(raw_text, "html.parser").get_text()
                    break

            pub = None
            for tag in ["pubDate", "published", "updated"]:
                el = item.find(tag)
                if el:
                    pub = _normalize_date(el.get_text(strip=True))
                    break

            if not title and not url:
                continue

            hashes = compute_hashes(url=url, title=title)
            results.append({
                "data_type":    "event",
                "source_name":  source_label,
                "url":          url,
                "title":        title,
                "body":         body,
                "url_hash":     hashes["url_hash"],
                "title_hash":   hashes["title_hash"],
                "published_at": pub,
            })

        return results

    except Exception as e:
        log.debug(f"[RSS] {rss_url} hata: {e}")
        return []


# ─────────────────────────────────────────────────────────────────
# PLAYWRIGHT SCRAPER (JS-rendered sayfalar icin)
# ─────────────────────────────────────────────────────────────────

def _scrape_with_playwright(url: str, source_label: str,
                             max_items: int = 25) -> list[dict]:
    """
    Headless Chromium ile sayfayi render eder,
    DOM'dan haber/blog/duyuru linklerini cikarir.

    Calisan mekanizma:
      1. Sayfayi yukle (networkidle bekle)
      2. <a> elementlerini tara
      3. Blog/news/update icerigi gibi gorunen linkleri filtrele
      4. Her link icin baslik + URL cikart
    """
    log.info(f"[Playwright] {url} render ediliyor...")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            context = browser.new_context(
                user_agent=_USER_AGENT,
                viewport={"width": 1280, "height": 800},
                locale="en-US",
            )
            page = context.new_page()

            # Gereksiz kaynaklari engelle (hiz icin)
            page.route("**/*.{png,jpg,jpeg,gif,svg,webp,woff,woff2,ttf,eot}",
                       lambda route: route.abort())
            page.route("**/analytics*", lambda route: route.abort())
            page.route("**/tracking*", lambda route: route.abort())

            page.goto(url, wait_until="networkidle", timeout=30000)

            # Sayfanin tamamen yuklenmesi icin ekstra bekleme
            page.wait_for_timeout(2000)

            # DOM'dan linkleri cek (JavaScript evaluate)
            links_data = page.evaluate("""() => {
                const results = [];
                const seen = new Set();

                // Tum <a> elementlerini tara
                document.querySelectorAll('a[href]').forEach(a => {
                    const href = a.href;
                    const text = (a.textContent || '').trim();

                    // Filtreler
                    if (!href || !text) return;
                    if (text.length < 15 || text.length > 300) return;
                    if (!href.startsWith('http')) return;
                    if (seen.has(href)) return;

                    // Blog/news/update icerigi gibi gorunen linkleri sec
                    const hrefLower = href.toLowerCase();
                    const isContent =
                        hrefLower.includes('/blog/') ||
                        hrefLower.includes('/news/') ||
                        hrefLower.includes('/post/') ||
                        hrefLower.includes('/posts/') ||
                        hrefLower.includes('/article') ||
                        hrefLower.includes('/update') ||
                        hrefLower.includes('/announcement') ||
                        hrefLower.includes('/changelog') ||
                        hrefLower.includes('/press') ||
                        hrefLower.includes('/ecosystem') ||
                        hrefLower.includes('/community');

                    // Yakinindaki <time> veya date elementini bul
                    let dateStr = null;
                    const parent = a.closest('article, .post, .card, li, div');
                    if (parent) {
                        const timeEl = parent.querySelector('time');
                        if (timeEl) {
                            dateStr = timeEl.getAttribute('datetime') || timeEl.textContent;
                        }
                    }

                    if (isContent) {
                        seen.add(href);
                        results.push({
                            url: href,
                            title: text.substring(0, 200),
                            date: dateStr,
                        });
                    }
                });

                return results;
            }""")

            browser.close()

        if not links_data:
            log.info(f"[Playwright] {url}: icerik linki bulunamadi.")
            return []

        # Normalize et
        results = []
        parsed_base = urlparse(url)
        base_domain = parsed_base.netloc.lower()

        for item in links_data[:max_items]:
            item_url = item["url"]

            # Dis domain linkleri filtrele (sadece ayni site + bilinen platformlar)
            item_domain = urlparse(item_url).netloc.lower()
            if item_domain != base_domain:
                allowed = ("medium.com", "mirror.xyz", "substack.com",
                           "blog.", "ghost.io")
                if not any(a in item_domain for a in allowed):
                    continue

            pub_at = _normalize_date(item.get("date")) if item.get("date") else None
            hashes = compute_hashes(url=item_url, title=item["title"])

            results.append({
                "data_type":    "event",
                "source_name":  source_label,
                "url":          item_url,
                "title":        item["title"],
                "body":         None,
                "url_hash":     hashes["url_hash"],
                "title_hash":   hashes["title_hash"],
                "published_at": pub_at,
            })

        log.info(f"[Playwright] {url}: {len(results)} icerik bulundu.")
        return results

    except Exception as e:
        log.error(f"[Playwright] {url} hatasi: {e}")
        return []


# ─────────────────────────────────────────────────────────────────
# FALLBACK: requests + BeautifulSoup (Playwright yoksa)
# ─────────────────────────────────────────────────────────────────

def _scrape_html_fallback(url: str, source_label: str) -> list[dict]:
    """
    Playwright yuklu degilse requests ile HTML scraping yapar.
    NOT: SPA sitelerde calismaz (ham HTML'de icerik yok).
    """
    try:
        r = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        if r.status_code != 200:
            return []

        soup = BeautifulSoup(r.text, "html.parser")
        results = []
        seen = set()

        selectors = [
            "article a", ".post a", ".news a", ".blog a",
            "main a", ".card a", "[class*='post'] a",
            "[class*='blog'] a", "[class*='news'] a",
            "[class*='article'] a",
        ]

        for selector in selectors:
            for a in soup.select(selector):
                href  = a.get("href", "")
                title = a.get_text(strip=True)

                if not title or len(title) < 15:
                    continue
                if not href or href in seen:
                    continue

                if href.startswith("/"):
                    parsed = urlparse(url)
                    href = f"{parsed.scheme}://{parsed.netloc}{href}"
                elif not href.startswith("http"):
                    continue

                # Icerik linki mi kontrolu
                href_lower = href.lower()
                content_hints = ("/blog/", "/news/", "/post/", "/article",
                                 "/update", "/announcement", "/changelog")
                if not any(h in href_lower for h in content_hints):
                    continue

                seen.add(href)
                hashes = compute_hashes(url=href, title=title)
                results.append({
                    "data_type":    "event",
                    "source_name":  source_label,
                    "url":          href,
                    "title":        title[:200],
                    "body":         None,
                    "url_hash":     hashes["url_hash"],
                    "title_hash":   hashes["title_hash"],
                    "published_at": None,
                })

        log.info(f"[HTMLFallback] {url}: {len(results)} icerik")
        return results[:20]

    except Exception as e:
        log.debug(f"[HTMLFallback] {url} hata: {e}")
        return []


# ─────────────────────────────────────────────────────────────────
# TARIH PARSE
# ─────────────────────────────────────────────────────────────────

def _normalize_date(date_str: str) -> str | None:
    """RSS/HTML tarih formatlarini ISO'ya normalize eder."""
    if not date_str:
        return None
    date_str = date_str.strip()

    # Zaten ISO formatinda mi?
    if re.match(r"\d{4}-\d{2}-\d{2}T", date_str):
        return date_str

    formats = [
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d",
        "%b %d, %Y",
        "%B %d, %Y",
        "%d %b %Y",
        "%d %B %Y",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(date_str.strip(), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.isoformat()
        except ValueError:
            continue
    return None

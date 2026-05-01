"""
Official Source Resolver
=========================
Verilen CoinGecko ID icin resmi kaynaklari ceker.

Kaynaklar:
  twitter   - @handle (X/Twitter)
  telegram  - t.me/... kanal URL'i
  website   - ana websitesi
  blog      - blog URL'i (varsa)
"""

import time
import logging
import requests

log = logging.getLogger(__name__)

COINGECKO_COIN = "https://api.coingecko.com/api/v3/coins/{id}"
_HEADERS  = {"Accept": "application/json"}
_TIMEOUT  = 12


def resolve_sources(coingecko_id: str) -> dict:
    """
    CoinGecko'daki links alanından resmi kaynaklari cikarir.
    Doner:
      {
        "twitter":  "@pepecoineth",        # veya None
        "telegram": "https://t.me/...",    # veya None
        "website":  "https://...",         # veya None
        "blog":     "https://.../blog",    # veya None
      }
    """
    detail = _fetch_links(coingecko_id)
    if not detail:
        return {}

    links = detail.get("links") or {}
    return {
        "twitter":  _extract_twitter(links),
        "telegram": _extract_telegram(links),
        "website":  _extract_website(links),
        "blog":     _extract_blog(links),
    }


# ─────────────────────────────────────────────────────────────────
# YARDIMCILAR
# ─────────────────────────────────────────────────────────────────

def _fetch_links(coingecko_id: str) -> dict | None:
    try:
        time.sleep(1.2)
        r = requests.get(
            COINGECKO_COIN.format(id=coingecko_id),
            params={
                "localization":    "false",
                "tickers":         "false",
                "market_data":     "false",
                "community_data":  "false",
                "developer_data":  "false",
            },
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
        if r.status_code == 429:
            log.warning("[SourceResolver] Rate limit — 60s bekleniyor...")
            time.sleep(60)
            r = requests.get(
                COINGECKO_COIN.format(id=coingecko_id),
                params={"localization":"false","tickers":"false",
                        "market_data":"false","community_data":"false",
                        "developer_data":"false"},
                headers=_HEADERS, timeout=_TIMEOUT,
            )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error(f"[SourceResolver] Link cekme hatasi ({coingecko_id}): {e}")
        return None


def _extract_twitter(links: dict) -> str | None:
    handle = links.get("twitter_screen_name")
    if handle:
        return f"@{handle.lstrip('@')}"
    return None


def _extract_telegram(links: dict) -> str | None:
    channel = links.get("telegram_channel_identifier")
    if channel:
        return f"https://t.me/{channel.lstrip('/')}"
    return None


def _extract_website(links: dict) -> str | None:
    homepages = links.get("homepage") or []
    # Bos olmayan ilk URL'yi al
    for url in homepages:
        if url and url.startswith("http"):
            return url.rstrip("/")
    return None


def _extract_blog(links: dict) -> str | None:
    """
    Blog URL'sini bulur. Arama stratejisi:

    1. CoinGecko announcement_url (en guvenilir)
    2. CoinGecko homepage & blockchain_site icerisinde blog ipuclari
    3. CoinGecko official_forum_url
    4. Website URL'sinden /blog, /news vb. path'leri HEAD ile deneme
    """
    blog_hints = (
        "medium.com", "blog.", "mirror.xyz", "substack.com",
        "/blog", "/news", "/updates", "/announcements",
    )

    # 1. announcement_url (en guvenilir kaynak)
    announcements = links.get("announcement_url") or []
    for url in announcements:
        if url and url.startswith("http"):
            return url.rstrip("/")

    # 2. Tum URL alanlarini tara (homepage, blockchain_site, vb.)
    url_fields = ["homepage", "blockchain_site"]
    for field in url_fields:
        for url in (links.get(field) or []):
            if url and any(hint in url.lower() for hint in blog_hints):
                return url.rstrip("/")

    # 3. official_forum_url
    forum = links.get("official_forum_url") or []
    for url in forum:
        if url and url.startswith("http"):
            return url.rstrip("/")

    # 4. Website'den /blog, /news path'lerini HEAD ile deneme
    website = _extract_website(links)
    if website:
        blog_url = _probe_blog_paths(website)
        if blog_url:
            return blog_url

    return None


def _probe_blog_paths(website: str) -> str | None:
    """
    Website URL'sine /blog, /news gibi path'ler ekleyerek
    HEAD request ile hangisinin 200 dondugunu bulur.
    """
    paths = ["/blog", "/news", "/updates", "/announcements", "/changelog"]
    for path in paths:
        url = website.rstrip("/") + path
        try:
            r = requests.head(
                url,
                timeout=6,
                allow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if r.status_code == 200:
                log.info(f"[SourceResolver] Blog path bulundu: {url}")
                return url
        except Exception:
            continue
    return None

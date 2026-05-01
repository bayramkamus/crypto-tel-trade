"""
News Collector — CryptoPanic API
==================================
Verilen coin icin CryptoPanic'ten haber ceker.
Dedup: URL hash + title hash kontrolu yapilir.

API dokumanı: https://cryptopanic.com/developers/api/
Ucretsiz tier: auth_token gerekli, gunluk istek siniri var.
"""

import time
import logging
import requests
from datetime import datetime, timezone

from scraping.dedup import compute_hashes

log = logging.getLogger(__name__)

CRYPTOPANIC_URL = "https://cryptopanic.com/api/developer/v2/posts/"
_TIMEOUT = 12


def fetch(symbol: str, auth_token: str,
          pages: int = 3, page_delay: float = 1.5) -> list[dict]:
    """
    CryptoPanic'ten haber ceker.

    Args:
        symbol:     Coin sembolu, ornek "PEPE"
        auth_token: CryptoPanic API anahtari
        pages:      Kac sayfa cekilecek (her sayfa max 20 haber)
        page_delay: Sayfalar arasi bekleme suresi (saniye)

    Doner: list of item dict:
      {
        "data_type":    "news",
        "source_name":  "cryptopanic",
        "url":          "https://...",
        "title":        "...",
        "body":         None,          # CryptoPanic ozet dondurmez
        "url_hash":     "...",
        "title_hash":   "...",
        "published_at": "2026-03-09T12:00:00+00:00",
      }
    """
    if not auth_token:
        log.warning("[NewsCollector] CRYPTOPANIC_TOKEN tanimlanmamis, atlaniyor.")
        return []

    results = []
    next_url = CRYPTOPANIC_URL
    params = {
        "auth_token":  auth_token,
        "currencies":  symbol.upper(),
    }

    for page in range(1, pages + 1):
        try:
            r = requests.get(
                next_url,
                params=params if page == 1 else None,
                timeout=_TIMEOUT,
            )
            if r.status_code == 429:
                retry = int(r.headers.get("Retry-After", 60))
                log.warning(f"[NewsCollector] Rate limit — {retry}s bekleniyor...")
                time.sleep(retry)
                r = requests.get(
                    next_url,
                    params=params if page == 1 else None,
                    timeout=_TIMEOUT,
                )

            if r.status_code == 401:
                log.error("[NewsCollector] Gecersiz API token.")
                break

            r.raise_for_status()
            data = r.json()

        except Exception as e:
            log.error(f"[NewsCollector] API hatasi (sayfa {page}): {e}")
            break

        items = data.get("results") or []
        for item in items:
            parsed = _parse_item(item)
            if parsed:
                results.append(parsed)

        # Sonraki sayfa var mi?
        next_url = data.get("next")
        if not next_url:
            break

        log.debug(f"[NewsCollector] Sayfa {page} tamamlandi ({len(items)} haber)")
        time.sleep(page_delay)

    log.info(f"[NewsCollector] {symbol}: {len(results)} haber cekidi")
    return results


def _parse_item(item: dict) -> dict | None:
    """Tek bir CryptoPanic post objesini normalize eder."""
    url   = item.get("url")
    title = item.get("title")

    if not url and not title:
        return None

    # published_at normalize et
    pub = item.get("published_at") or item.get("created_at")
    if pub:
        try:
            # CryptoPanic ISO format dondurur ama bazen Z ile biter
            pub = pub.replace("Z", "+00:00")
            datetime.fromisoformat(pub)   # gecerlilik kontrolu
        except ValueError:
            pub = None

    hashes = compute_hashes(url=url, title=title)

    # Source linki ayikla
    source = item.get("source") or {}
    source_domain = source.get("domain", "")

    return {
        "data_type":    "news",
        "source_name":  f"cryptopanic/{source_domain}" if source_domain else "cryptopanic",
        "url":          url,
        "title":        title,
        "body":         None,   # CryptoPanic icerigi API'de dondurmez
        "url_hash":     hashes["url_hash"],
        "title_hash":   hashes["title_hash"],
        "published_at": pub,
    }

"""
Deduplication Yardimcilari
===========================
URL ve baslik hash'leri uzerinden icerik tekrarini onler.
"""

import re
import hashlib
from urllib.parse import urlparse, urlencode, parse_qs, urlunparse


# ─────────────────────────────────────────────────────────────────
# URL NORMALIZE + HASH
# ─────────────────────────────────────────────────────────────────

# Temizlenecek UTM ve takip parametreleri
_STRIP_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "ref", "referrer", "source", "via", "from", "share",
    "fbclid", "gclid", "msclkid", "mc_eid",
}


def normalize_url(url: str) -> str | None:
    """
    URL'den takip parametrelerini temizler ve normalize eder.
    Ornek:
      https://coindesk.com/article?utm_source=twitter&id=123
      → https://coindesk.com/article?id=123
    """
    if not url:
        return None
    try:
        p = urlparse(url.strip())
        # Sadece bilinen parametreleri tut
        qs = parse_qs(p.query, keep_blank_values=False)
        clean_qs = {k: v for k, v in qs.items() if k not in _STRIP_PARAMS}
        clean = urlunparse((
            p.scheme.lower(),
            p.netloc.lower().lstrip("www."),
            p.path.rstrip("/"),
            p.params,
            urlencode(clean_qs, doseq=True),
            "",   # fragment'i at
        ))
        return clean
    except Exception:
        return url


def url_hash(url: str) -> str | None:
    norm = normalize_url(url)
    if not norm:
        return None
    return hashlib.sha256(norm.encode()).hexdigest()


# ─────────────────────────────────────────────────────────────────
# TITLE NORMALIZE + HASH
# ─────────────────────────────────────────────────────────────────

def normalize_title(title: str) -> str | None:
    """
    Basliği kucuk harfe cevirir, noktalama ve fazla boşluklari kaldirir.
    Ornek:
      "PEPE Coin: Price Surges 40%!" → "pepe coin price surges 40"
    """
    if not title:
        return None
    t = title.lower()
    t = re.sub(r"[^\w\s]", " ", t)   # noktalama → bosluk
    t = re.sub(r"\s+", " ", t).strip()
    return t if t else None


def title_hash(title: str) -> str | None:
    norm = normalize_title(title)
    if not norm:
        return None
    return hashlib.sha256(norm.encode()).hexdigest()


# ─────────────────────────────────────────────────────────────────
# BIRLESIK HASH HESAPLAMA
# ─────────────────────────────────────────────────────────────────

def compute_hashes(url: str = None, title: str = None) -> dict:
    """
    Hem URL hem baslik hash'ini hesaplar.
    Doner: {"url_hash": str|None, "title_hash": str|None}
    """
    return {
        "url_hash":   url_hash(url)     if url   else None,
        "title_hash": title_hash(title) if title else None,
    }

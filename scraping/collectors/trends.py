"""
Google Trends Collector
========================
pytrends uzerinden son 7 gunluk saatlik trend verisi ceker.

- Birden fazla keyword varyasyonu destekler
- Her keyword icin ayri seri ceker (karsılastirma icin)
- Rate limit icin bekleme mantigi ekli
"""

import time
import random
import logging
from datetime import datetime, timezone

# ── urllib3 >= 2.0 uyumluluk yamasi ──────────────────────────────────────────
# pytrends, Retry() icinde eski 'method_whitelist' parametresini kullaniyor.
# urllib3 2.0'da bu parametre 'allowed_methods' olarak yeniden adlandirildi.
# Asagidaki yama pytrends'e dokunmadan sorunu seffaf bicimde cozer.
try:
    from urllib3.util.retry import Retry as _Retry
    _orig_retry_init = _Retry.__init__

    def _patched_retry_init(self, *args, **kwargs):
        if "method_whitelist" in kwargs:
            kwargs["allowed_methods"] = kwargs.pop("method_whitelist")
        _orig_retry_init(self, *args, **kwargs)

    _Retry.__init__ = _patched_retry_init
except Exception:
    pass  # urllib3 yuklu degil veya farkli surum — sessizce gec
# ─────────────────────────────────────────────────────────────────────────────

try:
    from pytrends.request import TrendReq
    PYTRENDS_OK = True
except ImportError:
    PYTRENDS_OK = False
    print("[WARN] pytrends bulunamadi: pip install pytrends")

log = logging.getLogger(__name__)

# Son 7 gun, saatlik veri
_TIMEFRAME = "now 7-d"
_GEO   = ""   # Worldwide (TR icin "TR" kullanilabilir)
_GPROP = ""   # Web arama

_RETRIES      = 3
_RETRY_DELAY  = 90   # 429 sonrasi bekleme (saniye)
_KW_DELAY     = (8, 18)  # Keyword'ler arasi rastgele bekleme araligi (sn)

# Browser gibi gorunen User-Agent
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
}


def generate_keywords(symbol: str, name: str = None) -> list[str]:
    """
    Coin icin aranacak keyword listesi olusturur.
    Ornek (PEPE):
      ["PEPE coin", "PEPE crypto", "PEPE token"]
    """
    symbol = symbol.upper()
    keywords = [f"{symbol} coin", f"{symbol} crypto"]

    if name and name.lower() != symbol.lower():
        clean_name = name.strip()
        keywords.append(f"{clean_name} crypto")

    # Maksimum 5 keyword (pytrends limiti)
    return keywords[:5]


def fetch(symbol: str, name: str = None,
          keywords: list[str] = None) -> list[dict]:
    """
    Google Trends'ten son 7 gunluk saatlik veri ceker.

    Args:
        symbol:   Coin sembolu, ornek "PEPE"
        name:     Coin adi, ornek "Pepe" (keyword cesitligi icin)
        keywords: Manuel keyword listesi (None ise otomatik olusturulur)

    Doner: list of point dict:
      [{"keyword": "PEPE coin", "date": "2026-03-09T14:00:00+00:00", "value": 75}, ...]
    """
    if not PYTRENDS_OK:
        log.warning("[TrendsCollector] pytrends yuklu degil.")
        return []

    kw_list = keywords or generate_keywords(symbol, name)
    # Maksimum 3 keyword: her biri ayri istek, daha az 429 riski
    kw_list = kw_list[:3]
    log.info(f"[TrendsCollector] {symbol}: {kw_list}")

    results = []
    for i, kw in enumerate(kw_list):
        # Keyword'ler arasi rastgele bekleme (Google bot tespitini azaltir)
        if i > 0:
            delay = random.uniform(*_KW_DELAY)
            log.debug(f"[TrendsCollector] {delay:.1f}s bekleniyor...")
            time.sleep(delay)

        points = _fetch_single_keyword(kw, symbol)
        results.extend(points)

    log.info(f"[TrendsCollector] {symbol}: {len(results)} toplam veri noktasi")
    return results


def _fetch_single_keyword(kw: str, symbol: str) -> list[dict]:
    """Tek bir keyword icin trend verisini ceker. Basarisizsa bos liste doner."""
    # Her keyword icin yeni session acar (cookie/session kirliligi onlenir)
    pytrends = TrendReq(
        hl="en-US",
        tz=0,
        timeout=(10, 45),
        retries=2,
        backoff_factor=1.5,
        requests_args={"headers": _HEADERS},
    )

    for attempt in range(1, _RETRIES + 1):
        try:
            pytrends.build_payload(
                kw_list=[kw],
                timeframe=_TIMEFRAME,
                geo=_GEO,
                gprop=_GPROP,
            )
            df = pytrends.interest_over_time()
            break
        except Exception as e:
            err_str = str(e)
            if "429" in err_str or "Too Many" in err_str or "response" in err_str.lower():
                log.warning(
                    f"[TrendsCollector] Rate limit '{kw}' "
                    f"(deneme {attempt}/{_RETRIES}) — {_RETRY_DELAY}s bekleniyor..."
                )
                time.sleep(_RETRY_DELAY + random.uniform(0, 15))
            else:
                log.error(f"[TrendsCollector] '{kw}' hatasi: {e}")
                return []
    else:
        log.warning(f"[TrendsCollector] '{kw}': {_RETRIES} denemede basarisiz, atlaniyor.")
        return []

    if df is None or df.empty:
        log.warning(f"[TrendsCollector] '{kw}': bos veri dondu.")
        return []

    if "isPartial" in df.columns:
        df = df.drop(columns=["isPartial"])

    points = []
    for ts, row in df.iterrows():
        date_str = ts.to_pydatetime().replace(tzinfo=timezone.utc).isoformat()
        if kw in row:
            points.append({
                "keyword": kw,
                "date":    date_str,
                "value":   int(row[kw]),
            })

    log.info(f"[TrendsCollector] '{kw}': {len(points)} veri noktasi")
    return points


def fetch_related(symbol: str, name: str = None) -> dict:
    """
    Ilgili arama sorgulari ve konulari ceker (ek analiz icin).
    Doner: {"rising_queries": [...], "top_queries": [...]}
    """
    if not PYTRENDS_OK:
        return {}

    kw_list  = generate_keywords(symbol, name)[:1]  # Tek keyword
    pytrends = TrendReq(
        hl="en-US", tz=0, timeout=(10, 45),
        retries=2, backoff_factor=1.5,
        requests_args={"headers": _HEADERS},
    )

    try:
        pytrends.build_payload(
            kw_list=kw_list,
            timeframe=_TIMEFRAME,
            geo=_GEO,
        )
        related = pytrends.related_queries()
        kw = kw_list[0]
        return {
            "rising_queries": _extract_queries(related, kw, "rising"),
            "top_queries":    _extract_queries(related, kw, "top"),
        }
    except Exception as e:
        log.debug(f"[TrendsCollector] related_queries hatasi: {e}")
        return {}


def _extract_queries(related: dict, kw: str, kind: str) -> list[str]:
    try:
        df = related.get(kw, {}).get(kind)
        if df is not None and not df.empty:
            return df["query"].tolist()[:10]
    except Exception:
        pass
    return []

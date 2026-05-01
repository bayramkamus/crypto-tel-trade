"""
AI Extract + Classify
======================
raw_content tablosundaki ham icerigi siniflandirir.

Her icerik icin:
  - Bu coin hakkinda mi? (is_relevant)
  - Icerik turu: news / event / announcement / fud / rumor / other
  - Sentiment (news/social): positive / negative / neutral
  - Event varsa: event_type, event_date, summary
  - Onem seviyesi: high / medium / low

NOT: Bu modul OpenAI API kullanir (openai paketi gerekli).
     API key olmadan siniflandirma atlaniyor, ham veriler kullanilabilir.

Kullanim:
  from scraping.ai_classify import classify_batch
  results = classify_batch(items, symbol="PEPE", name="Pepe")
"""

import os
import json
import time
import logging

log = logging.getLogger(__name__)

try:
    import openai
    OPENAI_OK = True
except ImportError:
    OPENAI_OK = False
    log.warning("⚠  openai paketi bulunamadi. "
                "AI siniflandirma devre disi. pip install openai")

_MODEL      = "gpt-4o-mini"   # Hizli ve ekonomik
_TIMEOUT    = 30
_BATCH_SIZE = 10   # Her LLM cagirisinda kac icerik


# ─────────────────────────────────────────────────────────────────
# ANA FONKSİYON
# ─────────────────────────────────────────────────────────────────

def classify_batch(items: list[dict], symbol: str,
                   name: str = None) -> list[dict]:
    """
    items: raw_content satirlarinin listesi
    Doner: her item icin classification dict eklenmiş kopya

    Her sonuc dict:
      {
        "content_id":   int,
        "is_relevant":  True/False,
        "content_type": "news"|"announcement"|"event"|"fud"|"rumor"|"other",
        "sentiment":    "positive"|"negative"|"neutral"|None,
        "event_type":   "listing"|"partnership"|"upgrade"|"burn"|"other"|None,
        "event_date":   "2026-03-10"|None,
        "summary":      "...",
        "importance":   "high"|"medium"|"low",
      }
    """
    if not OPENAI_OK:
        log.warning("[Classify] openai paketi yok, ham veri donduruluyor.")
        return [_empty_result(item) for item in items]

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        log.warning("[Classify] OPENAI_API_KEY tanimlanmamis, atlaniyor.")
        return [_empty_result(item) for item in items]

    client  = openai.OpenAI(api_key=api_key)
    results = []

    # Toplu islem: _BATCH_SIZE'lik gruplarda gonder
    for i in range(0, len(items), _BATCH_SIZE):
        batch = items[i:i + _BATCH_SIZE]
        batch_results = _classify_batch(client, batch, symbol, name or symbol)
        results.extend(batch_results)
        if i + _BATCH_SIZE < len(items):
            time.sleep(0.5)

    log.info(f"[Classify] {len(results)} icerik siniflandirildi.")
    return results


# ─────────────────────────────────────────────────────────────────
# BATCH SINIFLANDIRMA
# ─────────────────────────────────────────────────────────────────

def _classify_batch(client, items: list[dict],
                    symbol: str, name: str) -> list[dict]:
    """Bir grup icerigi tek LLM cagirisinda siniflandirir."""

    # Prompt'a gidecek icerik listesi
    content_list = []
    for idx, item in enumerate(items):
        title = item.get("title") or ""
        body  = (item.get("body") or "")[:300]  # Maliyet kontrolu
        content_list.append(
            f"[{idx}] TITLE: {title}\nBODY: {body}"
        )

    prompt = _build_prompt(symbol, name, content_list)

    try:
        response = client.chat.completions.create(
            model=_MODEL,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.choices[0].message.content
        parsed = _parse_response(raw, len(items))
    except Exception as e:
        log.error(f"[Classify] API hatasi: {e}")
        return [_empty_result(item) for item in items]

    # Sonuclara content_id ekle
    results = []
    for idx, item in enumerate(items):
        if idx < len(parsed):
            result = parsed[idx]
        else:
            result = _empty_classification()
        result["content_id"] = item.get("id")
        results.append(result)

    return results


def _build_prompt(symbol: str, name: str,
                  content_list: list[str]) -> str:
    items_text = "\n\n".join(content_list)
    return f"""Sen bir kripto para haber analiz uzmanisın.
Asagidaki iceriklerin her biri {name} ({symbol}) coin'i hakkinda mi diye degerlendirmeni istiyorum.

Her icerik icin asagidaki JSON formatinda cevap ver:
{{
  "idx": <sayi>,
  "is_relevant": true/false,
  "content_type": "news" | "announcement" | "event" | "fud" | "rumor" | "other",
  "sentiment": "positive" | "negative" | "neutral",
  "event_type": "listing" | "partnership" | "upgrade" | "burn" | "regulatory" | "other" | null,
  "event_date": "YYYY-MM-DD" | null,
  "summary": "<tek cumle ozet>",
  "importance": "high" | "medium" | "low"
}}

Tum icerikleri bir JSON dizisi olarak don: [{{"idx": 0, ...}}, {{"idx": 1, ...}}, ...]

Icerikler:
{items_text}

Sadece JSON dizisini don, baska aciklama ekleme."""


def _parse_response(raw: str, expected_count: int) -> list[dict]:
    """LLM cevabini parse eder."""
    try:
        # JSON blogu bul
        start = raw.find("[")
        end   = raw.rfind("]") + 1
        if start == -1 or end == 0:
            raise ValueError("JSON listesi bulunamadi")

        data = json.loads(raw[start:end])
        # idx sirasina gore sirala
        data.sort(key=lambda x: x.get("idx", 0))
        return [_normalize_classification(d) for d in data]
    except Exception as e:
        log.error(f"[Classify] Parse hatasi: {e}\nRaw: {raw[:200]}")
        return [_empty_classification() for _ in range(expected_count)]


def _normalize_classification(d: dict) -> dict:
    """LLM ciktisini guvende normalize eder."""
    VALID_TYPES      = {"news", "announcement", "event", "fud", "rumor", "other"}
    VALID_SENTIMENT  = {"positive", "negative", "neutral"}
    VALID_EVENT_TYPE = {"listing", "partnership", "upgrade", "burn",
                        "regulatory", "other"}
    VALID_IMPORTANCE = {"high", "medium", "low"}

    return {
        "is_relevant":  bool(d.get("is_relevant", False)),
        "content_type": d.get("content_type", "other") if d.get("content_type") in VALID_TYPES else "other",
        "sentiment":    d.get("sentiment") if d.get("sentiment") in VALID_SENTIMENT else "neutral",
        "event_type":   d.get("event_type") if d.get("event_type") in VALID_EVENT_TYPE else None,
        "event_date":   d.get("event_date"),
        "summary":      str(d.get("summary") or "")[:300],
        "importance":   d.get("importance", "low") if d.get("importance") in VALID_IMPORTANCE else "low",
    }


def _empty_classification() -> dict:
    return {
        "is_relevant":  None,
        "content_type": None,
        "sentiment":    None,
        "event_type":   None,
        "event_date":   None,
        "summary":      None,
        "importance":   None,
    }


def _empty_result(item: dict) -> dict:
    r = _empty_classification()
    r["content_id"] = item.get("id")
    return r

"""
Ticker ve Yön Çıkarma — Paylaşılan Modül
==========================================
Tüm pipeline bileşenleri (collector, scraping, backtest, rapor)
bu modülden import eder. NOISE ve regex tek kaynakta yaşar;
bir değişiklik her yere yansır.

Yön değerleri:
  "LONG"       — Açık LONG / BUY ifadesi
  "SHORT"      — Açık SHORT / SELL ifadesi
  "LONG_IMPL"  — Emoji veya anahtar kelime bazlı örtülü boğa sinyali
  None         — Yön sinyali yok (mesaj NEUTRAL olarak etiketlenir)
"""

import re

# Ticker olarak yorumlanmaması gereken kelimeler
NOISE: frozenset[str] = frozenset({
    "VIP", "TP", "SL", "USD", "THE", "FOR", "NEW", "ALL", "NOW", "TOP", "GET",
    "NOT", "ARE", "YOU", "HAS", "ITS", "DAY", "OUR", "MAX", "USE", "HOW", "API",
    "NDX", "DJI", "SPX", "DXY", "BK", "ATH", "ATL", "CEX", "DEX", "ETF", "NFT",
    "AI", "WEB", "BOT", "APP", "P2P", "AMA", "CEO", "CMC", "EUR", "GBP",
    "JUST", "IN", "BREAKING", "LIVE", "NEWS", "UPDATE", "ALERT", "INFO",
})

# Öncelik sırasına göre ticker regex kalıpları
TICKER_PATTERNS: tuple[str, ...] = (
    r"#([A-Z]{2,10}/USDT)",
    r"#([A-Z]{2,10}USDT)",
    r"Coin\s*:\s*#?([A-Z]{2,10})",
    r"\$([A-Z]{2,10})\b",
    r"\b([A-Z]{2,10})/USDT\b",
    r"#([A-Z]{2,10})\b",
    r"\b([A-Z]{2,10})\s*[-:]\s*(?:LONG|SHORT|BUY|SELL)\b",
    r"^([A-Z]{2,5})\b",           # Mesaj başında düz ticker: "XAI + 0.8R..."
)

# Örtülü boğa (LONG_IMPL) sinyali — emoji ve anahtar kelime kalıpları
# Kasıtlı olarak dar tutuldu: net yükseliş niyeti taşıyan ifadeler.
# "izleyin", "dikkat", "giriş" gibi nötr kelimeler dahil edilmedi.
_IMPLICIT_LONG_RE = re.compile(
    r"(?:"
    # Boğa emojileri
    r"🚀|🔥|📈|💎|🌙|🤑|⬆️|🔝|💥|🏆|🎯"
    # İngilizce açık boğa kelimeleri (IGNORECASE ile büyük/küçük fark etmez)
    r"|pump|moon|breakout|bullish|pumping|flying|mooning|ripping"
    r"|break\s*out|bull\s*run|bull\s*flag"
    # Türkçe boğa ifadeleri
    r"|alım\b|alış\b"
    r"|yükseliyor|fırlıyor|uçuyor|pompalıyor|patluyor"
    r"|yükseliş|fırlayor|ralli"
    r")",
    re.UNICODE | re.IGNORECASE,
)

# SHORT sinyalini yanlış tetikleyebilecek bağlamı yakalamak için
# (örn: "SELL pressure" vs "SELL signal")
_SHORT_CONTEXT_RE = re.compile(
    r"\bSHORT\b|\bSELL\b|Short\b|\bdüşüyor\b|\bayıcı\b|\bSHORTLA\b",
    re.UNICODE,
)


def _normalize_ticker_text(text: str) -> str:
    """Telegram markdown artifacts can split otherwise normal ticker patterns."""
    text = re.sub(r"[*_`]+", "", text)
    text = re.sub(r"\s*/\s*", "/", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def extract_ticker(text: str) -> str | None:
    """
    Mesaj metninden ticker sembolünü çıkarır.
    İlk eşleşen kalıbı döner; NOISE listesindeki kelimeler atlanır.
    """
    text = _normalize_ticker_text(text)
    for pat in TICKER_PATTERNS:
        m = re.search(pat, text)
        if m:
            t = m.group(1).replace("/USDT", "").replace("USDT", "")
            if len(t) >= 2 and t not in NOISE:
                return t
    return None


def extract_direction(text: str) -> str | None:
    """
    Mesaj metninden sinyal yönünü çıkarır.

    Dönüş değerleri:
      "LONG"      — açık LONG / BUY
      "SHORT"     — açık SHORT / SELL
      "LONG_IMPL" — emoji/anahtar kelime bazlı örtülü boğa sinyali
      None        — yön sinyali yok
    """
    # Açık yönler önce kontrol edilir
    if re.search(r"\bLONG\b|\bBUY\b|Long\b", text):
        return "LONG"
    if _SHORT_CONTEXT_RE.search(text):
        return "SHORT"
    # Örtülü boğa sinyali
    if _IMPLICIT_LONG_RE.search(text):
        return "LONG_IMPL"
    return None

# 🔗 [source_resolver.py](file:///c:/Users/bayra/Desktop/tel-pipline-scraping/scraping/source_resolver.py) — Detaylı Kod Açıklaması

Bu dosya, bir projenin / kripto paranın **Resmi Kaynaklarını** (Official Sources) bulmakla görevlidir. Sistem, herhangi bir haber veya etkinlik çekeceği zaman rastgele yerlere bakmak yerine, bu modülün bulduğu resmi Twitter sayfasını, Telegram kanalını ve Web sitesini dinler. O yüzden sistem için bir "pusula" görevi görür.

Bu veriler **CoinGecko API** üzerinden çekilir.

---

## 📦 1. Modül Dökümanı ve Import'lar (Satır 1–21)

```python
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
```

- Modül, bir coin'in 4 temel iletişim kanalını hedefler: Twitter, Telegram, Website ve Blog.
- `COINGECKO_COIN`: İstek atılacak ana endpoint. İçerisindeki `{id}` kısmı Python'un objesi formatlanırken `coingecko_id` (örn: `"bitcoin"`, `"pepe"`) ile değiştirilecektir.

---

## 🚀 2. Ana Fonksiyon: [resolve_sources](file:///c:/Users/bayra/Desktop/tel-pipline-scraping/scraping/source_resolver.py#24-46) (Satır 24–45)

```python
def resolve_sources(coingecko_id: str) -> dict:
```

Önceki dosyalarda gördüğümüz **Orkestratör/Facade (Cephe)** mantığıyla çalışır. Tüm alt işlemleri ([_extract_twitter](file:///c:/Users/bayra/Desktop/tel-pipline-scraping/scraping/source_resolver.py#84-89), [_extract_telegram](file:///c:/Users/bayra/Desktop/tel-pipline-scraping/scraping/source_resolver.py#91-96) vb.) koordine edip nihai bir JSON/Sözlük döner.

```python
    detail = _fetch_links(coingecko_id)
    if not detail:
        return {}
```
Gidip CoinGecko'dan coin'in tüm detayını (büyük bir JSON objesi) çeker. İnternet kesilirse veya CoinGecko çökerse [detail](file:///c:/Users/bayra/Desktop/tel-pipline-scraping/scraping/coin_resolver.py#156-187) None döner, fonksiyon da hata vermek yerine boş sözlük `{}` döner.

```python
    links = detail.get("links") or {}
    return {
        "twitter":  _extract_twitter(links),
        "telegram": _extract_telegram(links),
        "website":  _extract_website(links),
        "blog":     _extract_blog(links),
    }
```
API'den dönen koca JSON'ın sadece `"links"` kısmını alır ve işi özelleşmiş alt fonksiyonlara (yardımcılara) delege eder. Bütün temizleme ve arama işlemleri aşağıda gerçekleşir.

---

## 🌐 3. API'den Veri Çekme (Satır 52–81)

```python
def _fetch_links(coingecko_id: str) -> dict | None:
```

CoinGecko'dan detay çeken kısımdır. [coin_resolver.py](file:///c:/Users/bayra/Desktop/tel-pipline-scraping/scraping/coin_resolver.py) dosyasındaki mantıkla tamamen aynı **Rate Limit (Aşırı İstek Engeli)** ve **Retry (Tekrar Dene)** kurgusunu içerir.

```python
        time.sleep(1.2)
        r = requests.get(
            COINGECKO_COIN.format(id=coingecko_id),
            params={
                "localization":    "false",
                "tickers":         "false",
                "market_data":     "false",
                "community_data":  "false",
                "developer_data":  "false",
            }, ...
```
API'ye istek atarken tüm gereksiz verileri (`market_data` vb.) `false` yaparak yükü (Data Payload) küçültür. Sadece bize gereken `"links"` nesnesinin dönmesini sağlar. Ağırlığı azalttığı için istek daha hızlı yanıtlanır.

```python
        if r.status_code == 429:
            log.warning("[SourceResolver] Rate limit — 60s bekleniyor...")
            time.sleep(60)
            r = requests.get(...)
```
Eğer 429 statü kodu gelirse bu API'yi çok hızlı darladığımız anlamına gelir. 60 saniye bekler ve şansını bir kez daha dener.

---

## 🐦 4. Sosyal Medya Çıkarımı (Satır 84–95)

Sosyal medya hesapları CoinGecko'dan biraz yamuk/dağınık gelir. Bunları bizim formatımıza (`@kullaniciadi` ve tam url) uyduran bölümlerdir.

### Twitter Çıkarımı
```python
def _extract_twitter(links: dict) -> str | None:
    handle = links.get("twitter_screen_name")
    if handle:
        return f"@{handle.lstrip('@')}"
    return None
```
- API'den `pepecoineth` döner. Fonksiyon bunu `@pepecoineth` yapar.
- `lstrip('@')`: Başına zaten `@` koyan projeler varsa, `@` işaretini silip sonra bizim `@` işaretini koyar (Örn: `@@pepe` olmasını engeller).

### Telegram Çıkarımı
```python
def _extract_telegram(links: dict) -> str | None:
    channel = links.get("telegram_channel_identifier")
    if channel:
        return f"https://t.me/{channel.lstrip('/')}"
    return None
```
- API'den `pepe_kanal_resmi` döner. Bunu tıklanabilir tam bir URL'ye `https://t.me/pepe_kanal_resmi` çevirir. Yine baştaki `/` kirliliğini `lstrip('/')` ile engeller.

---

## 🌍 5. Web ve Blog Keşfi (Satır 98–171)

### Website Çıkarımı
```python
def _extract_website(links: dict) -> str | None:
    homepages = links.get("homepage") or []
    # Bos olmayan ilk URL'yi al
    for url in homepages:
        if url and url.startswith("http"):
            return url.rstrip("/")
```
CoinGecko'da birden fazla internet sitesi alanı (homepage) olabilir. İlk geçerli (http ile başlayan ve boş olmayan) bağlantıyı seçer. Sonundaki bitirme takısını (trailing slash `/`) temizler (`https://pepe.com/` → `https://pepe.com`).

### 🕵️ Blog Bulma Stratejileri (Satır 107–147)

Bir projenin resmi duyurularının paylaşıldığı mecrayı bulmak zordur. Bu fonksiyon 4 Aşama (Fallback Strategy) ile blog bulmaya çalışır. Zeka seviyesinin en yüksek olduğu fonksiyonlardan biridir:

```python
def _extract_blog(links: dict) -> str | None:
    blog_hints = ("medium.com", "blog.", "mirror.xyz", "substack.com", ...)
```

**Strateji 1: Doğrudan Duyuru URL'leri**
```python
    announcements = links.get("announcement_url") or []
    for url in announcements:
        if url and url.startswith("http"):
            return url.rstrip("/")
```
Eğer projenin CoinGecko'ya kaydettirdiği bir resmi `announcement_url` varsa direkt onu alır. Bu en güvenilirdir.

**Strateji 2: URL'lerin içinde "blog" aramak**
```python
    url_fields = ["homepage", "blockchain_site"]
    for field in url_fields:
        for url in (links.get(field) or []):
            if url and any(hint in url.lower() for hint in blog_hints):
                return url.rstrip("/")
```
Sitede veya resmi ağ adresinde (`blog.pepe.com`, `medium.com/pepe`) yukarıda tanımladığı `blog_hints` ipuçlarından (medium, substack vb.) biri geçiyorsa onu blog olarak işareler. ([any](file:///c:/Users/bayra/Desktop/tel-pipline-scraping/scraping/coin_resolver.py#218-230) fonksiyonu ile).

**Strateji 3: Resmi Forumu aramak**
`official_forum_url` alanında bir kayıt varsa, bunu blog kabul eder.

**Strateji 4: Aktif Web Taraması (Active Probing - Satır 150–171)**
```python
def _probe_blog_paths(website: str) -> str | None:
    paths = ["/blog", "/news", "/updates", "/announcements", "/changelog"]
    for path in paths:
        url = website.rstrip("/") + path
        try:
            r = requests.head(url, timeout=6, allow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                ...
```
- EĞER yukarıdaki 3 stratejiden sonuç alınamazsa sistem siber güvenlikteki brute-force (klasör bulma) araçlarına benzeyen bir yöntem izler.
- Bulduğu websitesinin ardına sık kullanılan yol isimlerini (`/blog`, `/news`, `/updates`) yapıştırarak o sayfalara **HEAD request** atar.
- `requests.head()`: Tüm HTML'i indirmek yerine sadece sayfa var mı diye Sorar. Bu Sunucuyu yormaz, veri tasarrufu sağlar.
- `status_code == 200` (Başarılı) yanıtı dönerse "Aha! Buldum" der ve o linki sisteme verir. Karşı site bot olduğumuzu anlayıp reddetmesin diye `User-Agent` olarak normal bir tarayıcı (Mozilla) kılığına girer.

---

## 🎯 Tasarım Mimarisi / Özet

- **Önceliklendirme (Priority / Fallback Strategy):** En kesin olandan (`announcement_url`) başlayıp giderek en az ihtimali olan yöne doğru (Website sahte URL denemeleri [_probe_blog_paths](file:///c:/Users/bayra/Desktop/tel-pipline-scraping/scraping/source_resolver.py#150-171)) arama yapar. Mükemmel bir algoritma mantığıdır.
- **Güvenli Erişim:** İstisnasız her şey `try/except` ile korunmuş `if not url` kontrollerinden geçirilmiştir. Değişkenlerin bozuk gelmesi sistemde bir çökmeye neden olmaz.
- **Resource Optimization (Hafıza Yönetimi):** CoinGecko'ya istek atarken gereksiz verileri kapaması (`params={'market_data':'false'}`) ve hedef sitenin sadece sunucu başlıklarını sorgulayıp HTML'i çekmemesi (`requests.head()`) kodun çok hızlı çalışmasını sağlar.

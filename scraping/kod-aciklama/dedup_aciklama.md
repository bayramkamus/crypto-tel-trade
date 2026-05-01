# 🛡️ [dedup.py](file:///c:/Users/bayra/Desktop/tel-pipline-scraping/scraping/dedup.py) — Detaylı Kod Açıklaması

Bu dosya, internetten çekilen içeriklerde (haberler, etkinlikler) **Deduplication (Veri Tekilleştirme)** işlemi yapmak için kullanılan yardımcı fonksiyonları içerir. 

Aynı haberi farklı kaynaklardan veya aynı kaynaktan farklı zamanlarda çektiğimizde veritabanını şişirmemek için, içeriğin **URL'sini** ve **Başlığını** normalize edip **kriptografik özetini (hash)** çıkarır.

---

## 📦 1. Modül Dökümanı ve Import'lar (Satır 1–9)

```python
"""
Deduplication Yardimcilari
===========================
URL ve baslik hash'leri uzerinden icerik tekrarini onler.
"""

import re
import hashlib
from urllib.parse import urlparse, urlencode, parse_qs, urlunparse
```

| Modül/Paket | Ne İşe Yarıyor? |
|-------------|-----------------|
| [re](file:///c:/Users/bayra/Desktop/tel-pipline-scraping/scraping/coin_resolver.py#24-87) | Metin işlemede (Regex) kullanılır (başlıktaki noktalamaları silmek için). |
| `hashlib` | String'lerden SHA-256 algoritmasıyla benzersiz özet (hash) üretmek için. |
| `urllib.parse` | URL'leri parçalarına ayırmak, içindeki "utm_source" gibi parametreleri temizlemek ve tekrar birleştirmek için hayat kurtaran Python standart kütüphanesi. |

---

## 🧹 2. URL Normalizasyonu (Satır 16–49)

Aynı sayfanın linki çoğu zaman farklı parametrelerle (reklam takibi için vs.) paylaşılır. 

**Örnek:**
1. `https://haber.com/pepe-cikiyor`
2. `https://haber.com/pepe-cikiyor?utm_source=twitter&utm_campaign=yaz`
3. `www.haber.com/pepe-cikiyor/`

Aslında bu üç link de **Aynı Habere** gider. Eğer bunu hesaba katmazsak, sistemi kolayca kandırıp aynı haberi 3 kere kaydederiz.

### `_STRIP_PARAMS` Seti (Satır 16–21)
```python
_STRIP_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "ref", "referrer", "source", "via", "from", "share",
    "fbclid", "gclid", "msclkid", "mc_eid",
}
```
Silinecek UTM (Google Analytics) ve sosyal medya takip parametrelerinin listesidir. Set (küme) olarak tanımlanmıştır ki arama işlemi `O(1)` hızında olsun.

### [normalize_url](file:///c:/Users/bayra/Desktop/tel-pipline-scraping/scraping/dedup.py#24-49) Fonksiyonu (Satır 24–49)
```python
def normalize_url(url: str) -> str | None:
```
Bu fonksiyon karmaşık linki temizler ve standart bir formata sokar.

```python
    try:
        p = urlparse(url.strip())
        qs = parse_qs(p.query, keep_blank_values=False)
```
- `urlparse`: URL'yi scheme (`https`), netloc (`haber.com`), path (`/haber`), query (`?id=5`) gibi bileşenlerine böler.
- `parse_qs`: Soru işaretinden sonraki kısmı `{"utm_source": ["twitter"], "id": ["5"]}` şeklinde dictionary'e (sözlüğe) çevirir.

```python
        clean_qs = {k: v for k, v in qs.items() if k not in _STRIP_PARAMS}
```
- **Dict Comprehension**: Eğer parametre adı `_STRIP_PARAMS` listesinde varsa, o parametreyi ÇÖPE ATAR. Sadece işe yarayanlar (örn: `?article_id=15`) kalır.

```python
        clean = urlunparse((
            p.scheme.lower(),                   # 1. https (kucuk harf)
            p.netloc.lower().lstrip("www."),    # 2. www. varsa sil
            p.path.rstrip("/"),                 # 3. sondaki slash'i (/) sil
            p.params,                           # 4. parametreler (genelde bos)
            urlencode(clean_qs, doseq=True),    # 5. temizlenmis query'i string yap
            "",                                 # 6. #yorumlar gibi fragmentleri at
        ))
        return clean
```
- `urlunparse`: Temizlenmiş parçaları yeniden birleştirip temiz bir URL string'i oluşturur.

> [!TIP]
> **Defensive Programming**: Bütün bu işlemler `try/except` bloğu içine alınmıştır. Eğer parse edilemeyen çok bozuk bir URL gelirse (sayfa çökmesin diye) string'in orijinal hali geri döndürülür (`except Exception: return url`).

### URL Hash'leme (Satır 51–56)
```python
def url_hash(url: str) -> str | None:
    norm = normalize_url(url)
    if not norm:
        return None
    return hashlib.sha256(norm.encode()).hexdigest()
```
Normalize edilmiş tertemiz URL'yi alır ve **SHA-256** ile şifreler.
- Dönen değer 64 karakter uzunluğunda benzersiz bir hex string'idir (Örn: `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`).
- Veritabanında uzun uzun URL'leri aramak yerine, bu 64 karakter üzerinden arama yapmak veritabanı performansını (index taraması) katlayarak artırır!

---

## 📝 3. Başlık Normalizasyonu (Satır 62–80)

Haber kanalları bazen URL vermez veya sadece metin atar. Veya URL'yi kısaltma servisi (bit.ly vb.) üzerinden verdikleri için URL'ler tutmaz ama haber aynıdır. Bu yüzden **Habere Atılan Başlık** üzerinden de tekilleştirme yaparız.

### [normalize_title](file:///c:/Users/bayra/Desktop/tel-pipline-scraping/scraping/dedup.py#62-74) Fonksiyonu
```python
def normalize_title(title: str) -> str | None:
    """
    Ornek: "PEPE Coin: Price Surges 40%!" → "pepe coin price surges 40"
    """
    if not title:
        return None
    t = title.lower()
    t = re.sub(r"[^\w\s]", " ", t)   # noktalama → bosluk
    t = re.sub(r"\s+", " ", t).strip()
    return t if t else None
```

- `title.lower()`: Tüm harfleri küçültür ("PEPE" == "pepe" sayılması için).
- `re.sub(r"[^\w\s]", " ", t)`: Harf (`\w`) ve Boşluk (`\s`) DIŞINDAKİ her şeyi (ünlem, nokta, virgül, emoji vb.) kaldırıp yerine boşluk koyar. (Örn: `Bitcoin is up!!!` → `bitcoin is up   `).
- `re.sub(r"\s+", " ", t)`: İki ve daha fazla ardışık boşluğu (`\s+`) Tek Boşluğa (` `) indirger. (`bitcoin is up   ` → `bitcoin is up`).

### Title Hash'leme (Satır 76–80)
```python
def title_hash(title: str) -> str | None:
    norm = normalize_title(title)
    if not norm:
        return None
    return hashlib.sha256(norm.encode()).hexdigest()
```
Aynı URL'deki gibi tertemiz başlığı alıp `SHA-256` özetine çevirir. Başlığa bilerek fazladan boşluk koysalar veya noktalama canavarlığı yapsalar bile hash aynı çıkacaktır.

---

## ⚙️ 4. Birleşik Hash Hesaplama (Satır 87–96)

```python
def compute_hashes(url: str = None, title: str = None) -> dict:
    """
    Hem URL hem baslik hash'ini hesaplar.
    Doner: {"url_hash": str|None, "title_hash": str|None}
    """
    return {
        "url_hash":   url_hash(url)     if url   else None,
        "title_hash": title_hash(title) if title else None,
    }
```

Dış modüllerin ([collect.py](file:///c:/Users/bayra/Desktop/tel-pipline-scraping/scraping/collect.py) veya veri toplayıcılar) kullanacağı **Ana Fonksiyon** budur. Verilen URL ve Title'ı alıp, yukardaki adımlardan geçirip tek bir Dictionary döndürür. Bu dictionary daha sonra veritabanına ([db.py](file:///c:/Users/bayra/Desktop/tel-pipline-scraping/scraping/db.py) içindeki tekilleştirme tablolarına) kaydetmek üzere kullanılır. 

---

## 🎯 Tasarımın Özeti

Tasarımda **Safe/Defensive (Savunmacı)** bir kodlama yapısı görüyoruz:
- Link boş (None) gelirse program hata (NullPointer vb.) vermesin diye bolca `if not url: return None` kontrolleri var.
- Regex ve Parse hatalarını göğüsleyip sistemi ayakta tutan `try/except` blokları var.
- Tek bir merkezi string normları ve `hashlib.sha256` ile büyük text verileri çok küçük ve aranabilir 64 bytelık karakter setlerine dönüştürülüyor.

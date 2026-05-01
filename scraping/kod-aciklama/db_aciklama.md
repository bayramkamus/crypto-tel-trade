# 🗄️ [db.py](file:///c:/Users/bayra/Desktop/tel-pipline-scraping/scraping/db.py) — Detaylı Kod Açıklaması

Bu modül, scraping (veri çekme) sürecinde elde edilen verilerin saklanacağı **SQLite veritabanı** (`scraping_data.db`) için tablo yapılarını (schema) ve temel ekleme/okuma (CRUD) işlemlerini tanımlar.

---

## 📦 1. Modül Docstring'i ve Import'lar (Satır 1–18)

```python
"""
Scraping Veritabani
====================
scraping_data.db icin schema ve CRUD operasyonlari.

Tablolar:
  coins            - cozumlenmis coin kimlikleri
  official_sources - resmi kaynak listesi (Twitter, Telegram, website)
  raw_content      - ham cekilen icerik (news, event)
  seen_hashes      - dedup icin URL + title hash
  trends_data      - pytrends zaman serisi
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = "scraping_data.db"
```

Modülün başında veri tabanındaki **5 ana tablonun** ne işe yaradığı özetlenir.
- `sqlite3`: Python'a gömülü, sunucu gerektirmeyen hafif veritabanı motoru.
- `datetime`, `timezone`: Güvenilir ve standart zaman damgaları (timestamp) oluşturmak için.
- `DB_PATH`: Varsayılan veritabanı dosya adı.

---

## 🏗️ 2. Veritabanı Şeması (Satır 25–74)

```python
SCHEMA = """
CREATE TABLE IF NOT EXISTS coins (...);
CREATE TABLE IF NOT EXISTS official_sources (...);
...
```

Burada SQLite için SQL Data Definition Language (DDL) komutları yer alıyor. Hepsine satır satır bakalım:

### `coins` Tablosu
```sql
CREATE TABLE IF NOT EXISTS coins (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL,
    name            TEXT,
    coingecko_id    TEXT UNIQUE,
    contract        TEXT,
    chain           TEXT,
    market_cap_usd  REAL,
    ambiguous       INTEGER DEFAULT 0,
    resolved_at     TEXT
);
```
Coin kimliklerini tutar.
- `coingecko_id TEXT UNIQUE`: Aynı `coingecko_id`'ye sahip birden fazla coin olamaz. Gelen yeni verinin **yeni mi yoksa güncelleme mi** olduğunu belirlemek için kilit noktadır.
- `ambiguous`: (0 veya 1). SQLite'ta boolean (True/False) tipi yoktur, bunun yerine `INTEGER` (0/1) kullanılır.

### `official_sources` Tablosu
```sql
CREATE TABLE IF NOT EXISTS official_sources (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    coin_id     INTEGER NOT NULL REFERENCES coins(id),
    source_type TEXT NOT NULL,
    value       TEXT NOT NULL,
    UNIQUE(coin_id, source_type, value)
);
```
Coin'in Twitter, Telegram gibi linklerini saklar.
- `REFERENCES coins(id)`: **Foreign Key** (Yabancı Anahtar). Bu kaynağın hangi coin'e ait olduğunu `coins` tablosuna bağlar.
- `UNIQUE(coin_id, source_type, value)`: **Composite Unique Constraint**. Bir coin için aynı tipte ve aynı URL'de kaynak ikinci kez eklenemez.

### `raw_content` Tablosu
```sql
CREATE TABLE IF NOT EXISTS raw_content (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    coin_id      INTEGER NOT NULL REFERENCES coins(id),
    data_type    TEXT NOT NULL,       -- 'news' veya 'event'
    source_name  TEXT NOT NULL,       -- 'CryptoPanic', 'Telegram' vs.
    url          TEXT,
    title        TEXT,
    body         TEXT,
    url_hash     TEXT,
    title_hash   TEXT,
    published_at TEXT,
    fetched_at   TEXT NOT NULL
);
```
İnternetten çekilen ham haberleri ve etkinlikleri sular. `url_hash` ve `title_hash` sütunları **veri tekilleştirme (deduplication)** için kullanılır.

### `seen_hashes` Tablosu
```sql
CREATE TABLE IF NOT EXISTS seen_hashes (
    hash        TEXT PRIMARY KEY,
    first_seen  TEXT NOT NULL
);
```
Daha önce veritabanına kaydedilmiş tüm başlık ve URL'lerin hash (şifrelenmiş özet) değerlerini tutar. Yeni bir içerik geldiğinde, hash'i bu tabloda aranır. Varsa içerik zaten kaydedilmiştir.

### `trends_data` Tablosu
```sql
CREATE TABLE IF NOT EXISTS trends_data (
    ...
    UNIQUE(coin_id, keyword, date)
);
```
Google Trends puanlarını tutar. Aynı coin, aynı kelime ve aynı gün için **sadece bir puan** kaydedilebilir.

---

## 🔌 3. Bağlantı ve Başlatma (Satır 81–94)

```python
def get_conn(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn
```
SQLite bağlantısı açan çekirdek fonksiyon. İçinde bazı çok önemli ayarlar var:

| Ayar | Ne İşe Yarıyor? |
|------|-----------------|
| `conn.row_factory = sqlite3.Row` | Dönen SQL sonuçlarını liste (tuple) değil, **sözlük (dict)** benzeri bir yapıda döndürür. Böylece `row["symbol"]` şeklinde adıyla erişilebilir. Okunabilirliği artırır. |
| `PRAGMA journal_mode=WAL` | **Write-Ahead Logging**: SQLite'ın performansını inanılmaz artırır. Aynı anda bir sürecin yazmasını ve diğerinin veritabanını okumasını kilitlenmeden sağlar. (Concurrency) |
| `PRAGMA foreign_keys=ON` | SQLite varsayılan olarak Foreign Key kısıtlamalarını kontrol etmez. Bunu açarak `REFERENCES coins(id)` kuralını aktif ediyoruz. Olmayan coin_id'ye haber eklenmesini engeller. |

```python
def init_db(db_path: str = DB_PATH):
    conn = get_conn(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
```
Bağlanıp `SCHEMA` string'ini (tablo oluşturma SQL'leri) çalıştırır. Tablolar yoksa oluşturulur, varsa dokunulmaz (`IF NOT EXISTS`).

---

## 🪙 4. Coin İşlemleri: [upsert_coin](file:///c:/Users/bayra/Desktop/tel-pipline-scraping/scraping/db.py#101-132) (Satır 101–148)

```python
def upsert_coin(db_path: str, coin: dict) -> int:
```

### UPSERT Mantığı Nedir?
UPSERT, `"UPdate veya inSERT"` demektir. Kayıt yoksa ekle, zaten varsa alanlarını güncelle.

```python
cur = conn.execute("""
    INSERT INTO coins
        (symbol, name, coingecko_id, ...)
    VALUES (?, ?, ?, ...)
    ON CONFLICT(coingecko_id) DO UPDATE SET
        symbol         = excluded.symbol,
        name           = excluded.name,
        ...
""", (...))
```

Bu çok modern bir SQL desenidir (SQLite 3.24.0 ile eklendi):
1. `INSERT` yapmaya çalış.
2. `coingecko_id` sütununda bir çakışma (CONFLICT) olursa (çünkü `UNIQUE` yaptık).
3. Hata vermek yerine `DO UPDATE SET` ile mevcut satırı güncelle.
4. `excluded`, SQL'de `"yeni eklenmeye çalışılan ama reddedilen değer"` anlamına gelir.

```python
coin_id = cur.lastrowid or _get_coin_id(conn, coin["coingecko_id"])
```
- Ortalıkta yeni bir satır oluşturulduysa `cur.lastrowid` ID döner.
- Sadece güncelleme yapıldıysa SQLite `lastrowid` döndürmeyebilir (versiyona göre). O yüzden `None` kalırsa [_get_coin_id](file:///c:/Users/bayra/Desktop/tel-pipline-scraping/scraping/db.py#134-139) ile ID sorgulanır, **çok güvenli** bir desendir.

---

## 🗂️ 5. Ham İçerik (Raw Content) ve Tekilleştirme (Satır 187–249)

```python
def save_content(db_path: str, coin_id: int, item: dict) -> int | None:
```

Elde edilen haberi veritabanına yazar. Ama asıl işlevi **aynı haberi iki kere yazmamaktır (Dedup)**.

### Adım 1: Hash Kontrolü
```python
for h in [item.get("url_hash"), item.get("title_hash")]:
    if h and _hash_seen(conn, h):
        conn.close()
        return None
```
Hem URL hash'ini hem de Başlık hash'ini kontrol eder. Eğer bu hash'lerden **herhangi biri** `seen_hashes` tablosunda varsa, bu haber daha önce kaydedilmiştir. Fonksiyon anında `None` dönerek eklemeyi reddeder.

### Adım 2: İçeriği Kaydet
```python
cur = conn.execute("""
    INSERT INTO raw_content (...) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
""", (...))
```

### Adım 3: Hash'leri Gelecek İçin İşaretle
```python
for h in [item.get("url_hash"), item.get("title_hash")]:
    if h:
        _mark_hash(conn, h, now)
```
Eğer haber eklendiyse, sahip olduğu `url_hash` ve `title_hash` değerleri `seen_hashes` tablosuna yazılır. Böylece bir dahaki sefere adım 1'de yakalanır.

### Yardımcı Hash Fonksiyonları
```python
def _hash_seen(conn: sqlite3.Connection, h: str) -> bool:
    return conn.execute("SELECT 1 FROM seen_hashes WHERE hash = ?", (h,)).fetchone() is not None

def _mark_hash(conn: sqlite3.Connection, h: str, ts: str):
    conn.execute("INSERT OR IGNORE INTO seen_hashes (hash, first_seen) VALUES (?, ?)", (h, ts))
```
- `SELECT 1`: Veriyi çekmez, sadece "böyle bir satır var mı" diye bakar, performansı artırır.
- `INSERT OR IGNORE`: Satır zaten varsa hata vermez, sessizce yok sayar.

> [!TIP]
> **Neden Hash kullanıyoruz?** Bir URL veya haber başlığı yüzlerce karakter (hatta megabaytlarca) uzunluğunda olabilir. Bunları olduğu gibi veritabanında aramak yavaştır. Bunların kriptografik özetini (örn. 32 karakterlik MD5 veya 64 karakterlik SHA-256) oluşturup bunlarda arama yapmak ise anında sonuçlanır. İlerleyen kodlarda muhtemelen `dedup.py` dosyasında bu string'leri hash'leyen fonksiyonları göreceğiz.

---

## 📈 6. Trend Verileri (Satır 256–282)

```python
def save_trends(db_path: str, coin_id: int, keyword: str, series: list[dict]):
```

```python
for point in series:
    conn.execute("""
        INSERT OR REPLACE INTO trends_data
            (coin_id, keyword, date, value, fetched_at)
        VALUES (?, ?, ?, ?, ?)
    """, (coin_id, keyword, point["date"], point["value"], now))
```

Burada **`INSERT OR REPLACE`** deseni görüyoruz. Bu, UPSERT'e alternatif olarak kullanılan daha eski/basit bir SQLite kalıbıdır.
- `UNIQUE(coin_id, keyword, date)` kuralını ihlal eden bir veri gelirse, eski satırı tamamen siler ve bu yeni değerlerle baştan yeni satır yazar.
- Trend puanlarında bir güncelleme (geçmişe dönük düzeltme) olduysa veritabanına otomatik yansır.

---

## 🎯 Öğrenilen SQL ve Python Desenleri

| Desen / Kavram | Nerede Kullanılıyor | Açıklama |
|----------------|---------------------|----------|
| **WAL Mode** | `PRAGMA journal_mode=WAL` | Okuma ve yazma işlemlerinin birbirini kilitlemesini önleyen performans ayarı. |
| **Row Factory** | `conn.row_factory` | Tablo dönüş tiplerini Index (0, 1) yerine isimli objeye (dict) çevirir. |
| **UPSERT** | `ON CONFLICT DO UPDATE` | Veri çakışmasında hata atmayıp mevcut satırı güncelleyen modern SQL kalıbı. |
| **Deduplication (Dedup)** | `seen_hashes` tablosu | Tekrar eden verilerin kriptografik hash'ler yoluyla O(1) hızında reddedilmesi. |
| **Parametrik Sorgu** | `.execute(..., (?, ?))` | Hiçbir zaman string f-format metoduyla veritabanına değişken geçilmemesi kuralı (SQL Injection Koruması). |
| **Idempotence** | [process_coin()](file:///c:/Users/bayra/Desktop/tel-pipline-scraping/scraping/collect.py#184-328) içinde çağrılar | Aynı coin'i veya haberi 10 kere de çalıştırsanız, `UPSERT` ve `Dedup` sayesinde veritabanı bozulmaz veya çoğalmaz. Bu, sistemin stabil çalışmasını sağlar. |

# Collectors (Veri Toplayıcılar) Kod İncelemesi

Bu klasör (`scraping/collectors`), bir kripto para veya proje için internet üzerindeki çeşitli kaynaklardan (haber, duyuru, trend vb.) otomatik olarak veri toplayan (scrape eden) modülleri içerir. Klasörde 3 adet temel dosya bulunmaktadır: `events.py`, `news.py` ve `trends.py`. Aşağıda bu dosyaların nasıl çalıştığı adım adım ve öğretici bir dille anlatılmıştır.

---

## 1. `events.py` (Etkinlik ve Duyuru Toplayıcısı)

Bu modül, projelerin resmi kanallarından yapılan duyuruları ve etkinlikleri toplamayı amaçlar. Temel olarak iki farklı kaynaktan veri çeker: Telegram kanalları ve Resmi Web Siteleri/Bloglar.

### A. Telegram Collector
`fetch_telegram` ve `_fetch_telegram_async` fonksiyonları, **Telethon** kütüphanesini kullanarak belirli bir Telegram kanalındaki (örneğin duyuru kanalları) son mesajları asenkron olarak çeker.
- **Nasıl Çalışır?** Telegram'a bir API kimliğiyle bağlanır, belirtilen kanal geçmişine gider ve verilen gün sayısına (`days_back`) kadar olan mesajları alır.
- **Dedup (Veri Tekilleştirme):** Toplanan her mesaj için `compute_hashes` fonksiyonu ile başlık ve URL üzerinden bir "hash" (benzersiz kimlik) üretilir. Böylece aynı mesajın veritabanına defalarca kaydedilmesi önlenir.

### B. Website / Blog Collector
`fetch_website` fonksiyonu, projenin web sitesindeki blog veya haber içeriklerini çeker. Akıllı bir "3 Aşamalı Strateji" izler:
1. **RSS Feed (En Hafif Yöntem):** Önce sitenin `/feed`, `/rss` gibi yaygın RSS adreslerini tarar (`_try_rss`). Eğer RSS bulunursa, veriyi parse etmek çok kolaydır ve doğrudan sonuç döndürür. Javascript yüklenmesine gerek kalmaz.
2. **Playwright ile Scraping (Gelişmiş Yöntem):** Eğer RSS yoksa ve site JavaScript ile render edilen modern bir siteyse (örneğin React/Vue/Nextjs gibi SPA siteleri), bir "Headless Browser" (görünmez tarayıcı) olan **Playwright** devreye girer (`_scrape_with_playwright`). Tarayıcı siteyi açar, ağ isteklerinin bitmesini bekler, sayfadaki linkleri (`<a>`) bulur ve url'inde veya metninde blog/haber ibaresi geçen haberleri çeker. Görselleri ve analiz scriptlerini indirmez (hız kazanmak için iptal eder).
3. **HTML Fallback (Basit Yöntem):** Eğer Playwright kurulu değilse son çare olarak sayfayı direkt `requests` ve `BeautifulSoup` (`_scrape_html_fallback`) ile şablon olarak indirip içindeki haber başlıklarını ve linkleri spesifik CSS etiketleriyle (örn: `.news a`, `.blog a`) seçmeye çalışır.

---

## 2. `news.py` (Haber Toplayıcısı)

Bu modül, dış kaynaklardan (özellikle **CryptoPanic API**) bir kripto para ile ilgili en son gelişmeleri ve haberleri çeker.

### Nasıl Çalışır?
- **API İsteği:** `fetch` fonksiyonu, parametre olarak verilen `symbol` (örneğin PEPE) ve bir `auth_token` anahtarı ile CryptoPanic API'sine bağlanarak parametre olarak coinin sembolünü iletir.
- **Sayfalama (Pagination):** Sonuçlar tek seferde gelmeyebileceği için `pages` değişkeni ile belirtilen sayfa sayısı (varsayılan 3 sayfa) kadar dönerek sonraki (`next`) URL'lerden verileri sayfa sayfa alır. API engellemesin diye sayfalar arasında `time.sleep` ile ufak beklemeler yapar.
- **Rate Limit (Hız Sınırı) Koruması:** Eğer API `429 Too Many Requests` (çok fazla istek attınız) hatası verirse, sistem çökmez. API'den gelen HTTP cevap başlıklarındaki `Retry-After` (şu kadar saniye bekle formülü) değerini alır, o kadar süre kod duraklatılır ve tekrar denenir.
- **Veri Normalize Etme:** API'den gelen karmaşık veri, `_parse_item` fonksiyonunda projede kullanılan ortak sözlük formatına (`data_type=news`, `url`, `title`, `published_at` vs.) dönüştürülür. Çekilen haberlerin yine `url_hash` ve `title_hash` verileri oluşturulur.

---

## 3. `trends.py` (Google Trends Toplayıcısı)

Bu modülün amacı, Google Trends verilerini kullanarak kripto paranın veya projenin son günlerdeki aranma hacmini/popülerliğini izlemektir. Resmi Google API'si için popüler ve ücretsiz olan **`pytrends`** kütüphanesini kullanır.

### Nasıl Çalışır?
- **Anahtar Kelime Türetme (Keyword Generation):** Sadece "PEPE" kelimesini aratmak yerine, `generate_keywords` fonksiyonu ("PEPE coin", "PEPE crypto" gibi) birden fazla doğru kelime varyasyonu üretir. Böylece daha hatasız bir hacim ölçülür.
- **Trend İstekleri:** `fetch` ana fonksiyonu içerisinde, oluşturulan her anahtar kelime için trend araması başlatılır. `timeframe="now 7-d"` ile Google'dan aranan coinin son 7 gününün saatlik verisi dilim dilim istenir.
- **Google Bot Korumasını Atlatma:** Google algoritmaları botları çok çabuk tespit edip captcha ya da 429 engeli koyduğu için;
  - İstek atarken standart ve modern bir tarayıcının `User-Agent` bilgisi kullanılır, böylece normal bir insan gibi görünülür.
  - Her kelime işleminden önce mutlaka rastgele sürelerde beklenir (`random.uniform(8, 18)` saniye).
  - Rate limit'e takılırsa, kod 90 saniye artı rastgele bir gecikme (jitter) ekleyerek bekler ve işlemi (`_RETRIES` sınırınca) sakince tekrar dener.
- **Benzer Sorgular (Related Queries):** `fetch_related` yardımcı fonksiyonu Google Trends'te coin aratılırken kullanıcıların onun yanında daha başka neler aradığını listeler (Örnek: "PEPE coin nereden alınır" vs). Bunlar yükselişte olan (`rising`) ve en çok aranan (`top`) kelime gruplarıdır. Bu sayede coinin etrafında oluşan güncel olaylar tahmin edilebilir.

### Genel Özet
Bu üç dosya, sistemin "veri toplama antenleri" görevi görür:
- **`events.py`**: İçeriden ve en sağlam kaynaklardan (projenin kendi Telegramı veya Resmi Web Sitesi) veri çeker.
- **`news.py`**: Dış dünyadan, sektörel medya ve web sitelerinden (CryptoPanic) üçüncü parti gelişmeleri takip eder.
- **`trends.py`**: İnsanlardan, yani halk tabanındaki ilgi seviyesinden (Google Trends) istatistikleri ve aranma hype'ını ölçer.
Bu üç yapı da çektiği veriyi, işlenmek üzere aynı veri yapılandırmasında (Python dictionary listesi) ana sisteme geri döndürür.

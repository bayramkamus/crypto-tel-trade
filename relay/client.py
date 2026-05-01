"""
Relay Client — Collector Tarafı
=================================
Collector bu client'ı kullanarak event'leri relay API'ye gönderir.
Yerel SQLite yerine HTTP POST ile buffer'a yazar.

Kullanım:
    client = RelayClient(base_url="http://sunucu:8642", token="xxx")
    result = await client.send_events([event1, event2, ...])
"""

import logging
from typing import Optional

import httpx

log = logging.getLogger("relay.client")

# Batch gönderim boyutu (çok büyük payload'ları önler)
MAX_BATCH_SIZE = 200

# HTTP timeout (saniye)
REQUEST_TIMEOUT = 30.0


class RelayClient:
    """Async HTTP client — collector'dan relay API'ye event gönderir."""

    def __init__(self, base_url: str, token: str):
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=REQUEST_TIMEOUT,
            )
        return self._client

    async def send_events(self, events: list[dict]) -> dict:
        """
        Event listesini batch halinde relay API'ye gönderir.
        Her event dict formatında:
            {channel_id, channel_name, message_id, timestamp,
             message_text, sender_id, reply_to, views, forwards,
             extracted_ticker}

        Döner: {"accepted": int, "duplicates": int}
        """
        if not events:
            return {"accepted": 0, "duplicates": 0}

        client = await self._get_client()
        total_accepted = 0
        total_duplicates = 0

        # Büyük listeyi batch'lere böl
        for i in range(0, len(events), MAX_BATCH_SIZE):
            batch = events[i:i + MAX_BATCH_SIZE]
            try:
                resp = await client.post(
                    "/v1/events",
                    json={"events": batch},
                )
                resp.raise_for_status()
                data = resp.json()
                total_accepted += data.get("accepted", 0)
                total_duplicates += data.get("duplicates", 0)

            except httpx.HTTPStatusError as e:
                log.error(
                    f"Relay API HTTP hatası: {e.response.status_code} — "
                    f"{e.response.text[:200]}"
                )
                raise
            except httpx.RequestError as e:
                log.error(f"Relay API bağlantı hatası: {e}")
                raise

        log.info(
            f"Relay'e gönderildi: {total_accepted} kabul, "
            f"{total_duplicates} duplicate "
            f"(toplam {len(events)} event)"
        )
        return {"accepted": total_accepted, "duplicates": total_duplicates}

    async def send_single_event(self, event: dict) -> bool:
        """
        Tek bir event gönderir.
        Başarılıysa True, duplicate ise False döner.
        """
        result = await self.send_events([event])
        return result.get("accepted", 0) > 0

    async def health_check(self) -> dict:
        """Relay sunucu sağlık kontrolü."""
        client = await self._get_client()
        try:
            resp = await client.get("/v1/health")
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            log.error(f"Health check başarısız: {e}")
            return {"status": "error", "detail": str(e)}

    async def close(self):
        """Client'ı kapat."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

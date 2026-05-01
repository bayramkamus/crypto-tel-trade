#!/usr/bin/env python3
"""
Telegram Canlı Toplayıcı — Başlatma Noktası
=============================================
Tek komutla canli Telegram mesaj dinleyiciyi baslatir.

Kullanim:
    python run_collector.py                    # yerel mod: backfill + canli dinleme
    python run_collector.py --no-backfill      # sadece canli dinleme
    python run_collector.py --no-scraping      # sadece DB'ye yaz, scraping yapma
    python run_collector.py --db custom.db     # farkli DB yolu
    python run_collector.py --relay            # RELAY MODU: event'leri API'ye gönder
"""

import argparse
import asyncio
import os
import signal
import sys

from collector import LiveCollector


def main():
    parser = argparse.ArgumentParser(
        description="Telegram kanallarini surekli dinle, mesajlari topla"
    )
    parser.add_argument(
        "--no-backfill", action="store_true",
        help="Gecmis mesajlari cekme, sadece canli dinle",
    )
    parser.add_argument(
        "--no-scraping", action="store_true",
        help="Scraping pipeline'a mesaj iletme, sadece DB'ye yaz",
    )
    parser.add_argument(
        "--db", default=None,
        help="pump_research.db yolu (varsayilan: config.DB_PATH)",
    )
    parser.add_argument(
        "--relay", action="store_true",
        help="Relay modu: event'leri yerel DB yerine relay API'ye gönder",
    )
    parser.add_argument(
        "--relay-url", default=None,
        help="Relay API base URL (varsayilan: RELAY_BASE_URL env)",
    )
    parser.add_argument(
        "--relay-token", default=None,
        help="Relay API token (varsayilan: RELAY_TOKEN env)",
    )
    parser.add_argument(
        "--relay-batch-size", type=int, default=None,
        help="Relay batch gönderim boyutu (varsayılan: 50)",
    )
    args = parser.parse_args()

    # Relay ayarlarını env'den veya argümandan al
    relay_url = args.relay_url or os.environ.get("RELAY_BASE_URL", "")
    relay_token = args.relay_token or os.environ.get("RELAY_TOKEN", "")

    if args.relay and (not relay_url or not relay_token):
        print("HATA: Relay modu için RELAY_BASE_URL ve RELAY_TOKEN gerekli.")
        print("  --relay-url ve --relay-token argümanları veya")
        print("  RELAY_BASE_URL ve RELAY_TOKEN environment variable'ları ayarlayın.")
        sys.exit(1)

    # Banner
    mode = "RELAY" if args.relay else "YEREL"
    print()
    print("╔══════════════════════════════════════════════════════════╗")
    print(f"║       TELEGRAM CANLI MESAJ TOPLAYICI [{mode:^6s}]          ║")
    print("║       Backfill + Canli Dinleme + Scraping               ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print()

    collector = LiveCollector(
        db_path=args.db,
        do_backfill=not args.no_backfill,
        forward_scraping=not args.no_scraping,
        relay_mode=args.relay,
        relay_url=relay_url if args.relay else None,
        relay_token=relay_token if args.relay else None,
        relay_batch_size=args.relay_batch_size,
    )

    # Graceful shutdown
    def _shutdown(sig, frame):
        collector.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Asyncio event loop
    try:
        asyncio.run(collector.start())
    except KeyboardInterrupt:
        collector.stop()


if __name__ == "__main__":
    main()

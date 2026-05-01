"""
Coin Identity Resolver
========================
Verilen symbol icin CoinGecko'dan gercek kimlik bilgisini ceker.

- Birden fazla eslesme varsa uyari verir, market cap buyugunu alir
- Coin objesi: symbol, name, coingecko_id, contract, chain, market_cap_usd, ambiguous
"""

import time
import logging
import requests

log = logging.getLogger(__name__)

COINGECKO_SEARCH   = "https://api.coingecko.com/api/v3/search"
COINGECKO_COIN     = "https://api.coingecko.com/api/v3/coins/{id}"
COINGECKO_MARKETS  = "https://api.coingecko.com/api/v3/coins/markets"

_HEADERS = {"Accept": "application/json"}
_TIMEOUT = 12


def resolve(symbol: str) -> dict | None:
    """
    Symbol'u cozumle.
    Donen dict ornek:
      {
        "symbol":         "PEPE",
        "name":           "Pepe",
        "coingecko_id":   "pepe",
        "contract":       "0x6982508...",
        "chain":          "ethereum",
        "market_cap_usd": 4_200_000_000,
        "ambiguous":      False,
      }
    Bulunamazsa None doner.
    """
    symbol = symbol.upper().strip()
    log.info(f"[CoinResolver] {symbol} cozumleniyor...")

    # 1. CoinGecko search ile aday listesi
    candidates = _search_candidates(symbol)
    if not candidates:
        log.warning(f"[CoinResolver] {symbol} icin sonuc bulunamadi.")
        return None

    # 2. Market cap ile sirala, bire indir
    ambiguous = len(candidates) > 1
    if ambiguous:
        names = [f"{c['name']} ({c['id']})" for c in candidates[:5]]
        log.warning(
            f"[CoinResolver] ⚠  '{symbol}' icin {len(candidates)} eslesme bulundu: "
            f"{', '.join(names)}. "
            f"Market cap buyugu secildi: {candidates[0]['name']} ({candidates[0]['id']})"
        )

    best = candidates[0]

    # 3. Detayli bilgi cek (contract, chain, vb.)
    detail = _fetch_detail(best["id"])
    if not detail:
        # Sadece temel bilgiyle devam et
        return {
            "symbol":         symbol,
            "name":           best.get("name"),
            "coingecko_id":   best["id"],
            "contract":       None,
            "chain":          None,
            "market_cap_usd": best.get("market_cap"),
            "ambiguous":      ambiguous,
        }

    # En buyuk platform'dan contract al
    contract, chain = _extract_contract(detail)

    return {
        "symbol":         symbol,
        "name":           detail.get("name"),
        "coingecko_id":   detail["id"],
        "contract":       contract,
        "chain":          chain,
        "market_cap_usd": (detail.get("market_data") or {})
                          .get("market_cap", {}).get("usd"),
        "ambiguous":      ambiguous,
    }


# ─────────────────────────────────────────────────────────────────
# YARDIMCILAR
# ─────────────────────────────────────────────────────────────────

def _search_candidates(symbol: str) -> list[dict]:
    """
    CoinGecko search API ile symbol'e uyan coinleri bulur.
    Market cap sirasiyla doner.
    """
    try:
        r = requests.get(
            COINGECKO_SEARCH,
            params={"query": symbol},
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
        if r.status_code == 429:
            log.warning("[CoinResolver] Rate limit — 60s bekleniyor...")
            time.sleep(60)
            r = requests.get(COINGECKO_SEARCH,
                             params={"query": symbol},
                             headers=_HEADERS, timeout=_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.error(f"[CoinResolver] Search hatasi: {e}")
        return []

    # Sadece symbol tam eslesenleri al
    coins = [
        c for c in data.get("coins", [])
        if c.get("symbol", "").upper() == symbol
    ]

    if not coins:
        return []

    # Market cap rank ile sirala (kucuk rank = buyuk coin)
    coins.sort(key=lambda c: c.get("market_cap_rank") or 9999)

    # Market cap verisini eklemek icin markets endpoint
    ids = ",".join(c["id"] for c in coins[:10])
    try:
        time.sleep(1.2)
        mr = requests.get(
            COINGECKO_MARKETS,
            params={
                "vs_currency": "usd",
                "ids": ids,
                "order": "market_cap_desc",
                "per_page": 10,
                "page": 1,
            },
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
        if mr.status_code == 200:
            mc_map = {c["id"]: c.get("market_cap") for c in mr.json()}
            for c in coins:
                c["market_cap"] = mc_map.get(c["id"])
            coins.sort(key=lambda c: c.get("market_cap") or 0, reverse=True)
    except Exception:
        pass   # market cap siralama basarisiz, search sirasi kalir

    return coins


def _fetch_detail(coingecko_id: str) -> dict | None:
    """Coin detay endpoint'ini ceker."""
    try:
        time.sleep(1.2)
        r = requests.get(
            COINGECKO_COIN.format(id=coingecko_id),
            params={
                "localization": "false",
                "tickers": "false",
                "market_data": "true",
                "community_data": "false",
                "developer_data": "false",
            },
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
        if r.status_code == 429:
            log.warning("[CoinResolver] Rate limit — 60s bekleniyor...")
            time.sleep(60)
            r = requests.get(
                COINGECKO_COIN.format(id=coingecko_id),
                params={"localization":"false","tickers":"false",
                        "market_data":"true","community_data":"false",
                        "developer_data":"false"},
                headers=_HEADERS, timeout=_TIMEOUT,
            )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error(f"[CoinResolver] Detay cekme hatasi ({coingecko_id}): {e}")
        return None


def _extract_contract(detail: dict) -> tuple[str | None, str | None]:
    """
    detail['platforms'] sozlugundan en buyuk chain'i ve contract'i cikarir.
    Ornek: {"ethereum": "0x6982...", "bsc": "0xabc..."}
    Oncelik sirasi: ethereum, bsc, polygon, solana, diger
    """
    platforms: dict = detail.get("platforms") or {}
    if not platforms:
        return None, None

    priority = ["ethereum", "binance-smart-chain", "polygon-pos",
                "solana", "avalanche", "arbitrum-one", "optimism"]

    for chain in priority:
        if chain in platforms and platforms[chain]:
            return platforms[chain], chain

    # Oncelik listesinde yoksa ilk dolu platform'u al
    for chain, contract in platforms.items():
        if contract:
            return contract, chain

    return None, None


# ─────────────────────────────────────────────────────────────────
# TOPLU COZUMLEME
# ─────────────────────────────────────────────────────────────────

def resolve_many(symbols: list[str]) -> dict[str, dict | None]:
    """
    Birden fazla symbol'u sirayla cozumler.
    Doner: {symbol: coin_dict_or_None}
    """
    results = {}
    for i, sym in enumerate(symbols, 1):
        print(f"  [{i}/{len(symbols)}] {sym} cozumleniyor...")
        results[sym] = resolve(sym)
        if i < len(symbols):
            time.sleep(1.5)   # CoinGecko rate limit icin
    return results

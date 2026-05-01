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

import logging
import sqlite3
from datetime import datetime, timezone

log = logging.getLogger(__name__)

DB_PATH = "scraping_data.db"


# ─────────────────────────────────────────────────────────────────
# SCHEMA
# ─────────────────────────────────────────────────────────────────

SCHEMA = """
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

CREATE TABLE IF NOT EXISTS official_sources (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    coin_id     INTEGER NOT NULL REFERENCES coins(id),
    source_type TEXT NOT NULL,
    value       TEXT NOT NULL,
    UNIQUE(coin_id, source_type, value)
);

CREATE TABLE IF NOT EXISTS raw_content (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    coin_id      INTEGER NOT NULL REFERENCES coins(id),
    data_type    TEXT NOT NULL,
    source_name  TEXT NOT NULL,
    url          TEXT,
    title        TEXT,
    body         TEXT,
    url_hash     TEXT,
    title_hash   TEXT,
    published_at TEXT,
    fetched_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS seen_hashes (
    hash        TEXT PRIMARY KEY,
    first_seen  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trends_data (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    coin_id     INTEGER NOT NULL REFERENCES coins(id),
    keyword     TEXT NOT NULL,
    date        TEXT NOT NULL,
    value       INTEGER,
    fetched_at  TEXT NOT NULL,
    UNIQUE(coin_id, keyword, date)
);
"""


# ─────────────────────────────────────────────────────────────────
# BAGLANTI
# ─────────────────────────────────────────────────────────────────

def get_conn(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: str = DB_PATH):
    conn = get_conn(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    log.debug(f"DB hazir: {db_path}")


# ─────────────────────────────────────────────────────────────────
# COINS
# ─────────────────────────────────────────────────────────────────

def upsert_coin(db_path: str, coin: dict) -> int:
    """
    Coin'i kaydeder veya gunceller. Coin ID doner.
    coin dict: symbol, name, coingecko_id, contract, chain,
               market_cap_usd, ambiguous
    """
    conn = get_conn(db_path)
    now  = datetime.now(timezone.utc).isoformat()
    cur  = conn.execute("""
        INSERT INTO coins
            (symbol, name, coingecko_id, contract, chain,
             market_cap_usd, ambiguous, resolved_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(coingecko_id) DO UPDATE SET
            symbol         = excluded.symbol,
            name           = excluded.name,
            contract       = excluded.contract,
            chain          = excluded.chain,
            market_cap_usd = excluded.market_cap_usd,
            ambiguous      = excluded.ambiguous,
            resolved_at    = excluded.resolved_at
    """, (
        coin["symbol"], coin.get("name"), coin.get("coingecko_id"),
        coin.get("contract"), coin.get("chain"),
        coin.get("market_cap_usd"), 1 if coin.get("ambiguous") else 0,
        now,
    ))
    coin_id = cur.lastrowid or _get_coin_id(conn, coin["coingecko_id"])
    conn.commit()
    conn.close()
    return coin_id


def _get_coin_id(conn: sqlite3.Connection, coingecko_id: str) -> int:
    row = conn.execute(
        "SELECT id FROM coins WHERE coingecko_id = ?", (coingecko_id,)
    ).fetchone()
    return row["id"] if row else None


def get_coin_by_symbol(db_path: str, symbol: str) -> dict | None:
    conn = get_conn(db_path)
    row = conn.execute(
        "SELECT * FROM coins WHERE symbol = ? ORDER BY market_cap_usd DESC LIMIT 1",
        (symbol.upper(),)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


# ─────────────────────────────────────────────────────────────────
# OFFICIAL SOURCES
# ─────────────────────────────────────────────────────────────────

def save_sources(db_path: str, coin_id: int, sources: dict):
    """
    sources ornek:
      {"twitter": "@pepecoineth", "website": "https://...",
       "telegram": "t.me/...", "blog": "https://.../blog"}
    """
    conn = get_conn(db_path)
    for source_type, value in sources.items():
        if not value:
            continue
        conn.execute("""
            INSERT OR IGNORE INTO official_sources (coin_id, source_type, value)
            VALUES (?, ?, ?)
        """, (coin_id, source_type, value))
    conn.commit()
    conn.close()


def get_sources(db_path: str, coin_id: int) -> dict:
    conn = get_conn(db_path)
    rows = conn.execute(
        "SELECT source_type, value FROM official_sources WHERE coin_id = ?",
        (coin_id,)
    ).fetchall()
    conn.close()
    return {r["source_type"]: r["value"] for r in rows}


# ─────────────────────────────────────────────────────────────────
# RAW CONTENT
# ─────────────────────────────────────────────────────────────────

def save_content(db_path: str, coin_id: int, item: dict) -> int | None:
    """
    item dict: data_type, source_name, url, title, body,
               url_hash, title_hash, published_at
    Donen: yeni satir id, zaten varsa None
    """
    conn = get_conn(db_path)
    now  = datetime.now(timezone.utc).isoformat()

    # Dedup kontrolu (hash tablosu uzerinden)
    for h in [item.get("url_hash"), item.get("title_hash")]:
        if h and _hash_seen(conn, h):
            conn.close()
            return None

    cur = conn.execute("""
        INSERT INTO raw_content
            (coin_id, data_type, source_name, url, title, body,
             url_hash, title_hash, published_at, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        coin_id, item["data_type"], item["source_name"],
        item.get("url"), item.get("title"), item.get("body"),
        item.get("url_hash"), item.get("title_hash"),
        item.get("published_at"), now,
    ))

    # Hash'leri isaretle
    for h in [item.get("url_hash"), item.get("title_hash")]:
        if h:
            _mark_hash(conn, h, now)

    conn.commit()
    conn.close()
    return cur.lastrowid


def _hash_seen(conn: sqlite3.Connection, h: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM seen_hashes WHERE hash = ?", (h,)
    ).fetchone() is not None


def _mark_hash(conn: sqlite3.Connection, h: str, ts: str):
    conn.execute(
        "INSERT OR IGNORE INTO seen_hashes (hash, first_seen) VALUES (?, ?)",
        (h, ts)
    )


def get_content(db_path: str, coin_id: int,
                data_type: str = None, limit: int = 100) -> list:
    conn = get_conn(db_path)
    query = "SELECT * FROM raw_content WHERE coin_id = ?"
    params = [coin_id]
    if data_type:
        query += " AND data_type = ?"
        params.append(data_type)
    query += " ORDER BY published_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────────────────────────
# TRENDS DATA
# ─────────────────────────────────────────────────────────────────

def save_trends(db_path: str, coin_id: int, keyword: str,
                series: list[dict]):
    """
    series: [{"date": "2026-03-01", "value": 75}, ...]
    """
    conn = get_conn(db_path)
    now  = datetime.now(timezone.utc).isoformat()
    for point in series:
        conn.execute("""
            INSERT OR REPLACE INTO trends_data
                (coin_id, keyword, date, value, fetched_at)
            VALUES (?, ?, ?, ?, ?)
        """, (coin_id, keyword, point["date"], point["value"], now))
    conn.commit()
    conn.close()


def get_trends(db_path: str, coin_id: int) -> list:
    conn = get_conn(db_path)
    rows = conn.execute("""
        SELECT keyword, date, value
        FROM trends_data
        WHERE coin_id = ?
        ORDER BY keyword, date
    """, (coin_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

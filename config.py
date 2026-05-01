"""
Public configuration loader.

User-specific secrets are loaded from .env and channel names are loaded from
configs/channels.yml. Never commit .env or Telegram .session files to GitHub.
"""

import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _parse_simple_channels_yaml(path: Path) -> dict:
    """Small fallback parser for configs/channels.yml when PyYAML is absent."""
    data = {"channels": []}
    if not path.exists():
        return data

    current_key = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.endswith(":"):
            current_key = stripped[:-1]
            if current_key == "channels":
                data.setdefault("channels", [])
            continue
        if current_key == "channels" and stripped.startswith("-"):
            value = stripped[1:].strip().strip('"').strip("'")
            if value:
                data["channels"].append(value.lstrip("@"))
            continue
        if ":" in stripped:
            key, value = stripped.split(":", 1)
            data[key.strip()] = value.strip().strip('"').strip("'")
            current_key = None
    return data


def _load_channels_config(path: Path) -> dict:
    try:
        import yaml  # type: ignore
    except ImportError:
        return _parse_simple_channels_yaml(path)

    if not path.exists():
        return {"channels": []}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    channels = [
        str(item).strip().lstrip("@")
        for item in loaded.get("channels", [])
        if str(item).strip()
    ]
    loaded["channels"] = channels
    return loaded


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_channels() -> list[str]:
    raw = os.environ.get("TELEGRAM_CHANNELS", "")
    return [item.strip().lstrip("@") for item in raw.split(",") if item.strip()]


_load_dotenv(BASE_DIR / ".env")

CHANNELS_CONFIG = Path(os.environ.get("CHANNELS_CONFIG", "configs/channels.yml"))
if not CHANNELS_CONFIG.is_absolute():
    CHANNELS_CONFIG = BASE_DIR / CHANNELS_CONFIG
_channels_cfg = _load_channels_config(CHANNELS_CONFIG)

# Telegram API bilgileri: https://my.telegram.org
API_ID = _env_int("TELEGRAM_API_ID", 0)
API_HASH = os.environ.get("TELEGRAM_API_HASH", "")

# Telethon creates a .session file after first login.
# Treat it like a password and do not share it.
SESSION = os.environ.get("TELEGRAM_SESSION_NAME", "pump_research")

# Izlenecek kanallar: TELEGRAM_CHANNELS env veya configs/channels.yml
CHANNELS = _env_channels() or _channels_cfg.get("channels", [])

# Veritabani ve backfill ayarlari
DB_PATH = os.environ.get("DB_PATH", "pump_research.db")
BACKFILL_DAYS = _env_int("BACKFILL_DAYS", int(_channels_cfg.get("backfill_days", 30) or 30))
BACKFILL_BATCH_SIZE = _env_int("BACKFILL_BATCH_SIZE", 100)


def normalize_channel_id(raw_id) -> int:
    """
    Telethon channel ID'sini canonical formata cevirir.
    Canonical format: pozitif integer, Telegram -100 prefix'i olmadan.
    """
    raw_id = int(raw_id)
    if raw_id < 0:
        value = str(abs(raw_id))
        if value.startswith("100") and len(value) > 10:
            return int(value[3:])
        return abs(raw_id)
    return raw_id

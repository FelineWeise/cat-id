import os

from dotenv import load_dotenv

load_dotenv()


def _parse_csv_env(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name, "")
    values = [item.strip() for item in raw.split(",") if item.strip()]
    return values or default


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


APP_ENV = os.getenv("APP_ENV", "development").strip().lower()
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:8000").strip()
SPOTIFY_REDIRECT_URI = os.getenv(
    "SPOTIFY_REDIRECT_URI",
    f"{APP_BASE_URL.rstrip('/')}/api/spotify/callback",
).strip()

SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()
LASTFM_API_KEY = os.getenv("LASTFM_API_KEY", "").strip()

ALLOWED_ORIGINS = _parse_csv_env(
    "ALLOWED_ORIGINS",
    ["http://localhost:8000", "https://localhost:8000"],
)
ENABLE_DEBUG_ENDPOINT = _bool_env("ENABLE_DEBUG_ENDPOINT", APP_ENV != "production")

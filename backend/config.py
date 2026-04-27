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
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:8000").strip().rstrip("/")
SPOTIFY_REDIRECT_DERIVED_FROM_APP_BASE = f"{APP_BASE_URL}/api/spotify/callback"
_explicit_redirect = os.getenv("SPOTIFY_REDIRECT_URI", "").strip()
SPOTIFY_REDIRECT_URI = (
    _explicit_redirect.rstrip("/")
    if _explicit_redirect
    else SPOTIFY_REDIRECT_DERIVED_FROM_APP_BASE
)

SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()
LASTFM_API_KEY = os.getenv("LASTFM_API_KEY", "").strip()
SESSION_STORE_BACKEND = os.getenv("SESSION_STORE_BACKEND", "memory").strip().lower()
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "3600").strip() or "3600")
REDIS_URL = os.getenv("REDIS_URL", "").strip()

ALLOWED_ORIGINS = _parse_csv_env(
    "ALLOWED_ORIGINS",
    ["http://localhost:8000", "https://localhost:8000"],
)
ENABLE_DEBUG_ENDPOINT = _bool_env("ENABLE_DEBUG_ENDPOINT", APP_ENV != "production")


def _validate_runtime_config() -> None:
    if APP_ENV not in {"development", "production"}:
        raise RuntimeError("APP_ENV must be either 'development' or 'production'.")

    if APP_ENV == "production":
        if not APP_BASE_URL:
            raise RuntimeError("APP_BASE_URL must be set in production.")
        if not APP_BASE_URL.startswith("https://"):
            raise RuntimeError("APP_BASE_URL must start with https:// in production.")
        if not SPOTIFY_REDIRECT_URI.startswith("https://"):
            raise RuntimeError("SPOTIFY_REDIRECT_URI must start with https:// in production.")
        if SPOTIFY_REDIRECT_URI != SPOTIFY_REDIRECT_DERIVED_FROM_APP_BASE:
            raise RuntimeError(
                "SPOTIFY_REDIRECT_URI must exactly match APP_BASE_URL + '/api/spotify/callback' in production."
            )
        if ENABLE_DEBUG_ENDPOINT:
            raise RuntimeError("ENABLE_DEBUG_ENDPOINT must be false in production.")
        if SESSION_STORE_BACKEND == "redis" and not REDIS_URL:
            raise RuntimeError("REDIS_URL must be set when SESSION_STORE_BACKEND=redis.")


_validate_runtime_config()

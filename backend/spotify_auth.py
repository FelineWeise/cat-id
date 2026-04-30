"""Spotify OAuth PKCE flow for user-scoped actions (playlists, queue)."""

import base64
import hashlib
import logging
import secrets
import time
from urllib.parse import urlencode

import httpx
import requests
import spotipy
from requests.adapters import HTTPAdapter
from spotipy.oauth2 import SpotifyOAuth
from urllib3.util import Retry

from backend.config import (
    SPOTIFY_CLIENT_ID,
    SPOTIFY_CLIENT_SECRET,
    SPOTIFY_REDIRECT_URI,
)

logger = logging.getLogger(__name__)

SCOPES = (
    "playlist-modify-public playlist-modify-private playlist-read-private "
    "user-modify-playback-state user-read-playback-state user-read-private "
    "streaming"
)


def build_oauth_manager(cache_handler: spotipy.CacheHandler | None = None) -> SpotifyOAuth:
    return SpotifyOAuth(
        client_id=SPOTIFY_CLIENT_ID,
        client_secret=SPOTIFY_CLIENT_SECRET,
        redirect_uri=SPOTIFY_REDIRECT_URI,
        scope=SCOPES,
        cache_handler=cache_handler,
        show_dialog=False,
    )


def get_authorize_url(state: str | None = None) -> str:
    """Return the Spotify authorization URL the browser should redirect to."""
    oauth = build_oauth_manager()
    params = {
        "client_id": oauth.client_id,
        "response_type": "code",
        "redirect_uri": oauth.redirect_uri,
        "scope": oauth.scope,
    }
    if state:
        params["state"] = state
    return f"https://accounts.spotify.com/authorize?{urlencode(params)}"


def build_pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def get_authorize_url_pkce(state: str, code_challenge: str) -> str:
    params = {
        "client_id": SPOTIFY_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": SPOTIFY_REDIRECT_URI,
        "scope": SCOPES,
        "state": state,
        "code_challenge_method": "S256",
        "code_challenge": code_challenge,
    }
    return f"https://accounts.spotify.com/authorize?{urlencode(params)}"


def normalize_token_expiry(token_info: dict) -> dict:
    """Spotify returns expires_in but spotipy's is_token_expired expects expires_at."""
    out = dict(token_info)
    if "expires_at" not in out and "expires_in" in out:
        out["expires_at"] = int(time.time()) + int(out["expires_in"])
    return out


def exchange_code(code: str, code_verifier: str | None = None) -> dict:
    """Exchange an authorization code for token info dict."""
    if code_verifier:
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": SPOTIFY_REDIRECT_URI,
            "client_id": SPOTIFY_CLIENT_ID,
            "code_verifier": code_verifier,
        }
        with httpx.Client(timeout=10) as client:
            response = client.post("https://accounts.spotify.com/api/token", data=payload)
            response.raise_for_status()
            token_info = response.json()
        return normalize_token_expiry(token_info)
    oauth = build_oauth_manager()
    raw = oauth.get_access_token(code, as_dict=True, check_cache=False)
    return normalize_token_expiry(raw)


def _spotify_user_http_session() -> requests.Session:
    """HTTP session for user OAuth calls.

    Spotipy's default urllib3 :class:`spotipy.util.Retry` treats 429 as retryable even when
    ``retries=0``, which still runs one ``increment()`` per response — logging util warnings
    and ``Max Retries reached`` for *every* throttled request while not helping quota.

    We use urllib3's plain :class:`~urllib3.util.retry.Retry` with **429 excluded** and
    ``respect_retry_after_header=False`` so 429 returns immediately to our handlers (no
    multi-hour sleeps inside urllib3). Transient 5xx still retry a few times.
    """
    session = requests.Session()
    retry = Retry(
        total=6,
        connect=2,
        read=False,
        redirect=False,
        status=3,
        backoff_factor=0.35,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST", "PUT", "DELETE"]),
        respect_retry_after_header=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def get_user_client(access_token: str) -> spotipy.Spotify:
    """Return a Spotify client authenticated with the user's access token."""
    return spotipy.Spotify(
        auth=access_token,
        requests_session=_spotify_user_http_session(),
        # Retries are configured on the mounted HTTPAdapter, not Spotipy's default session.
        retries=0,
        status_retries=0,
    )


def refresh_pkce_access_token(refresh_token: str) -> dict:
    """Refresh tokens issued via PKCE (body params only; no Basic client_secret header)."""
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": SPOTIFY_CLIENT_ID,
    }
    with httpx.Client(timeout=10) as client:
        response = client.post(
            "https://accounts.spotify.com/api/token",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response.raise_for_status()
        return normalize_token_expiry(response.json())


def refresh_if_needed(token_info: dict) -> dict:
    """Refresh the access token if expired. Returns updated token_info."""
    token_info = normalize_token_expiry(token_info)
    oauth = build_oauth_manager()
    expired = "expires_at" not in token_info or oauth.is_token_expired(token_info)
    if not expired:
        return token_info
    refresh_token = token_info.get("refresh_token")
    if not refresh_token:
        raise ValueError("Spotify token expired and no refresh_token is stored")
    updated = refresh_pkce_access_token(refresh_token)
    if "refresh_token" not in updated:
        updated["refresh_token"] = refresh_token
    return updated

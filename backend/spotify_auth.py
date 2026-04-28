"""Spotify OAuth Authorization Code flow for user-scoped actions (playlists, queue)."""

import logging
from urllib.parse import urlencode

import spotipy
from spotipy.oauth2 import SpotifyOAuth

from backend.config import (
    SPOTIFY_CLIENT_ID,
    SPOTIFY_CLIENT_SECRET,
    SPOTIFY_REDIRECT_URI,
)

logger = logging.getLogger(__name__)

SCOPES = "playlist-modify-public playlist-modify-private user-modify-playback-state"


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


def exchange_code(code: str) -> dict:
    """Exchange an authorization code for token info dict."""
    oauth = build_oauth_manager()
    return oauth.get_access_token(code, as_dict=True, check_cache=False)


def get_user_client(access_token: str) -> spotipy.Spotify:
    """Return a Spotify client authenticated with the user's access token."""
    return spotipy.Spotify(
        auth=access_token,
        retries=0,
        status_retries=0,
        backoff_factor=0,
    )


def refresh_if_needed(token_info: dict) -> dict:
    """Refresh the access token if expired. Returns updated token_info."""
    oauth = build_oauth_manager()
    if oauth.is_token_expired(token_info):
        token_info = oauth.refresh_access_token(token_info["refresh_token"])
    return token_info

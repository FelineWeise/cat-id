import asyncio
import logging
import secrets
from collections.abc import Sequence

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

logging.basicConfig(level=logging.INFO)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend.deezer import fetch_track_info as deezer_fetch
from backend.config import (
    ALLOWED_ORIGINS,
    APP_BASE_URL,
    APP_ENV,
    ENABLE_DEBUG_ENDPOINT,
    SPOTIFY_CLIENT_ID,
    SPOTIFY_REDIRECT_DERIVED_FROM_APP_BASE,
    SPOTIFY_REDIRECT_URI,
)
from backend.lastfm import fetch_track_tags, get_similar_tracks, get_track_tags
from backend.models import AudioSimilarRequest, SimilarTracksResponse, TrackInfo, TrackRequest
from backend.spotify import (
    build_recommendation_targets,
    compute_similarity,
    get_audio_features,
    get_recommendations,
    get_track_info,
    search_track,
)
from backend.spotify_auth import (
    exchange_code,
    get_authorize_url,
    get_user_client,
    refresh_if_needed,
)
from backend.tag_categories import (
    INSTRUMENTAL_TAGS,
    VOCAL_TAGS,
    build_tag_categories,
    estimate_features_from_tags,
    normalize_tag,
)

app = FastAPI(
    title="Follow Your Cat ID",
    description="Find songs similar to a given Spotify track using Last.fm collaborative filtering.",
    version="2.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Always over-fetch from Last.fm so the client has a large pool to filter.
OVERFETCH_LIMIT = 50
MAX_ENRICH_CONCURRENCY = 10
MIN_FETCH_MULTIPLIER = 2
MAX_FETCH_MULTIPLIER = 3
FULL_TAG_ENRICH_LIMIT = 25

logger = logging.getLogger(__name__)
_shared_http_client: httpx.AsyncClient | None = None


def _get_http_client() -> httpx.AsyncClient:
    global _shared_http_client
    if _shared_http_client is None:
        _shared_http_client = httpx.AsyncClient(timeout=10)
    return _shared_http_client


@app.on_event("startup")
async def startup_checks() -> None:
    """Log OAuth callback config and highlight insecure setups."""
    logger.info("App environment: %s", APP_ENV)
    logger.info("App base URL: %s", APP_BASE_URL)
    logger.info("Allowed origins: %s", ALLOWED_ORIGINS)
    logger.info("Spotify redirect URI: %s", SPOTIFY_REDIRECT_URI)
    if SPOTIFY_REDIRECT_URI != SPOTIFY_REDIRECT_DERIVED_FROM_APP_BASE:
        logger.warning(
            "SPOTIFY_REDIRECT_URI differs from APP_BASE_URL default (%s). "
            "Ensure Spotify Developer Dashboard lists the redirect URI exactly (scheme, host, path, no stray slash).",
            SPOTIFY_REDIRECT_DERIVED_FROM_APP_BASE,
        )
    if not SPOTIFY_REDIRECT_URI.startswith("https://"):
        logger.warning(
            "SPOTIFY_REDIRECT_URI is not HTTPS. Spotify may reject authorize requests "
            "with 'redirect_uri: Insecure'."
        )


@app.on_event("shutdown")
async def shutdown_clients() -> None:
    global _shared_http_client
    if _shared_http_client is not None:
        await _shared_http_client.aclose()
        _shared_http_client = None


async def _enrich_lastfm(
    seed: TrackInfo, limit: int, exclude: set[str] | None = None,
) -> tuple[list[TrackInfo], list[str]]:
    """Shared Last.fm pipeline: fetch similar tracks, enrich with Spotify/Deezer/tags.

    Mutates *seed* in-place (adds bpm, tags, preview_url).
    Returns (enriched_tracks, seed_tags).
    """
    primary_artist = seed.artists[0]
    fetch_limit = limit
    if exclude:
        fetch_limit = min(limit * MIN_FETCH_MULTIPLIER, 250)

    lastfm_results, seed_tags = await asyncio.gather(
        asyncio.to_thread(get_similar_tracks, primary_artist, seed.name, fetch_limit),
        asyncio.to_thread(get_track_tags, primary_artist, seed.name),
    )

    if exclude:
        filtered_results = [
            r for r in lastfm_results
            if f"{r['artist']}::{r['name']}".lower() not in exclude
        ]
        if len(filtered_results) < limit and fetch_limit < 250:
            expanded = min(limit * MAX_FETCH_MULTIPLIER, 250)
            expanded_results = await asyncio.to_thread(
                get_similar_tracks, primary_artist, seed.name, expanded,
            )
            filtered_results = [
                r for r in expanded_results
                if f"{r['artist']}::{r['name']}".lower() not in exclude
            ]
        lastfm_results = filtered_results
    lastfm_results = lastfm_results[:limit]

    client = _get_http_client()
    seed_deezer = await deezer_fetch(client, primary_artist, seed.name)
    seed.bpm = seed_deezer.get("bpm")
    seed.tags = seed_tags
    if not seed.preview_url:
        seed.preview_url = seed_deezer.get("preview")

    semaphore = asyncio.Semaphore(MAX_ENRICH_CONCURRENCY)

    async def enrich(item: dict, include_tags: bool) -> TrackInfo | None:
        artist_name = item["artist"]
        track_name = item["name"]
        match_score = item["match"]

        async with semaphore:
            try:
                sp_track, dz_info = await asyncio.gather(
                    asyncio.to_thread(search_track, artist_name, track_name),
                    deezer_fetch(client, artist_name, track_name),
                )
                tags: list[str] = []
                if include_tags:
                    tags = await fetch_track_tags(client, artist_name, track_name)
            except Exception:
                logger.warning("Failed to enrich '%s - %s'", artist_name, track_name)
                return None

        if sp_track:
            sp_track.match_score = match_score
            sp_track.bpm = dz_info.get("bpm")
            sp_track.tags = [normalize_tag(tag) for tag in tags]
            if not sp_track.preview_url:
                sp_track.preview_url = dz_info.get("preview")
            return sp_track

        return TrackInfo(
            name=track_name,
            artists=[artist_name],
            album="",
            album_art=item.get("image"),
            preview_url=dz_info.get("preview"),
            spotify_url=None,
            match_score=match_score,
            bpm=dz_info.get("bpm"),
            tags=[normalize_tag(tag) for tag in tags],
        )

    top_batch = lastfm_results[:FULL_TAG_ENRICH_LIMIT]
    tail_batch = lastfm_results[FULL_TAG_ENRICH_LIMIT:]
    top_results = await asyncio.gather(*(enrich(item, include_tags=True) for item in top_batch))
    tail_results = await asyncio.gather(*(enrich(item, include_tags=False) for item in tail_batch))
    results = [*top_results, *tail_results]

    return [r for r in results if r is not None], [normalize_tag(tag) for tag in seed_tags]


def _track_tag_set(track: TrackInfo) -> set[str]:
    return {normalize_tag(tag) for tag in (track.tags or [])}


def _instrumental_bias_score(track: TrackInfo) -> float:
    tags = _track_tag_set(track)
    if not tags:
        return 0.0
    has_instrumental = bool(tags & INSTRUMENTAL_TAGS)
    has_vocal = bool(tags & VOCAL_TAGS)
    if has_instrumental and not has_vocal:
        return 1.0
    if has_instrumental and has_vocal:
        return 0.5
    if has_vocal:
        return 0.0
    return 0.25


@app.post("/api/similar", response_model=SimilarTracksResponse)
async def api_similar(req: TrackRequest):
    try:
        seed = await asyncio.to_thread(get_track_info, req.url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Spotify API error: {exc}")

    exclude = set(req.exclude) if req.exclude else None
    try:
        similar, seed_tags = await _enrich_lastfm(seed, OVERFETCH_LIMIT, exclude)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Last.fm API error: {exc}")

    all_tags: set[str] = set(seed_tags)
    for t in similar:
        all_tags.update(t.tags or [])
    tag_categories = build_tag_categories(list(all_tags))

    return SimilarTracksResponse(
        seed_track=seed,
        similar_tracks=similar,
        seed_tags=seed_tags,
        tag_categories=tag_categories,
    )


@app.post("/api/similar/audio", response_model=SimilarTracksResponse)
async def api_similar_audio(req: AudioSimilarRequest):
    """Audio similarity with weighted dimensions.

    Tries Spotify audio-features + recommendations first.
    Falls back to Last.fm candidates + tag-estimated features when the
    Spotify endpoints return 403 (restricted app).
    """
    # 1. Resolve seed track
    try:
        seed = await asyncio.to_thread(get_track_info, req.url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Spotify API error: {exc}")

    if not seed.spotify_id:
        raise HTTPException(status_code=400, detail="Could not resolve a Spotify track ID.")

    # 2. Try Spotify audio-features for the seed
    seed_features = None
    spotify_available = False
    try:
        features_map = await asyncio.to_thread(get_audio_features, [seed.spotify_id])
        seed_features = features_map.get(seed.spotify_id)
        spotify_available = seed_features is not None
    except ValueError:
        logger.info("Spotify audio-features unavailable, falling back to tag estimation")
    except Exception as exc:
        logger.warning("Audio features fetch failed: %s", exc)

    if spotify_available:
        return await _audio_spotify_path(seed, seed_features, req)

    return await _audio_fallback_path(seed, req)


async def _audio_spotify_path(
    seed: TrackInfo, seed_features, req: AudioSimilarRequest,
) -> SimilarTracksResponse:
    """Spotify-native path: recommendations + real audio features."""
    seed.audio_features = seed_features
    seed.bpm = seed_features.tempo

    targets = build_recommendation_targets(seed_features, req.weights)

    try:
        candidates = await asyncio.to_thread(
            get_recommendations, seed.spotify_id, targets, min(req.limit * 2, 100),
        )
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Recommendations error: {exc}")

    if not candidates:
        return SimilarTracksResponse(seed_track=seed, similar_tracks=[])

    candidate_ids = [t.spotify_id for t in candidates if t.spotify_id]
    try:
        cand_features = await asyncio.to_thread(get_audio_features, candidate_ids)
    except Exception:
        cand_features = {}

    for track in candidates:
        af = cand_features.get(track.spotify_id) if track.spotify_id else None
        track.audio_features = af
        if af:
            track.bpm = af.tempo
            track.match_score = compute_similarity(seed_features, af, req.weights)
        else:
            track.match_score = 0.0

    candidates.sort(key=lambda t: t.match_score or 0, reverse=True)
    return SimilarTracksResponse(
        seed_track=seed, similar_tracks=candidates[: req.limit],
    )


async def _audio_fallback_path(
    seed: TrackInfo, req: AudioSimilarRequest,
) -> SimilarTracksResponse:
    """Fallback: Last.fm candidates + tag-estimated features + weighted scoring."""
    exclude = set(req.exclude) if req.exclude else None
    try:
        similar, seed_tags = await _enrich_lastfm(seed, OVERFETCH_LIMIT, exclude)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Last.fm API error: {exc}")

    seed.audio_features = estimate_features_from_tags(seed.tags or [], seed.bpm)
    seed_features = seed.audio_features

    for track in similar:
        track.audio_features = estimate_features_from_tags(track.tags or [], track.bpm)
        if track.audio_features and seed_features:
            track.match_score = compute_similarity(seed_features, track.audio_features, req.weights)

    similar.sort(
        key=lambda t: ((t.match_score or 0), _instrumental_bias_score(t)),
        reverse=True,
    )

    all_tags: set[str] = set(seed_tags)
    for t in similar:
        all_tags.update(t.tags or [])

    return SimilarTracksResponse(
        seed_track=seed,
        similar_tracks=similar[: req.limit],
        seed_tags=seed_tags,
        tag_categories=build_tag_categories(list(all_tags)),
        approximated=True,
    )


@app.get("/api/health")
def health():
    return {"status": "ok"}


if ENABLE_DEBUG_ENDPOINT:
    @app.get("/api/debug/tags")
    def debug_tags(artist: str, track: str):
        """Diagnostic endpoint: test Last.fm tag fetching for a single track."""
        import httpx as _httpx
        from backend.config import LASTFM_API_KEY as _key

        params = {
            "method": "track.gettoptags",
            "artist": artist,
            "track": track,
            "api_key": _key,
            "format": "json",
            "autocorrect": 1,
        }
        resp = _httpx.get("https://ws.audioscrobbler.com/2.0/", params=params, timeout=10)
        raw = resp.json()
        parsed = get_track_tags(artist, track)
        return {"raw_response": raw, "parsed_tags": parsed}


# ---------------------------------------------------------------------------
# Spotify OAuth + user-scoped actions (playlist, queue)
# ---------------------------------------------------------------------------

# In-memory token store keyed by a simple session id (cookie).
# For production, use a proper session backend.
_token_store: dict[str, dict] = {}


@app.get("/api/spotify/oauth-config")
def spotify_oauth_config():
    """Public values the app sends to Spotify OAuth (for debugging redirect_uri mismatch)."""
    return {
        "app_base_url": APP_BASE_URL,
        "redirect_uri": SPOTIFY_REDIRECT_URI,
        "redirect_uri_derived_from_app_base": SPOTIFY_REDIRECT_DERIVED_FROM_APP_BASE,
        "spotify_client_id": SPOTIFY_CLIENT_ID,
    }


@app.get("/api/spotify/login")
def spotify_login():
    """Redirect browser to Spotify authorization page."""
    state = secrets.token_urlsafe(16)
    url = get_authorize_url(state=state)
    resp = RedirectResponse(url=url)
    resp.set_cookie("sp_state", state, httponly=True, secure=True, samesite="lax", max_age=300)
    return resp


@app.get("/api/spotify/callback")
def spotify_callback(request: Request, code: str = "", error: str = "", state: str = ""):
    saved_state = request.cookies.get("sp_state", "")
    if error:
        return RedirectResponse(url=f"/?spotify_error={error}")
    if not code or state != saved_state:
        return RedirectResponse(url="/?spotify_error=state_mismatch")

    try:
        token_info = exchange_code(code)
    except Exception as exc:
        logging.error("Spotify token exchange failed: %s", exc)
        return RedirectResponse(url="/?spotify_error=token_exchange_failed")

    session_id = secrets.token_urlsafe(32)
    _token_store[session_id] = token_info

    resp = RedirectResponse(url="/?spotify_connected=1")
    resp.set_cookie("sp_session", session_id, httponly=True, secure=True, samesite="lax", max_age=3600)
    resp.delete_cookie("sp_state")
    return resp


@app.get("/api/spotify/status")
def spotify_status(request: Request):
    """Check whether the user has a valid Spotify session."""
    session_id = request.cookies.get("sp_session", "")
    token_info = _token_store.get(session_id)
    if not token_info:
        return {"connected": False}
    try:
        token_info = refresh_if_needed(token_info)
        _token_store[session_id] = token_info
        sp = get_user_client(token_info["access_token"])
        user = sp.current_user()
        return {"connected": True, "user": user.get("display_name") or user.get("id")}
    except Exception:
        return {"connected": False}


@app.post("/api/spotify/logout")
def spotify_logout(request: Request):
    session_id = request.cookies.get("sp_session", "")
    _token_store.pop(session_id, None)
    resp = JSONResponse(content={"ok": True})
    resp.delete_cookie("sp_session")
    return resp


def _get_user_sp(request: Request) -> "spotipy.Spotify":
    session_id = request.cookies.get("sp_session", "")
    token_info = _token_store.get(session_id)
    if not token_info:
        raise HTTPException(status_code=401, detail="Not connected to Spotify. Please log in.")
    token_info = refresh_if_needed(token_info)
    _token_store[session_id] = token_info
    return get_user_client(token_info["access_token"])


class PlaylistRequest(BaseModel):
    name: str = Field(default="Similar Tracks", max_length=200)
    track_uris: list[str] = Field(min_length=1)
    public: bool = False


@app.post("/api/spotify/playlist")
def create_playlist(req: PlaylistRequest, request: Request):
    """Create a Spotify playlist and add the given tracks."""
    sp = _get_user_sp(request)
    user_id = sp.current_user()["id"]
    playlist = sp.user_playlist_create(user_id, req.name, public=req.public)
    for i in range(0, len(req.track_uris), 100):
        sp.playlist_add_items(playlist["id"], req.track_uris[i : i + 100])
    return {
        "playlist_id": playlist["id"],
        "playlist_url": playlist["external_urls"]["spotify"],
    }


class QueueRequest(BaseModel):
    track_uris: list[str] = Field(min_length=1)


def _err_text(exc: Exception) -> str:
    return str(exc).lower()


def _is_no_active_device_error(exc: Exception) -> bool:
    text = _err_text(exc)
    return "no active device found" in text


def _is_restricted_device_error(exc: Exception) -> bool:
    text = _err_text(exc)
    return "restricted device" in text


def _is_auth_error(exc: Exception) -> bool:
    text = _err_text(exc)
    return "token" in text or "expired" in text or "unauthorized" in text


def _pick_transfer_device(sp) -> str | None:
    try:
        payload = sp.devices()
    except Exception:
        return None
    devices = payload.get("devices", []) if isinstance(payload, dict) else []
    if not devices:
        return None

    # Prefer currently active -> unrestricted -> first available.
    active = next((d for d in devices if d.get("is_active")), None)
    if active and active.get("id"):
        return active["id"]
    unrestricted = next((d for d in devices if not d.get("is_restricted")), None)
    if unrestricted and unrestricted.get("id"):
        return unrestricted["id"]
    fallback = next((d for d in devices if d.get("id")), None)
    return fallback.get("id") if fallback else None


def _recover_player_once(sp) -> tuple[bool, str | None]:
    device_id = _pick_transfer_device(sp)
    if not device_id:
        return False, "No Spotify devices found. Open Spotify on a phone/desktop/web player first."
    try:
        # Keep playback paused state unchanged where possible.
        sp.transfer_playback(device_id=device_id, force_play=False)
        return True, None
    except Exception as exc:
        return False, f"Could not activate a Spotify device automatically: {exc}"


def _classify_queue_error(exc: Exception) -> dict[str, str | bool]:
    text = _err_text(exc)
    if _is_no_active_device_error(exc):
        return {
            "code": "NO_ACTIVE_DEVICE",
            "message": "No active Spotify device found. Open Spotify on phone/desktop/web player and try again.",
            "retryable": True,
        }
    if _is_restricted_device_error(exc):
        return {
            "code": "RESTRICTED_DEVICE",
            "message": "The selected Spotify device does not allow queue changes. Switch to another active device and try again.",
            "retryable": True,
        }
    if _is_auth_error(exc):
        return {
            "code": "AUTH_REQUIRED",
            "message": "Spotify session expired. Reconnect Spotify and try again.",
            "retryable": True,
        }
    return {
        "code": "SPOTIFY_API_ERROR",
        "message": f"Spotify rejected queue request: {text[:220]}",
        "retryable": False,
    }


def _queue_error_payload(uri: str, exc: Exception) -> dict[str, str | bool]:
    detail = _classify_queue_error(exc)
    return {
        "uri": uri,
        "reason": detail["message"],
        "code": detail["code"],
        "message": detail["message"],
        "retryable": detail["retryable"],
    }


def _queue_status_for_errors(errors: Sequence[dict[str, str | bool]]) -> int:
    retryable_codes = {"NO_ACTIVE_DEVICE", "RESTRICTED_DEVICE", "AUTH_REQUIRED"}
    if any((error.get("code") in retryable_codes) for error in errors):
        return 409
    return 502


@app.post("/api/spotify/queue")
def add_to_queue(req: QueueRequest, request: Request):
    """Add tracks to the user's Spotify playback queue."""
    sp = _get_user_sp(request)
    errors = []
    added = 0
    recovered_player = False

    for uri in req.track_uris:
        try:
            sp.add_to_queue(uri)
            added += 1
        except Exception as exc:
            recoverable = _is_no_active_device_error(exc) or _is_restricted_device_error(exc)
            if recoverable and not recovered_player:
                ok, recovery_error = _recover_player_once(sp)
                recovered_player = True
                if ok:
                    try:
                        sp.add_to_queue(uri)
                        added += 1
                        continue
                    except Exception as retry_exc:
                        errors.append(_queue_error_payload(uri, retry_exc))
                        continue
                if recovery_error:
                    errors.append({
                        "uri": uri,
                        "reason": recovery_error,
                        "code": "NO_ACTIVE_DEVICE",
                        "message": recovery_error,
                        "retryable": True,
                    })
                else:
                    errors.append(_queue_error_payload(uri, exc))
                continue
            errors.append(_queue_error_payload(uri, exc))

    if added == 0 and errors:
        status_code = _queue_status_for_errors(errors)
        raise HTTPException(
            status_code=status_code,
            detail={
                "code": "QUEUE_FAILED",
                "message": "Could not add tracks to queue.",
                "retryable": status_code == 409,
                "errors": errors,
            },
        )
    message = (
        f"Added {added} track(s) to your queue."
        if not errors
        else f"Added {added} track(s), failed {len(errors)}."
    )
    return {"added": added, "failed": len(errors), "errors": errors, "message": message}


app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")

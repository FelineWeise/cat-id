import asyncio
import logging
import random
import re
import secrets
import time
from collections.abc import Sequence
from difflib import SequenceMatcher
from urllib.parse import urlencode

import httpx
import requests
import spotipy
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

logging.basicConfig(level=logging.INFO)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend.deezer import fetch_track_info as deezer_fetch
from backend.metadata_fallback import (
    fetch_musicbrainz_hints,
    fetch_musicbrainz_spotify_relation_id,
)
from backend.config import (
    ALLOWED_ORIGINS,
    APP_BASE_URL,
    APP_ENV,
    ENABLE_DEBUG_ENDPOINT,
    REDIS_URL,
    SESSION_COOKIE_SECURE,
    SESSION_STORE_BACKEND,
    SESSION_TTL_SECONDS,
    SPOTIFY_CLIENT_ID,
    SPOTIFY_REDIRECT_DERIVED_FROM_APP_BASE,
    SPOTIFY_REDIRECT_URI,
)
from backend.lastfm import fetch_track_tags, get_similar_tracks, get_track_tags
from backend.models import (
    AudioSimilarRequest,
    AudioWeights,
    PlaylistSeedSummary,
    PlaylistLookupItem,
    SimilarTracksResponse,
    SimilarityFilters,
    TextPlaylistCreateRequest,
    TextPlaylistCreateResponse,
    TextPlaylistUnmatched,
    TrackInfo,
    TrackRequest,
    UnifiedSimilarRequest,
)
from backend.link_aggregator import resolve_external_links
from backend.audio_analysis import fetch_analysis_metrics
from backend.spotify import (
    build_recommendation_targets,
    compute_similarity,
    get_spotify_client,
    get_audio_features,
    get_recommendations,
    get_track_info,
    resolve_spotify_track_with_source,
    spotify_mapping_allowed,
)
from backend.spotify_auth import (
    build_pkce_pair,
    exchange_code,
    get_authorize_url,
    get_authorize_url_pkce,
    get_user_client,
    refresh_if_needed,
)
from backend.session_store import build_session_store
from backend.tag_categories import (
    INSTRUMENTAL_TAGS,
    VOCAL_TAGS,
    build_tag_categories,
    estimate_features_from_tags,
    normalize_tag,
    tag_alignment_score,
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

BASE_OVERFETCH_FACTOR = 2
MAX_OVERFETCH_LIMIT = 120
MAX_ENRICH_CONCURRENCY = 8
MIN_FETCH_MULTIPLIER = 2
MAX_FETCH_MULTIPLIER = 3
FULL_TAG_ENRICH_LIMIT = 18
ENRICH_TIME_BUDGET_SECONDS = 8.0
SPOTIFY_RESOLVE_BUDGET = 48
MB_FALLBACK_CAP = 18
STRICT_MAPPED_FETCH_MULT = 5
EXTERNAL_LINKS_ENRICH_CAP = 20
SPOTIFY_ENRICH_SEED_WEIGHT = 0.7
TAG_ALIGNMENT_WEIGHT = 0.25
DEEZER_SIGNAL_WEIGHT = 0.05
SEED_PROFILE_SCORE_WEIGHT = 0.35
PLAYLIST_FETCH_CAP = 120
PLAYLIST_SEED_SAMPLE_CAP = 12
BPM_INFERENCE_DEFAULT_RANGE = (95.0, 125.0)
BPM_INFERENCE_TAG_RANGES: dict[str, tuple[float, float]] = {
    # Lower/laid-back tendencies
    "ambient": (60.0, 95.0),
    "chill": (70.0, 105.0),
    "downtempo": (75.0, 108.0),
    "neo soul": (75.0, 112.0),
    "soul": (72.0, 110.0),
    "rnb": (70.0, 110.0),
    "r&b": (70.0, 110.0),
    # Mid-tempo pop/indie tendencies
    "pop": (90.0, 128.0),
    "indie": (85.0, 125.0),
    "alternative": (82.0, 126.0),
    # Higher-energy dance/club tendencies
    "house": (118.0, 132.0),
    "dance": (110.0, 132.0),
    "edm": (118.0, 140.0),
    "electronic": (108.0, 136.0),
    "garage": (124.0, 138.0),
    "dnb": (160.0, 176.0),
    "drum and bass": (160.0, 176.0),
    "jungle": (158.0, 176.0),
    "techno": (122.0, 142.0),
    "trap": (130.0, 160.0),
    "hip hop": (80.0, 108.0),
    "hip-hop": (80.0, 108.0),
    "rap": (78.0, 110.0),
}

logger = logging.getLogger(__name__)

# Cap Retry-After we echo to clients (Spotify sometimes returns very large values).
_SPOTIFY_RETRY_AFTER_HEADER_CAP = 3600


def _parse_retry_after_header(headers: dict | None) -> int | None:
    if not headers:
        return None
    raw = headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return max(1, int(str(raw).strip()))
    except ValueError:
        return None


def _is_spotify_rate_limit_error(exc: BaseException) -> bool:
    if isinstance(exc, spotipy.SpotifyException) and getattr(exc, "http_status", None) == 429:
        return True
    msg = str(exc).lower()
    if isinstance(exc, requests.exceptions.RetryError):
        return "429" in msg or "too many 429" in msg
    return "429" in msg and "too many" in msg


def _single_spotify_search(
    sp, q: str
) -> tuple[dict | None, bool, int | None]:
    """One search call; returns (payload or None, rate_limited, retry_after_seconds or None). Never raises."""
    try:
        payload = sp.search(q=q, type="track", limit=1, market="from_token")
        if isinstance(payload, dict):
            return payload, False, None
        return None, False, None
    except spotipy.SpotifyException as exc:
        if exc.http_status == 429:
            ra = _parse_retry_after_header(exc.headers)
            logger.warning("Spotify search rate limited (spotipy): retry_after=%s", ra)
            return None, True, ra
        logger.info("Spotify search failed: %s", exc)
        return None, False, None
    except (requests.exceptions.RetryError, OSError) as exc:
        if _is_spotify_rate_limit_error(exc):
            logger.warning("Spotify search rate limited (retries exhausted): %s", exc)
            return None, True, None
        logger.info("Spotify search error: %s", exc)
        return None, False, None
    except Exception as exc:
        if _is_spotify_rate_limit_error(exc):
            logger.warning("Spotify search rate limited: %s", exc)
            return None, True, None
        logger.info("Spotify search unexpected error: %s", exc)
        return None, False, None


_shared_http_client: httpx.AsyncClient | None = None
session_store = build_session_store(SESSION_STORE_BACKEND, REDIS_URL)
_EFFECTIVE_SESSION_BACKEND = getattr(session_store, "backend_key", SESSION_STORE_BACKEND)
_NON_ALNUM = re.compile(r"[^a-z0-9]+", re.I)
_SPOTIFY_PLAYLIST_URL_RE = re.compile(
    r"(?:https?://)?open\.spotify\.com/playlist/([A-Za-z0-9]{22})"
)
_SPOTIFY_PLAYLIST_URI_RE = re.compile(r"spotify:playlist:([A-Za-z0-9]{22})")


def _listener_fetch_limit(limit: int, strict_mapped_only: bool) -> int:
    """How many Last.fm candidates to pull before enrichment (strict mode needs more raw rows)."""
    if strict_mapped_only:
        return min(
            MAX_OVERFETCH_LIMIT,
            max(limit * STRICT_MAPPED_FETCH_MULT, limit * BASE_OVERFETCH_FACTOR),
        )
    return min(MAX_OVERFETCH_LIMIT, max(limit, limit * BASE_OVERFETCH_FACTOR))


def _build_similar_response(
    *,
    seed: TrackInfo,
    similar_ranked: list[TrackInfo],
    limit: int,
    strict_mapped_only: bool,
    seed_tags: list[str],
    tag_categories: dict[str, str],
    mapping_degraded_reason: str | None = None,
    external_links_degraded_reason: str | None = None,
    approximated: bool = False,
    playlist_id: str | None = None,
    playlist_sampled_seeds: list[PlaylistSeedSummary] | None = None,
) -> SimilarTracksResponse:
    """Slice ranked results; when strict_mapped_only, drop tracks without Spotify IDs."""
    seeded_ranked = sorted(
        similar_ranked,
        key=lambda track: (
            (track.match_score or 0.0) + (_seed_profile_score(seed, track) * SEED_PROFILE_SCORE_WEIGHT),
            _instrumental_bias_score(track),
            1.0 if track.preview_url else 0.0,
        ),
        reverse=True,
    )
    total_candidates = len(seeded_ranked)
    if strict_mapped_only:
        filtered = [t for t in seeded_ranked if t.spotify_id][:limit]
    else:
        filtered = seeded_ranked[:limit]
    mapped_count, unmapped_count = _mapping_summary(filtered)
    mapping_source_counts: dict[str, int] = {}
    mapping_used_user_token = False
    for track in filtered:
        if not track.spotify_id:
            continue
        src = track.mapping_source or "unknown"
        mapping_source_counts[src] = mapping_source_counts.get(src, 0) + 1
        if src.startswith("user_"):
            mapping_used_user_token = True
    return SimilarTracksResponse(
        seed_track=seed,
        similar_tracks=filtered,
        strict_mapped_only=strict_mapped_only,
        total_candidates=total_candidates,
        mapped_count=mapped_count,
        unmapped_count=unmapped_count,
        mapping_used_user_token=mapping_used_user_token,
        mapping_source_counts=mapping_source_counts,
        mapping_degraded_reason=mapping_degraded_reason,
        external_links_degraded_reason=external_links_degraded_reason,
        seed_tags=seed_tags,
        tag_categories=tag_categories,
        approximated=approximated,
        playlist_id=playlist_id,
        playlist_sampled_seeds=playlist_sampled_seeds or [],
    )


def _normalized_text(value: str) -> str:
    lowered = (value or "").strip().lower()
    lowered = _NON_ALNUM.sub(" ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def _seed_profile_score(seed: TrackInfo, track: TrackInfo) -> float:
    tag_score = tag_alignment_score(seed.tags or [], track.tags or [])
    bpm_score = 0.0
    if seed.bpm and track.bpm and seed.bpm > 0:
        bpm_diff_ratio = min(1.0, abs(seed.bpm - track.bpm) / seed.bpm)
        bpm_score = 1.0 - bpm_diff_ratio
    title_seed = _normalized_text(seed.name)
    title_track = _normalized_text(track.name)
    title_score = (
        SequenceMatcher(None, title_seed, title_track).ratio()
        if title_seed and title_track
        else 0.0
    )
    return (tag_score * 0.5) + (bpm_score * 0.35) + (title_score * 0.15)


def _extract_playlist_id(url_or_uri: str) -> str | None:
    raw = (url_or_uri or "").strip()
    if not raw:
        return None
    url_match = _SPOTIFY_PLAYLIST_URL_RE.search(raw)
    if url_match:
        return url_match.group(1)
    uri_match = _SPOTIFY_PLAYLIST_URI_RE.search(raw)
    if uri_match:
        return uri_match.group(1)
    return None


def _spotify_exception_detail(exc: spotipy.SpotifyException) -> str:
    status = getattr(exc, "http_status", None)
    reason = getattr(exc, "reason", None)
    code = getattr(exc, "code", None)
    return f"http_status={status}, code={code}, reason={reason}, message={str(exc)}"


def _truncate_text(value: str, max_len: int = 500) -> str:
    text = (value or "").strip()
    if len(text) <= max_len:
        return text
    return f"{text[:max_len]}...(truncated)"


def _clamp_bpm(value: float) -> float:
    return max(45.0, min(220.0, value))


def _infer_bpm_from_track_context(track: TrackInfo) -> float | None:
    # Keep this inference conservative and only as a final fallback.
    # We fuse tag priors + available analysis signals into a bounded estimate.
    tags = {normalize_tag(tag) for tag in (track.tags or []) if normalize_tag(tag)}
    ranges: list[tuple[float, float]] = [
        BPM_INFERENCE_TAG_RANGES[tag]
        for tag in tags
        if tag in BPM_INFERENCE_TAG_RANGES
    ]
    if not ranges:
        ranges = [BPM_INFERENCE_DEFAULT_RANGE]
    low = sum(r[0] for r in ranges) / len(ranges)
    high = sum(r[1] for r in ranges) / len(ranges)
    estimate = (low + high) / 2.0

    metrics = track.analysis_metrics or {}
    danceability = metrics.get("danceability")
    energy = metrics.get("energy")
    instrumentalness = (
        metrics.get("instrumentalness")
        if isinstance(metrics.get("instrumentalness"), (int, float))
        else (
            track.audio_features.instrumentalness
            if track.audio_features and isinstance(track.audio_features.instrumentalness, (int, float))
            else None
        )
    )
    if isinstance(danceability, (int, float)):
        estimate += (float(danceability) - 0.5) * 12.0
    if isinstance(energy, (int, float)):
        estimate += (float(energy) - 0.5) * 10.0
    if isinstance(instrumentalness, (int, float)):
        estimate += (float(instrumentalness) - 0.5) * 6.0

    bounded = min(max(estimate, low), high)
    return _clamp_bpm(bounded)


def _spotify_client_bearer_token(sp) -> str | None:
    token = getattr(sp, "_auth", None)
    if isinstance(token, str) and token.strip():
        return token.strip()
    auth_manager = getattr(sp, "auth_manager", None)
    if auth_manager is None:
        return None
    try:
        token_payload = auth_manager.get_access_token(as_dict=True)
    except TypeError:
        token_payload = auth_manager.get_access_token()
    except Exception:
        return None
    if isinstance(token_payload, dict):
        candidate = token_payload.get("access_token")
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    if isinstance(token_payload, str) and token_payload.strip():
        return token_payload.strip()
    return None


def _playlist_items_via_items_endpoint(
    sp,
    playlist_id: str,
    offset: int,
    limit: int,
) -> dict:
    token = _spotify_client_bearer_token(sp)
    if not token:
        raise ValueError("No bearer token available for Spotify items endpoint.")
    params = {
        "offset": offset,
        "limit": limit,
        "fields": "items(track(id,linked_from(id),name,artists(name),album(name,images,release_date),preview_url,external_urls,popularity),item(id,linked_from(id),name,artists(name),album(name,images,release_date),preview_url,external_urls,popularity)),next",
        "additional_types": "track",
    }
    url = f"https://api.spotify.com/v1/playlists/{playlist_id}/items?{urlencode(params)}"
    response = requests.get(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
        timeout=20,
    )
    if response.status_code >= 400:
        body_preview = _truncate_text(response.text or "")
        raise spotipy.SpotifyException(
            response.status_code,
            -1,
            f"{url}: {response.reason} body={body_preview}",
            headers=dict(response.headers),
        )
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Spotify items endpoint returned invalid payload.")
    return payload


def _playlist_item_to_track_info(item: dict) -> TrackInfo | None:
    track = None
    if isinstance(item, dict):
        # Common shape: {"track": {...}}
        if isinstance(item.get("track"), dict):
            track = item.get("track")
        # Alternative shape seen in some responses/tools: {"item": {...}}
        elif isinstance(item.get("item"), dict):
            nested = item.get("item")
            if isinstance(nested.get("track"), dict):
                track = nested.get("track")
            else:
                track = nested
        # Defensive fallback for any flattened track payload.
        elif item.get("id") and item.get("artists"):
            track = item
    if not isinstance(track, dict):
        return None
    track_id = track.get("id")
    if not track_id:
        linked_from = track.get("linked_from") if isinstance(track.get("linked_from"), dict) else {}
        track_id = linked_from.get("id")
    if not track_id:
        return None
    artists = [a.get("name", "") for a in (track.get("artists") or []) if isinstance(a, dict)]
    artists = [a for a in artists if a]
    if not artists:
        return None
    images = (track.get("album") or {}).get("images") or []
    album_art = images[0].get("url") if images and isinstance(images[0], dict) else None
    release_date = (track.get("album") or {}).get("release_date", "")
    release_year: int | None = None
    if isinstance(release_date, str) and release_date:
        try:
            release_year = int(release_date[:4])
        except ValueError:
            release_year = None
    return TrackInfo(
        name=str(track.get("name") or ""),
        artists=artists,
        album=str((track.get("album") or {}).get("name") or ""),
        album_art=album_art,
        preview_url=track.get("preview_url"),
        spotify_url=((track.get("external_urls") or {}).get("spotify")),
        spotify_id=track_id,
        popularity=track.get("popularity"),
        release_year=release_year,
        spotify_mapping_status="mapped",
        mapping_source="playlist_seed",
    )


def _playlist_sample_indices(total: int, sample_size: int) -> list[int]:
    if total <= 0 or sample_size <= 0:
        return []
    if total <= sample_size:
        return list(range(total))
    # Randomized sample per request so Reload generates a fresh playlist slice.
    return sorted(random.sample(range(total), sample_size))


def _fetch_playlist_seed_tracks(playlist_id: str, user_sp=None, max_tracks: int = 100) -> list[TrackInfo]:
    def fetch_with_client(sp, source_label: str) -> tuple[list[TrackInfo], list[str]]:
        diagnostics: list[str] = []
        # Probe playlist metadata first so we can fail early on true access errors.
        try:
            playlist_meta = sp.playlist(playlist_id, fields="id,public,owner(id),name")
            diagnostics.append(f"{source_label}:playlist_meta ok")
        except Exception as exc:
            diagnostics.append(f"{source_label}:playlist_meta failed {_truncate_text(str(exc), 300)}")
            raise
        items: list[TrackInfo] = []
        offset = 0
        while len(items) < max_tracks:
            page_size = min(50, max_tracks - len(items))
            try:
                payload = _playlist_items_via_items_endpoint(
                    sp,
                    playlist_id,
                    offset,
                    page_size,
                )
                diagnostics.append(f"{source_label}:items_endpoint ok offset={offset}")
            except Exception as exc:
                diagnostics.append(
                    f"{source_label}:items_endpoint failed offset={offset} {_truncate_text(str(exc), 500)}"
                )
                raise
            raw_items = payload.get("items", []) if isinstance(payload, dict) else []
            if not raw_items:
                break
            for row in raw_items:
                info = _playlist_item_to_track_info(row)
                if info is not None:
                    items.append(info)
            if not payload.get("next"):
                break
            offset += len(raw_items)
        return items, diagnostics

    errors: list[str] = []
    clients = [(user_sp, "user"), (get_spotify_client(), "app")]
    for client, source_label in clients:
        if client is None:
            continue
        try:
            tracks, diagnostics = fetch_with_client(client, source_label)
            if tracks:
                return tracks
            # If this client successfully executed the items endpoint path, do not
            # continue to fallback clients just because no usable tracks were parsed.
            # This avoids masking a valid user-token read with a later app-token 403.
            if any("items_endpoint ok" in d for d in diagnostics):
                return tracks
            if diagnostics:
                errors.extend(diagnostics)
        except Exception as exc:
            errors.append(f"{source_label}:failed {_truncate_text(str(exc), 700)}")
            continue
    if errors:
        raise RuntimeError(" | ".join(errors))
    return []


def _collect_playlist_recommendations(
    seeds: list[TrackInfo],
    req: UnifiedSimilarRequest,
    user_sp=None,
) -> list[TrackInfo]:
    if not seeds:
        return []
    effective_weights = _effective_weights_unified(
        req.weights, req.instrumental_similarity_only
    )
    recommendations_by_id: dict[str, TrackInfo] = {}
    source = "user" if user_sp is not None else "app"
    per_seed_limit = max(20, min(40, req.limit))
    for seed in seeds:
        if not seed.spotify_id:
            continue
        try:
            features = get_audio_features([seed.spotify_id], user_sp, source=source).get(seed.spotify_id)
        except Exception:
            features = None
        if features is None:
            continue
        targets = build_recommendation_targets(features, effective_weights)
        try:
            recs = get_recommendations(seed.spotify_id, targets, per_seed_limit, user_sp, source=source)
        except Exception:
            continue
        for track in recs:
            if not track.spotify_id:
                continue
            if track.spotify_id in recommendations_by_id:
                current = recommendations_by_id[track.spotify_id]
                if (track.match_score or 0.0) > (current.match_score or 0.0):
                    recommendations_by_id[track.spotify_id] = track
                continue
            recommendations_by_id[track.spotify_id] = track
    return list(recommendations_by_id.values())


async def _collect_playlist_listener_tracks(
    seeds: list[TrackInfo],
    req: UnifiedSimilarRequest,
    user_sp=None,
    *,
    use_metadata_fallback: bool,
) -> tuple[list[TrackInfo], list[str], str | None, str | None]:
    if not seeds:
        return [], [], None, None
    exclude = set(req.exclude) if req.exclude else None
    per_seed_limit = min(60, _listener_fetch_limit(req.limit, req.strict_mapped_only))

    async def run_for_seed(seed: TrackInfo):
        return await _enrich_lastfm(
            seed,
            per_seed_limit,
            exclude,
            use_metadata_fallback=use_metadata_fallback,
            user_sp=user_sp,
        )

    runs = await asyncio.gather(
        *(run_for_seed(seed) for seed in seeds),
        return_exceptions=True,
    )

    merged_by_key: dict[str, TrackInfo] = {}
    all_seed_tags: set[str] = set()
    mapping_reasons: set[str] = set()
    external_reasons: set[str] = set()

    for run in runs:
        if isinstance(run, Exception):
            continue
        similar, seed_tags, mapping_reason, external_reason = run
        all_seed_tags.update(seed_tags or [])
        if mapping_reason:
            mapping_reasons.add(mapping_reason)
        if external_reason:
            external_reasons.add(external_reason)
        for track in similar:
            key = f"{(track.artists[0] if track.artists else '').strip().lower()}::{track.name.strip().lower()}"
            current = merged_by_key.get(key)
            if current is None or (track.match_score or 0.0) > (current.match_score or 0.0):
                merged_by_key[key] = track

    mapping_degraded_reason = ",".join(sorted(mapping_reasons)) if mapping_reasons else None
    external_links_degraded_reason = ",".join(sorted(external_reasons)) if external_reasons else None
    return (
        list(merged_by_key.values()),
        sorted(all_seed_tags),
        mapping_degraded_reason,
        external_links_degraded_reason,
    )


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
    if APP_ENV == "production" and _EFFECTIVE_SESSION_BACKEND == "memory":
        logger.warning(
            "Effective session store is memory in production: OAuth sessions are per-process. "
            "Use SESSION_STORE_BACKEND=redis and REDIS_URL when running multiple workers or replicas, "
            "or /api/spotify/status may not see the session after callback."
        )


@app.on_event("shutdown")
async def shutdown_clients() -> None:
    global _shared_http_client
    if _shared_http_client is not None:
        await _shared_http_client.aclose()
        _shared_http_client = None


async def _enrich_lastfm(
    seed: TrackInfo,
    limit: int,
    exclude: set[str] | None = None,
    *,
    use_metadata_fallback: bool = True,
    user_sp=None,
) -> tuple[list[TrackInfo], list[str], str | None, str | None]:
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
    if not seed.album_art:
        seed.album_art = seed_deezer.get("album_art")

    seed_tag_list = [normalize_tag(tag) for tag in seed_tags]

    def fused_similarity_score(lastfm_match: float, tags: list[str], deezer_info: dict) -> float:
        seed_component = max(0.0, min(lastfm_match, 1.0)) * SPOTIFY_ENRICH_SEED_WEIGHT
        tag_component = tag_alignment_score(seed_tag_list, tags) * TAG_ALIGNMENT_WEIGHT
        deezer_component = (
            (0.5 if deezer_info.get("bpm") is not None else 0.0)
            + (0.5 if deezer_info.get("preview") else 0.0)
        ) * DEEZER_SIGNAL_WEIGHT
        return min(1.0, seed_component + tag_component + deezer_component)

    semaphore = asyncio.Semaphore(MAX_ENRICH_CONCURRENCY)
    enrich_deadline = time.monotonic() + ENRICH_TIME_BUDGET_SECONDS
    mapping_degraded_reason: str | None = None
    external_links_degraded_reason: str | None = None
    mapping_calls = 0
    mb_fallback_used = 0
    external_link_calls = 0

    async def enrich(item: dict, include_tags: bool) -> TrackInfo | None:
        nonlocal mapping_degraded_reason
        nonlocal mapping_calls
        nonlocal mb_fallback_used
        nonlocal external_link_calls
        nonlocal external_links_degraded_reason
        artist_name = item["artist"]
        track_name = item["name"]
        match_score = item["match"]

        async with semaphore:
            try:
                dz_info = await deezer_fetch(client, artist_name, track_name)
                candidate_isrc = dz_info.get("isrc") if isinstance(dz_info, dict) else None
                mapping_source_allowed = (
                    spotify_mapping_allowed("user")
                    if user_sp is not None
                    else spotify_mapping_allowed("app")
                )
                mapping_allowed = (
                    mapping_calls < SPOTIFY_RESOLVE_BUDGET
                    and time.monotonic() < enrich_deadline
                    and mapping_source_allowed
                )
                sp_track = None
                mapping_source: str | None = None
                if mapping_allowed:
                    mapping_calls += 1
                    sp_track, mapping_source = await asyncio.to_thread(
                        resolve_spotify_track_with_source,
                        artist_name,
                        track_name,
                        candidate_isrc,
                        user_sp=user_sp,
                        allow_app_fallback=user_sp is None,
                    )
                else:
                    if mapping_calls >= SPOTIFY_RESOLVE_BUDGET:
                        mapping_degraded_reason = "mapping_limit_reached"
                    elif not mapping_source_allowed:
                        mapping_degraded_reason = "spotify_mapping_rate_limited"
                    elif time.monotonic() >= enrich_deadline:
                        mapping_degraded_reason = "enrich_time_budget_exceeded"

                if (
                    sp_track is None
                    and use_metadata_fallback
                    and mb_fallback_used < MB_FALLBACK_CAP
                    and time.monotonic() < enrich_deadline
                ):
                    mb_spotify_id = await fetch_musicbrainz_spotify_relation_id(
                        client,
                        artist_name,
                        track_name,
                        candidate_isrc,
                    )
                    if mb_spotify_id and mapping_calls < SPOTIFY_RESOLVE_BUDGET:
                        mapping_calls += 1
                        sp_track, mapping_source = await asyncio.to_thread(
                            resolve_spotify_track_with_source,
                            artist_name,
                            track_name,
                            candidate_isrc,
                            user_sp=user_sp,
                            spotify_id_hint=mb_spotify_id,
                            allow_app_fallback=user_sp is None,
                        )

                if (
                    sp_track is None
                    and use_metadata_fallback
                    and mb_fallback_used < MB_FALLBACK_CAP
                    and time.monotonic() < enrich_deadline
                    and mapping_source_allowed
                ):
                    hints = await fetch_musicbrainz_hints(
                        client, artist_name, track_name, candidate_isrc,
                    )
                    mb_fallback_used += 1
                    hint_isrc, hint_artist, hint_title = hints
                    if (
                        any(h is not None for h in hints)
                        and mapping_calls < SPOTIFY_RESOLVE_BUDGET
                        and _should_retry_spotify_resolve(
                            artist_name,
                            track_name,
                            candidate_isrc,
                            hint_isrc,
                            hint_artist,
                            hint_title,
                        )
                    ):
                        mapping_calls += 1
                        sp_track, mapping_source = await asyncio.to_thread(
                            resolve_spotify_track_with_source,
                            hint_artist or artist_name,
                            hint_title or track_name,
                            hint_isrc or candidate_isrc,
                            user_sp=user_sp,
                            allow_app_fallback=user_sp is None,
                        )

                tags: list[str] = []
                if include_tags and time.monotonic() < enrich_deadline:
                    tags = await fetch_track_tags(client, artist_name, track_name)
            except Exception:
                logger.warning("Failed to enrich '%s - %s'", artist_name, track_name)
                return None

        normalized_tags = [normalize_tag(tag) for tag in tags]
        fused_score = fused_similarity_score(match_score, normalized_tags, dz_info)
        if sp_track:
            sp_track.match_score = fused_score
            sp_track.bpm = dz_info.get("bpm")
            sp_track.tags = normalized_tags
            sp_track.spotify_mapping_status = "mapped"
            sp_track.mapping_source = mapping_source
            if not sp_track.preview_url:
                sp_track.preview_url = dz_info.get("preview")
            if not sp_track.album_art:
                sp_track.album_art = dz_info.get("album_art")
            return sp_track

        external_links: dict[str, str] = {}
        external_primary_provider: str | None = None
        if (
            external_link_calls < EXTERNAL_LINKS_ENRICH_CAP
            and time.monotonic() < enrich_deadline
        ):
            external_link_calls += 1
            external_links, external_primary_provider = await resolve_external_links(
                client,
                artist=artist_name,
                title=track_name,
                isrc=candidate_isrc,
                deezer_url=dz_info.get("link") if isinstance(dz_info, dict) else None,
            )
        else:
            if external_link_calls >= EXTERNAL_LINKS_ENRICH_CAP:
                external_links_degraded_reason = "external_link_limit_reached"
            elif time.monotonic() >= enrich_deadline:
                external_links_degraded_reason = "enrich_time_budget_exceeded"

        return TrackInfo(
            name=track_name,
            artists=[artist_name],
            album="",
            album_art=dz_info.get("album_art") or item.get("image"),
            preview_url=dz_info.get("preview"),
            spotify_url=None,
            match_score=fused_score,
            bpm=dz_info.get("bpm"),
            tags=normalized_tags,
            external_links=external_links,
            external_primary_provider=external_primary_provider,
            spotify_mapping_status="unmapped",
        )

    top_batch = lastfm_results[:FULL_TAG_ENRICH_LIMIT]
    tail_batch = lastfm_results[FULL_TAG_ENRICH_LIMIT:]
    top_results = await asyncio.gather(*(enrich(item, include_tags=True) for item in top_batch))
    tail_results = await asyncio.gather(*(enrich(item, include_tags=False) for item in tail_batch))
    results = [*top_results, *tail_results]

    return (
        [r for r in results if r is not None],
        [normalize_tag(tag) for tag in seed_tags],
        mapping_degraded_reason,
        external_links_degraded_reason,
    )


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


def _fused_rank_value(track: TrackInfo) -> tuple[float, float, float]:
    mapping_boost = 0.05 if track.spotify_id else 0.0
    return (
        (track.match_score or 0.0) + mapping_boost,
        _instrumental_bias_score(track),
        1.0 if track.preview_url else 0.0,
    )


def _mapping_summary(tracks: Sequence[TrackInfo]) -> tuple[int, int]:
    mapped = sum(1 for track in tracks if track.spotify_id)
    return mapped, max(0, len(tracks) - mapped)


def _blend_track_lists(
    listener_tracks: list[TrackInfo],
    audio_tracks: list[TrackInfo],
    *,
    listener_weight: float = 0.55,
) -> list[TrackInfo]:
    by_key: dict[str, TrackInfo] = {}
    listener_scores: dict[str, float] = {}
    audio_scores: dict[str, float] = {}
    sources: dict[str, set[str]] = {}

    def track_key(track: TrackInfo) -> str:
        artist = track.artists[0] if track.artists else ""
        return f"{artist.strip().lower()}::{track.name.strip().lower()}"

    for track in listener_tracks:
        key = track_key(track)
        by_key[key] = track
        listener_scores[key] = float(track.match_score or 0.0)
        sources.setdefault(key, set()).add("listener")

    for track in audio_tracks:
        key = track_key(track)
        if key not in by_key:
            by_key[key] = track
        elif not by_key[key].spotify_id and track.spotify_id:
            by_key[key] = track
        audio_scores[key] = float(track.match_score or 0.0)
        sources.setdefault(key, set()).add("audio")

    blended: list[TrackInfo] = []
    for key, track in by_key.items():
        ls = listener_scores.get(key, 0.0)
        aps = audio_scores.get(key, 0.0)
        lw = max(0.0, min(1.0, listener_weight))
        aw = 1.0 - lw
        score = (ls * lw) + (aps * aw)
        track.match_score = score
        track.analysis_metrics = {
            **(track.analysis_metrics or {}),
            "listenerScore": ls,
            "audioScore": aps,
            "blendedScore": score,
            "sources": ",".join(sorted(sources.get(key, set()))),
        }
        blended.append(track)
    blended.sort(key=_fused_rank_value, reverse=True)
    return blended


def _apply_backend_filters(
    tracks: list[TrackInfo],
    filters: SimilarityFilters,
) -> list[TrackInfo]:
    filtered: list[TrackInfo] = []
    wanted_tags = {normalize_tag(tag) for tag in filters.tags_any}
    for track in tracks:
        if filters.bpm_min is not None and (track.bpm is None or track.bpm < filters.bpm_min):
            continue
        if filters.bpm_max is not None and (track.bpm is None or track.bpm > filters.bpm_max):
            continue
        # Metadata fields are often missing on fallback-enriched tracks.
        # Do not exclude unknown values here; let client-side post-filters
        # handle strict filtering when metadata is available.
        if (
            filters.popularity_min is not None
            and track.popularity is not None
            and track.popularity < filters.popularity_min
        ):
            continue
        if (
            filters.popularity_max is not None
            and track.popularity is not None
            and track.popularity > filters.popularity_max
        ):
            continue
        if (
            filters.release_year_min is not None
            and track.release_year is not None
            and track.release_year < filters.release_year_min
        ):
            continue
        if (
            filters.release_year_max is not None
            and track.release_year is not None
            and track.release_year > filters.release_year_max
        ):
            continue
        if filters.require_instrumental is True:
            tags = {normalize_tag(tag) for tag in (track.tags or [])}
            if not bool(tags & INSTRUMENTAL_TAGS):
                continue
        if wanted_tags:
            tags = {normalize_tag(tag) for tag in (track.tags or [])}
            if not (tags & wanted_tags):
                continue
        filtered.append(track)
    return filtered

def _should_retry_spotify_resolve(
    artist_name: str,
    track_name: str,
    candidate_isrc: str | None,
    hint_isrc: str | None,
    hint_artist: str | None,
    hint_title: str | None,
) -> bool:
    """True if MusicBrainz hints differ enough to justify a second Spotify lookup."""
    if hint_isrc:
        cur = candidate_isrc.strip().upper() if candidate_isrc else ""
        if not cur or hint_isrc.strip().upper() != cur:
            return True
    if hint_artist and hint_artist.strip().lower() != artist_name.strip().lower():
        return True
    if hint_title and hint_title.strip().lower() != track_name.strip().lower():
        return True
    return False


def _get_mapping_user_sp(request: Request):
    """Return a user-scoped Spotify client for mapping, or None if unavailable."""
    session_id = request.cookies.get("sp_session", "")
    token_info = session_store.get(session_id)
    if not token_info:
        return None
    try:
        token_info = refresh_if_needed(token_info)
        session_store.set(session_id, token_info, SESSION_TTL_SECONDS)
        return get_user_client(token_info["access_token"])
    except Exception:
        return None


def _effective_weights_unified(
    weights: AudioWeights, instrumental_similarity_only: bool
) -> AudioWeights:
    """When instrumental_similarity_only, de-emphasize valence/danceability for audio ranking."""
    if not instrumental_similarity_only:
        return weights
    return AudioWeights(
        tempo=weights.tempo,
        energy=weights.energy,
        valence=0.0,
        danceability=0.0,
        acousticness=weights.acousticness,
        instrumentalness=weights.instrumentalness,
    )


async def _enrich_analysis_metrics(tracks: list[TrackInfo], client: httpx.AsyncClient) -> None:
    async def enrich_single(track: TrackInfo) -> None:
        artist = track.artists[0] if track.artists else ""
        metrics = await fetch_analysis_metrics(
            artist=artist,
            title=track.name,
            spotify_id=track.spotify_id,
            client=client,
        )
        if metrics:
            track.analysis_metrics = {**(track.analysis_metrics or {}), **metrics}
            if track.bpm is None and isinstance(metrics.get("tempo"), (int, float)):
                track.bpm = float(metrics["tempo"])
        if track.bpm is None:
            inferred_bpm = _infer_bpm_from_track_context(track)
            if inferred_bpm is not None:
                track.bpm = inferred_bpm
                track.analysis_metrics = {
                    **(track.analysis_metrics or {}),
                    "bpm_inferred": True,
                    "bpm_inference_source": "tags+analysis_heuristic",
                }

    await asyncio.gather(*(enrich_single(track) for track in tracks))


@app.post("/api/similar/unified", response_model=SimilarTracksResponse)
async def api_similar_unified(req: UnifiedSimilarRequest, request: Request):
    mapping_user_sp = _get_mapping_user_sp(request)
    url_clean = req.resolved_spotify_url()
    playlist_id = _extract_playlist_id(url_clean) if url_clean else None
    if playlist_id:
        try:
            playlist_tracks = await asyncio.to_thread(
                _fetch_playlist_seed_tracks,
                playlist_id,
                mapping_user_sp,
                PLAYLIST_FETCH_CAP,
            )
        except spotipy.SpotifyException as exc:
            if exc.http_status == 403:
                raise HTTPException(
                    status_code=403,
                    detail=(
                        "Spotify denied access to this playlist after multiple fetch paths. "
                        f"Details: {_spotify_exception_detail(exc)}"
                    ),
                )
            raise HTTPException(
                status_code=502,
                detail=f"Spotify playlist fetch error: {_spotify_exception_detail(exc)}",
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Spotify playlist fetch error: {exc}")
        if not playlist_tracks:
            raise HTTPException(status_code=400, detail="Playlist has no usable tracks.")
        sample_size = min(PLAYLIST_SEED_SAMPLE_CAP, len(playlist_tracks))
        sampled_indices = _playlist_sample_indices(len(playlist_tracks), sample_size)
        sampled_seeds = [playlist_tracks[idx] for idx in sampled_indices]
        representative_seed = sampled_seeds[0]
        # For playlist seeds, run listener similarity on all sampled seeds so
        # ranking reflects the sampled slice as a whole, not one representative track.
        listener_seeds = sampled_seeds
        (
            listener_similar,
            seed_tags,
            mapping_degraded_reason,
            external_links_degraded_reason,
        ) = await _collect_playlist_listener_tracks(
            listener_seeds,
            req,
            mapping_user_sp,
            use_metadata_fallback=False,
        )
        playlist_audio_tracks = await asyncio.to_thread(
            _collect_playlist_recommendations,
            sampled_seeds,
            req,
            mapping_user_sp,
        )
        blended = _blend_track_lists(
            listener_similar,
            playlist_audio_tracks,
            listener_weight=0.35 if req.instrumental_similarity_only else 0.45,
        )
        all_tags: set[str] = set(seed_tags)
        for track in blended:
            all_tags.update(track.tags or [])
        tag_categories = build_tag_categories(list(all_tags))
        client = _get_http_client()
        await _enrich_analysis_metrics(blended, client)
        filtered = _apply_backend_filters(blended, req.filters)
        playlist_seed_trace = [
            PlaylistSeedSummary(
                name=seed_track.name,
                artists=seed_track.artists or [],
                spotify_id=seed_track.spotify_id,
            )
            for seed_track in sampled_seeds
        ]
        return _build_similar_response(
            seed=representative_seed,
            similar_ranked=filtered,
            limit=req.limit,
            strict_mapped_only=req.strict_mapped_only,
            seed_tags=seed_tags,
            tag_categories=tag_categories,
            mapping_degraded_reason=mapping_degraded_reason,
            external_links_degraded_reason=external_links_degraded_reason,
            approximated=True,
            playlist_id=playlist_id,
            playlist_sampled_seeds=playlist_seed_trace,
        )
    if url_clean:
        try:
            seed = await asyncio.to_thread(get_track_info, url_clean, mapping_user_sp)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Spotify API error: {exc}")
    else:
        artist = (req.seed_artist or "").strip()
        title = (req.seed_track or "").strip()
        if not artist or not title:
            raise HTTPException(
                status_code=400,
                detail="seed_artist and seed_track are required when url is empty",
            )
        seed = TrackInfo(
            name=title,
            artists=[artist],
            album="",
            spotify_mapping_status="unmapped",
        )

    exclude = set(req.exclude) if req.exclude else None
    fetch_limit = _listener_fetch_limit(req.limit, req.strict_mapped_only)
    try:
        listener_similar, seed_tags, mapping_degraded_reason, external_links_degraded_reason = await _enrich_lastfm(
            seed,
            fetch_limit,
            exclude,
            use_metadata_fallback=req.use_metadata_fallback,
            user_sp=mapping_user_sp,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Last.fm API error: {exc}")

    effective_weights = _effective_weights_unified(
        req.weights, req.instrumental_similarity_only
    )
    audio_req = AudioSimilarRequest(
        url=url_clean or "",
        limit=max(req.limit, min(req.limit * 2, 100)),
        weights=effective_weights,
        exclude=req.exclude,
        strict_mapped_only=False,
        use_metadata_fallback=req.use_metadata_fallback,
    )
    audio_response = await _audio_fallback_path(seed, audio_req, request)
    blend_listener = 0.35 if req.instrumental_similarity_only else 0.55
    blended = _blend_track_lists(
        listener_similar,
        audio_response.similar_tracks,
        listener_weight=blend_listener,
    )

    all_tags: set[str] = set(seed_tags)
    for t in blended:
        all_tags.update(t.tags or [])
    for t in audio_response.seed_tags:
        all_tags.add(t)
    tag_categories = build_tag_categories(list(all_tags))

    client = _get_http_client()
    await _enrich_analysis_metrics(blended, client)
    filtered = _apply_backend_filters(blended, req.filters)

    return _build_similar_response(
        seed=seed,
        similar_ranked=filtered,
        limit=req.limit,
        strict_mapped_only=req.strict_mapped_only,
        seed_tags=seed_tags,
        tag_categories=tag_categories,
        mapping_degraded_reason=mapping_degraded_reason,
        external_links_degraded_reason=external_links_degraded_reason,
        approximated=True,
    )


@app.post("/api/similar", response_model=SimilarTracksResponse)
async def api_similar(req: TrackRequest, request: Request):
    return await api_similar_unified(
        UnifiedSimilarRequest(
            url=req.url,
            limit=req.limit,
            exclude=req.exclude,
            strict_mapped_only=req.strict_mapped_only,
            use_metadata_fallback=req.use_metadata_fallback,
            filters=SimilarityFilters(),
            weights=AudioWeights(),
        ),
        request,
    )


@app.post("/api/similar/audio", response_model=SimilarTracksResponse)
async def api_similar_audio(req: AudioSimilarRequest, request: Request):
    return await api_similar_unified(
        UnifiedSimilarRequest(
            url=req.url,
            limit=req.limit,
            weights=req.weights,
            exclude=req.exclude,
            strict_mapped_only=req.strict_mapped_only,
            use_metadata_fallback=req.use_metadata_fallback,
            filters=SimilarityFilters(),
        ),
        request,
    )


async def _audio_spotify_path(
    seed: TrackInfo,
    seed_features,
    req: AudioSimilarRequest,
    user_sp=None,
) -> SimilarTracksResponse:
    """Spotify-native path: recommendations + real audio features."""
    seed.audio_features = seed_features
    seed.bpm = seed_features.tempo

    targets = build_recommendation_targets(seed_features, req.weights)

    feature_source = "user" if user_sp is not None else "app"
    try:
        candidates = await asyncio.to_thread(
            get_recommendations,
            seed.spotify_id,
            targets,
            min(req.limit * 2, 100),
            user_sp,
            source=feature_source,
        )
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Recommendations error: {exc}")

    if not candidates:
        return SimilarTracksResponse(
            seed_track=seed,
            similar_tracks=[],
            strict_mapped_only=req.strict_mapped_only,
            mapped_count=0,
            unmapped_count=0,
        )

    candidate_ids = [t.spotify_id for t in candidates if t.spotify_id]
    try:
        cand_features = await asyncio.to_thread(
            get_audio_features,
            candidate_ids,
            user_sp,
            source=feature_source,
        )
    except Exception:
        cand_features = {}

    for track in candidates:
        af = cand_features.get(track.spotify_id) if track.spotify_id else None
        track.audio_features = af
        if track.spotify_id and not track.mapping_source:
            track.mapping_source = "spotify_recommendation"
        if af:
            track.bpm = af.tempo
            track.match_score = compute_similarity(seed_features, af, req.weights)
        else:
            track.match_score = 0.0

    candidates.sort(key=lambda t: t.match_score or 0, reverse=True)
    return _build_similar_response(
        seed=seed,
        similar_ranked=candidates,
        limit=req.limit,
        strict_mapped_only=req.strict_mapped_only,
        seed_tags=[],
        tag_categories={},
    )


async def _audio_fallback_path(
    seed: TrackInfo, req: AudioSimilarRequest, request: Request,
) -> SimilarTracksResponse:
    """Fallback: Last.fm candidates + tag-estimated features + weighted scoring."""
    exclude = set(req.exclude) if req.exclude else None
    fetch_limit = _listener_fetch_limit(req.limit, req.strict_mapped_only)
    mapping_user_sp = _get_mapping_user_sp(request)
    try:
        similar, seed_tags, mapping_degraded_reason, external_links_degraded_reason = await _enrich_lastfm(
            seed,
            fetch_limit,
            exclude,
            use_metadata_fallback=req.use_metadata_fallback,
            user_sp=mapping_user_sp,
        )
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

    similar.sort(key=_fused_rank_value, reverse=True)

    all_tags: set[str] = set(seed_tags)
    for t in similar:
        all_tags.update(t.tags or [])

    return _build_similar_response(
        seed=seed,
        similar_ranked=similar,
        limit=req.limit,
        strict_mapped_only=req.strict_mapped_only,
        seed_tags=seed_tags,
        tag_categories=build_tag_categories(list(all_tags)),
        mapping_degraded_reason=mapping_degraded_reason,
        external_links_degraded_reason=external_links_degraded_reason,
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

@app.get("/api/spotify/oauth-config")
def spotify_oauth_config():
    """Public values the app sends to Spotify OAuth (for debugging redirect_uri mismatch)."""
    return {
        "app_base_url": APP_BASE_URL,
        "redirect_uri": SPOTIFY_REDIRECT_URI,
        "redirect_uri_derived_from_app_base": SPOTIFY_REDIRECT_DERIVED_FROM_APP_BASE,
        "spotify_client_id": SPOTIFY_CLIENT_ID,
        "session_store_backend": SESSION_STORE_BACKEND,
        "effective_session_store_backend": _EFFECTIVE_SESSION_BACKEND,
    }


@app.get("/api/spotify/login")
def spotify_login():
    """Redirect browser to Spotify authorization page."""
    state = secrets.token_urlsafe(16)
    verifier, challenge = build_pkce_pair()
    url = get_authorize_url_pkce(state=state, code_challenge=challenge)
    resp = RedirectResponse(url=url)
    resp.set_cookie(
        "sp_state", state, httponly=True, secure=SESSION_COOKIE_SECURE, samesite="lax", max_age=300,
    )
    resp.set_cookie(
        "sp_code_verifier", verifier, httponly=True, secure=SESSION_COOKIE_SECURE, samesite="lax", max_age=300,
    )
    return resp


@app.get("/api/spotify/callback")
def spotify_callback(request: Request, code: str = "", error: str = "", state: str = ""):
    saved_state = request.cookies.get("sp_state", "")
    code_verifier = request.cookies.get("sp_code_verifier", "")
    if error:
        return RedirectResponse(url=f"/?spotify_error={error}")
    if not code or state != saved_state:
        return RedirectResponse(url="/?spotify_error=state_mismatch")

    try:
        token_info = exchange_code(code, code_verifier=code_verifier or None)
    except Exception as exc:
        logging.error("Spotify token exchange failed: %s", exc)
        return RedirectResponse(url="/?spotify_error=token_exchange_failed")

    session_id = secrets.token_urlsafe(32)
    session_store.set(session_id, token_info, SESSION_TTL_SECONDS)

    resp = RedirectResponse(url="/?spotify_connected=1")
    resp.set_cookie(
        "sp_session", session_id, httponly=True, secure=SESSION_COOKIE_SECURE, samesite="lax", max_age=3600,
    )
    resp.delete_cookie("sp_state", httponly=True, secure=SESSION_COOKIE_SECURE, samesite="lax")
    resp.delete_cookie("sp_code_verifier", httponly=True, secure=SESSION_COOKIE_SECURE, samesite="lax")
    return resp


@app.get("/api/spotify/status")
def spotify_status(request: Request):
    """Check whether the user has a valid Spotify session."""
    session_id = request.cookies.get("sp_session", "")
    if not session_id:
        return {"connected": False, "reason": "no_session_cookie"}
    token_info = session_store.get(session_id)
    if not token_info:
        logger.warning(
            "Spotify session cookie present but no store entry (effective_backend=%s). "
            "Often caused by multiple workers/replicas with in-memory sessions.",
            _EFFECTIVE_SESSION_BACKEND,
        )
        return {
            "connected": False,
            "reason": "session_not_in_store",
            "session_store_backend": _EFFECTIVE_SESSION_BACKEND,
        }
    try:
        token_info = refresh_if_needed(token_info)
        session_store.set(session_id, token_info, SESSION_TTL_SECONDS)
        sp = get_user_client(token_info["access_token"])
        user = sp.current_user()
        granted_scopes = []
        raw_scopes = token_info.get("scope")
        if isinstance(raw_scopes, str) and raw_scopes.strip():
            granted_scopes = sorted(set(raw_scopes.split()))
        return {
            "connected": True,
            "user": user.get("display_name") or user.get("id"),
            "granted_scopes": granted_scopes,
        }
    except Exception:
        logger.exception("Spotify status: token refresh or API call failed")
        return {"connected": False, "reason": "spotify_token_invalid"}


@app.post("/api/spotify/logout")
def spotify_logout(request: Request):
    session_id = request.cookies.get("sp_session", "")
    session_store.delete(session_id)
    resp = JSONResponse(content={"ok": True})
    resp.delete_cookie("sp_session", httponly=True, secure=SESSION_COOKIE_SECURE, samesite="lax")
    return resp


def _get_user_sp(request: Request) -> "spotipy.Spotify":
    session_id = request.cookies.get("sp_session", "")
    token_info = session_store.get(session_id)
    if not token_info:
        raise HTTPException(status_code=401, detail="Not connected to Spotify. Please log in.")
    token_info = refresh_if_needed(token_info)
    session_store.set(session_id, token_info, SESSION_TTL_SECONDS)
    return get_user_client(token_info["access_token"])


class PlaylistRequest(BaseModel):
    name: str = Field(default="Similar Tracks", max_length=200)
    track_uris: list[str] = Field(min_length=1)
    public: bool = False


class PlaylistAddTracksRequest(BaseModel):
    track_uris: list[str] = Field(min_length=1)


@app.post("/api/spotify/playlist")
def create_playlist(req: PlaylistRequest, request: Request):
    """Create a Spotify playlist and add the given tracks."""
    try:
        sp = _get_user_sp(request)
        user_id = sp.current_user()["id"]
        playlist = _run_spotify_write_with_retry(
            lambda: sp.user_playlist_create(user_id, req.name, public=req.public)
        )
        for i in range(0, len(req.track_uris), 100):
            _run_spotify_write_with_retry(
                lambda chunk=req.track_uris[i : i + 100]: sp.playlist_add_items(playlist["id"], chunk)
            )
        return {
            "playlist_id": playlist["id"],
            "playlist_url": playlist["external_urls"]["spotify"],
        }
    except HTTPException:
        raise
    except Exception as exc:
        detail = _classify_queue_error(exc)
        raise HTTPException(
            status_code=409 if detail["retryable"] else 502,
            detail={
                "code": "PLAYLIST_CREATE_FAILED",
                "message": detail["message"],
                "retryable": detail["retryable"],
                "errors": [{"message": detail["message"], "code": detail["code"]}],
            },
        )


@app.get("/api/spotify/playlists", response_model=list[PlaylistLookupItem])
def list_playlists(request: Request):
    sp = _get_user_sp(request)
    items: list[PlaylistLookupItem] = []
    offset = 0
    while offset < 200:
        payload = sp.current_user_playlists(limit=50, offset=offset)
        playlist_items = payload.get("items", []) if isinstance(payload, dict) else []
        for item in playlist_items:
            items.append(
                PlaylistLookupItem(
                    id=item.get("id", ""),
                    name=item.get("name", "Untitled"),
                    uri=item.get("uri", ""),
                    owner=(item.get("owner", {}) or {}).get("display_name") or (item.get("owner", {}) or {}).get("id") or "",
                    public=item.get("public"),
                    tracks_total=(item.get("tracks", {}) or {}).get("total"),
                )
            )
        if not payload.get("next"):
            break
        offset += 50
    return items


@app.post("/api/spotify/playlists/{playlist_id}/tracks")
def add_tracks_to_playlist(playlist_id: str, req: PlaylistAddTracksRequest, request: Request):
    sp = _get_user_sp(request)
    for i in range(0, len(req.track_uris), 100):
        _run_spotify_write_with_retry(
            lambda chunk=req.track_uris[i : i + 100]: sp.playlist_add_items(playlist_id, chunk)
        )
    return {"playlist_id": playlist_id, "added": len(req.track_uris)}


class QueueRequest(BaseModel):
    track_uris: list[str] = Field(min_length=1)
    device_id: str | None = Field(
        default=None,
        max_length=128,
        description="Web Playback SDK device id; transfer playback here before queuing.",
    )


class PlayRequest(BaseModel):
    device_id: str = Field(min_length=1, max_length=128)
    uris: list[str] = Field(min_length=1, max_length=50)


class ResolveUriItem(BaseModel):
    key: str = Field(min_length=1, max_length=300)
    artist: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=200)


class ResolveUrisRequest(BaseModel):
    items: list[ResolveUriItem] = Field(min_length=1, max_length=120)
    use_musicbrainz_first: bool = Field(
        default=False,
        description="If true, try MusicBrainz Spotify URL relations before Spotify search (saves quota).",
    )


_SPOTIFY_TRACK_ID_RE = re.compile(r"^[0-9A-Za-z]{22}$")
_SPOTIFY_TRACK_LINK_RE = re.compile(r"(?:open\.spotify\.com/track/|spotify:track:)([0-9A-Za-z]{22})")


def _valid_spotify_track_id(track_id: str | None) -> bool:
    return bool(track_id and _SPOTIFY_TRACK_ID_RE.fullmatch(track_id))


def _spotify_uri_from_link(link: str | None) -> str | None:
    if not isinstance(link, str) or not link.strip():
        return None
    match = _SPOTIFY_TRACK_LINK_RE.search(link.strip())
    if not match:
        return None
    track_id = match.group(1)
    if not _valid_spotify_track_id(track_id):
        return None
    return f"spotify:track:{track_id}"


@app.get("/api/spotify/search-track")
def spotify_search_track(q: str, request: Request):
    sp = _get_user_sp(request)
    payload, rate_limited, retry_raw = _single_spotify_search(sp, q)
    if rate_limited:
        raw = retry_raw if retry_raw is not None else 60
        capped = min(raw, _SPOTIFY_RETRY_AFTER_HEADER_CAP)
        if raw > _SPOTIFY_RETRY_AFTER_HEADER_CAP:
            logger.warning(
                "search-track Retry-After from Spotify is %ss; echoing capped %ss to client",
                raw,
                capped,
            )
        return JSONResponse(
            status_code=429,
            content={"spotify_uri": None, "code": "spotify_rate_limited"},
            headers={"Retry-After": str(capped)},
        )
    if not payload:
        return {"spotify_uri": None}
    items = payload.get("tracks", {}).get("items", [])
    if not items:
        return {"spotify_uri": None}
    track_id = items[0].get("id")
    if not track_id:
        return {"spotify_uri": None}
    return {"spotify_uri": f"spotify:track:{track_id}"}


def _resolve_spotify_uri(sp, artist: str, title: str) -> tuple[str | None, bool, int | None]:
    """Resolve one track; returns (uri or None, rate_limited, retry_after_seconds). Never raises."""
    q = f"artist:{artist} track:{title}"
    payload, rate_limited, retry_after = _single_spotify_search(sp, q)
    if rate_limited:
        return None, True, retry_after
    if not payload:
        return None, False, None
    items = payload.get("tracks", {}).get("items", [])
    if not items:
        return None, False, None
    track_id = items[0].get("id")
    uri = f"spotify:track:{track_id}" if track_id else None
    return uri, False, None


@app.post("/api/spotify/resolve-uris")
async def spotify_resolve_uris(req: ResolveUrisRequest, request: Request):
    """Resolve many artist/title pairs to Spotify URIs with gentle pacing (fewer 429s)."""
    sp = _get_user_sp(request)
    results: list[dict[str, str | None]] = []
    any_rate_limited = False
    stopped_rate_limited = False
    retry_after_hint: int | None = None
    chunk_size = 25
    mb_resolved = 0
    mb_miss = 0
    external_link_resolved = 0
    spotify_search_resolved = 0
    mb_errors = 0
    http_client: httpx.AsyncClient | None = _get_http_client() if req.use_musicbrainz_first else None

    for i, item in enumerate(req.items):
        if stopped_rate_limited:
            results.append({"key": item.key, "spotify_uri": None})
            continue

        uri: str | None = None
        rate_limited = False

        if req.use_musicbrainz_first and http_client is not None:
            mb_id: str | None = None
            mb_lookup_ok = False
            try:
                mb_id = await fetch_musicbrainz_spotify_relation_id(
                    http_client,
                    item.artist,
                    item.title,
                    None,
                )
                mb_lookup_ok = True
            except Exception:
                mb_errors += 1
                mb_id = None
            # MusicBrainz etiquette: ~1 request per second per application.
            await asyncio.sleep(1.05)
            if _valid_spotify_track_id(mb_id):
                uri = f"spotify:track:{mb_id}"
                mb_resolved += 1
            elif mb_lookup_ok:
                mb_miss += 1

        if uri is None and req.use_musicbrainz_first and http_client is not None:
            # Third-party link path to reduce direct Spotify search pressure:
            # Deezer search -> Song.link cross-provider map -> Spotify URI.
            try:
                dz_info = await deezer_fetch(http_client, item.artist, item.title)
            except Exception:
                dz_info = {}
            deezer_url = dz_info.get("link") if isinstance(dz_info, dict) else None
            try:
                links, _primary = await resolve_external_links(
                    http_client,
                    artist=item.artist,
                    title=item.title,
                    isrc=(dz_info.get("isrc") if isinstance(dz_info, dict) else None),
                    deezer_url=deezer_url,
                )
            except Exception:
                links = {}
            uri_from_links = _spotify_uri_from_link((links or {}).get("spotify"))
            if uri_from_links:
                uri = uri_from_links
                external_link_resolved += 1

        if uri is None:
            uri, rate_limited, retry_after = await asyncio.to_thread(
                _resolve_spotify_uri, sp, item.artist, item.title
            )
            if uri and req.use_musicbrainz_first:
                spotify_search_resolved += 1

        if rate_limited:
            any_rate_limited = True
            stopped_rate_limited = True
            if retry_after is not None:
                retry_after_hint = retry_after
        results.append({"key": item.key, "spotify_uri": uri})
        if stopped_rate_limited:
            continue
        if i + 1 < len(req.items):
            await asyncio.sleep(0.08)
        if chunk_size and (i + 1) % chunk_size == 0 and i + 1 < len(req.items):
            await asyncio.sleep(0.2)
    resolved_count = sum(1 for row in results if row.get("spotify_uri"))
    unresolved_count = len(results) - resolved_count
    meta: dict[str, object] = {
        "total": len(results),
        "resolved": resolved_count,
        "unresolved": unresolved_count,
        "rate_limited": any_rate_limited,
    }
    if retry_after_hint is not None:
        meta["retry_after_seconds"] = min(
            max(int(retry_after_hint), 1),
            _SPOTIFY_RETRY_AFTER_HEADER_CAP,
        )
    if req.use_musicbrainz_first:
        meta["resolved_via_mb"] = mb_resolved
        meta["resolved_via_external_links"] = external_link_resolved
        meta["mb_no_spotify_link"] = mb_miss
        meta["resolved_via_spotify_search"] = spotify_search_resolved
        meta["mb_errors"] = mb_errors
    return {"results": results, "meta": meta}


def _session_access_token(request: Request) -> tuple[str, str]:
    """Return (session_id, access_token) after refresh; raises HTTPException if not connected."""
    session_id = request.cookies.get("sp_session", "")
    token_info = session_store.get(session_id)
    if not token_info:
        raise HTTPException(status_code=401, detail="Not connected to Spotify.")
    token_info = refresh_if_needed(token_info)
    session_store.set(session_id, token_info, SESSION_TTL_SECONDS)
    return session_id, token_info["access_token"]


@app.get("/api/spotify/player-token")
def spotify_player_token(request: Request):
    """Short-lived access token for the Spotify Web Playback SDK (same-origin only)."""
    _, access_token = _session_access_token(request)
    return {"access_token": access_token}


@app.post("/api/spotify/play")
def spotify_start_playback(req: PlayRequest, request: Request):
    """Start playback on a device (used with the in-browser Web Playback SDK)."""
    sp = _get_user_sp(request)
    _run_spotify_write_with_retry(lambda: sp.start_playback(device_id=req.device_id, uris=req.uris))
    return {"ok": True}


def _err_text(exc: Exception) -> str:
    return str(exc).lower()


def _retry_after_seconds(exc: Exception) -> int | None:
    if not isinstance(exc, spotipy.SpotifyException):
        return None
    if exc.http_status != 429:
        return None
    try:
        return int((exc.headers or {}).get("Retry-After", "1"))
    except Exception:
        return 1


def _run_spotify_write_with_retry(action, *, attempts: int = 5):
    for attempt in range(attempts):
        try:
            return action()
        except Exception as exc:
            retry_after = _retry_after_seconds(exc)
            if retry_after is None or attempt == attempts - 1:
                raise
            wait_seconds = retry_after * (2 ** attempt)
            time.sleep(wait_seconds)


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


def _parse_text_playlist_line(line: str) -> tuple[str | None, str | None]:
    raw = (line or "").strip()
    if not raw:
        return None, None
    for sep in (" — ", " - ", " – ", "—", "-", "–"):
        if sep in raw:
            left, right = raw.split(sep, 1)
            artist = left.strip()
            title = right.strip()
            if artist and title:
                return artist, title
    return None, None


@app.post("/api/spotify/queue")
def add_to_queue(req: QueueRequest, request: Request):
    """Add tracks to the user's Spotify playback queue."""
    sp = _get_user_sp(request)
    errors = []
    added = 0
    recovered_player = False
    device_id = (req.device_id or "").strip() or None

    if device_id:
        try:
            sp.transfer_playback(device_id=device_id, force_play=False)
        except Exception as exc:
            logger.info("transfer_playback before queue (device_id=%s): %s", device_id[:8], exc)

    def _add(uri: str, dev: str | None) -> None:
        _run_spotify_write_with_retry(lambda u=uri, d=dev: sp.add_to_queue(u, device_id=d))

    for uri in req.track_uris:
        try:
            _add(uri, device_id)
            added += 1
        except Exception as exc:
            recoverable = _is_no_active_device_error(exc) or _is_restricted_device_error(exc)
            if recoverable and not recovered_player:
                ok, recovery_error = _recover_player_once(sp)
                recovered_player = True
                if ok:
                    try:
                        _add(uri, device_id)
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


@app.post("/api/spotify/playlist/from-text", response_model=TextPlaylistCreateResponse)
def create_playlist_from_text(req: TextPlaylistCreateRequest, request: Request):
    sp = _get_user_sp(request)
    cleaned_lines = [line.strip() for line in req.lines if line and line.strip()]
    playlist_name = req.name.strip() or "Cat-ID Text Playlist"
    uris: list[str] = []
    unmatched: list[TextPlaylistUnmatched] = []

    for line in cleaned_lines:
        artist, title = _parse_text_playlist_line(line)
        if not artist or not title:
            unmatched.append(TextPlaylistUnmatched(line=line, reason="Could not parse line format"))
            continue
        try:
            response = _run_spotify_write_with_retry(
                lambda: sp.search(
                    q=f"artist:{artist} track:{title}",
                    type="track",
                    limit=1,
                    market="from_token",
                )
            )
            items = response.get("tracks", {}).get("items", [])
        except Exception:
            items = []

        if not items:
            unmatched.append(TextPlaylistUnmatched(line=line, reason="No Spotify match found"))
            continue
        track_id = items[0].get("id")
        if not track_id:
            unmatched.append(TextPlaylistUnmatched(line=line, reason="Match missing Spotify ID"))
            continue
        uris.append(f"spotify:track:{track_id}")

    if not uris:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "NO_MATCHED_TRACKS",
                "message": "No text lines could be mapped to Spotify tracks.",
                "unmatched": [item.model_dump() for item in unmatched],
            },
        )

    user_id = sp.current_user()["id"]
    playlist = _run_spotify_write_with_retry(
        lambda: sp.user_playlist_create(user_id, playlist_name, public=False)
    )
    for i in range(0, len(uris), 100):
        _run_spotify_write_with_retry(
            lambda chunk=uris[i : i + 100]: sp.playlist_add_items(playlist["id"], chunk)
        )

    return TextPlaylistCreateResponse(
        playlist_id=playlist["id"],
        playlist_url=playlist["external_urls"]["spotify"],
        input_count=len(cleaned_lines),
        matched_count=len(uris),
        unmatched=unmatched,
    )


app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")

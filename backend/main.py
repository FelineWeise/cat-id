import asyncio
import logging
import re
import secrets
import time
from collections.abc import Sequence
from difflib import SequenceMatcher

import httpx
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

logger = logging.getLogger(__name__)
_shared_http_client: httpx.AsyncClient | None = None
session_store = build_session_store(SESSION_STORE_BACKEND, REDIS_URL)
_EFFECTIVE_SESSION_BACKEND = getattr(session_store, "backend_key", SESSION_STORE_BACKEND)
_NON_ALNUM = re.compile(r"[^a-z0-9]+", re.I)


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
        score = (ls * 0.55) + (aps * 0.45)
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

    await asyncio.gather(*(enrich_single(track) for track in tracks))


@app.post("/api/similar/unified", response_model=SimilarTracksResponse)
async def api_similar_unified(req: UnifiedSimilarRequest, request: Request):
    mapping_user_sp = _get_mapping_user_sp(request)
    try:
        seed = await asyncio.to_thread(get_track_info, req.url, mapping_user_sp)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Spotify API error: {exc}")

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

    audio_req = AudioSimilarRequest(
        url=req.url,
        limit=max(req.limit, min(req.limit * 2, 100)),
        weights=req.weights,
        exclude=req.exclude,
        strict_mapped_only=False,
        use_metadata_fallback=req.use_metadata_fallback,
    )
    audio_response = await _audio_fallback_path(seed, audio_req, request)
    blended = _blend_track_lists(listener_similar, audio_response.similar_tracks)

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
        return {"connected": True, "user": user.get("display_name") or user.get("id")}
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


@app.get("/api/spotify/search-track")
def spotify_search_track(q: str, request: Request):
    sp = _get_user_sp(request)
    response = sp.search(q=q, type="track", limit=1, market="from_token")
    items = response.get("tracks", {}).get("items", [])
    if not items:
        return {"spotify_uri": None}
    track_id = items[0].get("id")
    if not track_id:
        return {"spotify_uri": None}
    return {"spotify_uri": f"spotify:track:{track_id}"}


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

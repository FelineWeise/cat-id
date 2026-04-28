from typing import Literal

from pydantic import BaseModel, Field


class TrackRequest(BaseModel):
    url: str = Field(description="Spotify track URL or URI")
    limit: int = Field(default=10, ge=1, le=250, description="Number of similar tracks to return")
    exclude: list[str] = Field(
        default_factory=list,
        description="Lowercased 'artist::trackname' keys to exclude from results",
    )
    strict_mapped_only: bool = Field(
        default=False,
        description="If true, only Spotify-mapped tracks are returned (queue/playlist-safe).",
    )
    use_metadata_fallback: bool = Field(
        default=True,
        description="If true, retry Spotify resolution using free MusicBrainz hints when direct mapping fails.",
    )


class AudioFeatures(BaseModel):
    tempo: float | None = None
    energy: float | None = None
    valence: float | None = None
    danceability: float | None = None
    acousticness: float | None = None
    instrumentalness: float | None = None


AUDIO_DIMENSION_KEYS = list(AudioFeatures.model_fields.keys())


class AudioWeights(BaseModel):
    """Per-dimension weights (0.0–1.0). Dimensions above 0.3 become recommendation targets."""
    tempo: float = Field(default=0.5, ge=0.0, le=1.0)
    energy: float = Field(default=0.5, ge=0.0, le=1.0)
    valence: float = Field(default=0.5, ge=0.0, le=1.0)
    danceability: float = Field(default=0.5, ge=0.0, le=1.0)
    acousticness: float = Field(default=0.5, ge=0.0, le=1.0)
    instrumentalness: float = Field(default=0.5, ge=0.0, le=1.0)


class AudioSimilarRequest(BaseModel):
    url: str = Field(description="Spotify track URL or URI")
    limit: int = Field(default=20, ge=1, le=250, description="Number of similar tracks to return")
    weights: AudioWeights = Field(default_factory=AudioWeights)
    exclude: list[str] = Field(
        default_factory=list,
        description="Lowercased 'artist::trackname' keys to exclude from results",
    )
    strict_mapped_only: bool = Field(
        default=False,
        description="If true, only Spotify-mapped tracks are returned (queue/playlist-safe).",
    )
    use_metadata_fallback: bool = Field(
        default=True,
        description="If true, retry Spotify resolution using free MusicBrainz hints when direct mapping fails.",
    )


class TrackInfo(BaseModel):
    name: str
    artists: list[str]
    album: str
    album_art: str | None = None
    preview_url: str | None = None
    spotify_url: str | None = None
    spotify_id: str | None = None
    mapping_source: str | None = None
    external_links: dict[str, str] = Field(default_factory=dict)
    external_primary_provider: str | None = None
    spotify_mapping_status: Literal["mapped", "unmapped"] = "mapped"
    match_score: float | None = None
    bpm: float | None = None
    tags: list[str] = Field(default_factory=list)
    audio_features: AudioFeatures | None = None


class SimilarTracksResponse(BaseModel):
    seed_track: TrackInfo
    similar_tracks: list[TrackInfo]
    strict_mapped_only: bool = Field(
        default=False,
        description="Echo of request: results are Spotify-mapped only when true.",
    )
    seed_tags: list[str] = Field(default_factory=list)
    total_candidates: int = Field(
        default=0,
        description="Total ranked candidate count before final limit slicing.",
    )
    mapped_count: int = Field(default=0, description="Number of tracks mapped to Spotify IDs.")
    unmapped_count: int = Field(default=0, description="Number of tracks without Spotify mapping.")
    mapping_used_user_token: bool = Field(
        default=False,
        description="True if at least one mapped track was resolved via user-token search.",
    )
    mapping_source_counts: dict[str, int] = Field(
        default_factory=dict,
        description="Counts of mapping sources for mapped tracks (for diagnostics).",
    )
    mapping_degraded_reason: str | None = Field(
        default=None,
        description="Reason mapping quality was reduced (for example rate limits or time budget).",
    )
    external_links_degraded_reason: str | None = Field(
        default=None,
        description="Reason external link enrichment was reduced (for example rate limits or time budget).",
    )
    tag_categories: dict[str, str] = Field(
        default_factory=dict,
        description="Map tag name -> category (mood, genre, vocals_instrumentals) for filter sections",
    )
    approximated: bool = Field(
        default=False,
        description="True when audio features were estimated from tags instead of Spotify's API",
    )

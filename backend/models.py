from typing import Literal

from pydantic import BaseModel, Field, model_validator


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


class SimilarityFilters(BaseModel):
    bpm_min: float | None = None
    bpm_max: float | None = None
    popularity_min: int | None = Field(default=None, ge=0, le=100)
    popularity_max: int | None = Field(default=None, ge=0, le=100)
    release_year_min: int | None = None
    release_year_max: int | None = None
    tags_any: list[str] = Field(default_factory=list)
    require_instrumental: bool | None = None


class UnifiedSimilarRequest(BaseModel):
    url: str = Field(
        default="",
        description="Spotify track URL or URI (optional if seed_artist and seed_track are set)",
    )
    seed_artist: str | None = Field(
        default=None,
        max_length=220,
        description="Primary artist for metadata-only seed (with seed_track) when url is empty",
    )
    seed_track: str | None = Field(
        default=None,
        max_length=320,
        description="Track title for metadata-only seed (with seed_artist) when url is empty",
    )
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
    instrumental_similarity_only: bool = Field(
        default=False,
        description="If true, rank audio similarity with valence/danceability zeroed and blend toward audio scores.",
    )
    filters: SimilarityFilters = Field(default_factory=SimilarityFilters)

    @model_validator(mode="after")
    def url_or_metadata_seed(self) -> "UnifiedSimilarRequest":
        u = (self.url or "").strip()
        a = (self.seed_artist or "").strip() if self.seed_artist else ""
        t = (self.seed_track or "").strip() if self.seed_track else ""
        if u:
            self.url = u
            self.seed_artist = None
            self.seed_track = None
            return self
        if a and t:
            self.url = ""
            self.seed_artist = a
            self.seed_track = t
            return self
        raise ValueError("Provide a Spotify url or both seed_artist and seed_track")

    def resolved_spotify_url(self) -> str:
        """Non-empty Spotify URL for audio sub-requests, or empty when using metadata-only seed."""
        return (self.url or "").strip()


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
    popularity: int | None = None
    release_year: int | None = None
    tags: list[str] = Field(default_factory=list)
    audio_features: AudioFeatures | None = None
    analysis_metrics: dict[str, float | str | bool | None] = Field(default_factory=dict)


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


class TextPlaylistCreateRequest(BaseModel):
    name: str = Field(default="Cat-ID Text Playlist", min_length=1, max_length=200)
    lines: list[str] = Field(default_factory=list, description="Lines in format 'Artist — Track'")


class TextPlaylistUnmatched(BaseModel):
    line: str
    reason: str


class TextPlaylistCreateResponse(BaseModel):
    playlist_id: str
    playlist_url: str
    input_count: int
    matched_count: int
    unmatched: list[TextPlaylistUnmatched] = Field(default_factory=list)


class PlaylistLookupItem(BaseModel):
    id: str
    name: str
    uri: str
    owner: str
    public: bool | None = None
    tracks_total: int | None = None

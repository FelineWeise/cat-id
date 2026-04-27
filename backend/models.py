from pydantic import BaseModel, Field


class TrackRequest(BaseModel):
    url: str = Field(description="Spotify track URL or URI")
    limit: int = Field(default=10, ge=1, le=250, description="Number of similar tracks to return")
    exclude: list[str] = Field(
        default_factory=list,
        description="Lowercased 'artist::trackname' keys to exclude from results",
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


class TrackInfo(BaseModel):
    name: str
    artists: list[str]
    album: str
    album_art: str | None = None
    preview_url: str | None = None
    spotify_url: str | None = None
    spotify_id: str | None = None
    match_score: float | None = None
    bpm: float | None = None
    tags: list[str] = []
    audio_features: AudioFeatures | None = None


class SimilarTracksResponse(BaseModel):
    seed_track: TrackInfo
    similar_tracks: list[TrackInfo]
    seed_tags: list[str] = []
    tag_categories: dict[str, str] = Field(
        default_factory=dict,
        description="Map tag name -> category (mood, genre, vocals_instrumentals) for filter sections",
    )
    approximated: bool = Field(
        default=False,
        description="True when audio features were estimated from tags instead of Spotify's API",
    )

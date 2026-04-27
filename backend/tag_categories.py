"""Tag semantics: UI filter categories and audio-feature estimation from tags.

Categories (for UI filter sections):
- mood: mood/feeling tags
- genre: style, genre, scene
- vocals_instrumentals: vocal presence, instrumental, acoustic

Audio-feature estimation maps Last.fm tags to approximate 0–1 values for
energy, valence, danceability, acousticness, and instrumentalness. Used as a
fallback when Spotify's audio-features endpoint is unavailable.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from backend.models import AudioFeatures

TagCategory = Literal["mood", "genre", "vocals_instrumentals"]

_WHITESPACE_RE = re.compile(r"\s+")

TAG_ALIASES: dict[str, str] = {
    "female vocals": "female vocal",
    "male vocals": "male vocal",
    "no vocals": "no vocal",
}

TAG_TO_CATEGORY: dict[str, TagCategory] = {
    "chill": "mood",
    "chillout": "mood",
    "relaxing": "mood",
    "relaxed": "mood",
    "energetic": "mood",
    "sad": "mood",
    "happy": "mood",
    "dark": "mood",
    "uplifting": "mood",
    "melancholic": "mood",
    "aggressive": "mood",
    "calm": "mood",
    "romantic": "mood",
    "atmospheric": "mood",
    "dreamy": "mood",
    "emotional": "mood",
    "intense": "mood",
    "peaceful": "mood",
    "party": "mood",
    "angry": "mood",
    "epic": "mood",
    "melancholy": "mood",
    "instrumental": "vocals_instrumentals",
    "female vocal": "vocals_instrumentals",
    "female vocals": "vocals_instrumentals",
    "male vocal": "vocals_instrumentals",
    "male vocals": "vocals_instrumentals",
    "vocals": "vocals_instrumentals",
    "vocal": "vocals_instrumentals",
    "acoustic": "vocals_instrumentals",
    "no vocal": "vocals_instrumentals",
    "no vocals": "vocals_instrumentals",
}

DEFAULT_CATEGORY: TagCategory = "genre"

INSTRUMENTAL_TAGS = {"instrumental", "no vocal"}
VOCAL_TAGS = {"vocal", "vocals", "female vocal", "male vocal"}

# ---------------------------------------------------------------------------
# Tag → approximate audio-feature signals
# Each tag maps to partial dimension estimates (0–1). Tags without an entry
# contribute nothing; dimensions without any contributing tag default to 0.5.
# ---------------------------------------------------------------------------
TAG_FEATURE_SIGNALS: dict[str, dict[str, float]] = {
    # Mood → energy / valence
    "energetic": {"energy": 0.9, "danceability": 0.7, "valence": 0.65},
    "aggressive": {"energy": 0.95, "valence": 0.2},
    "intense": {"energy": 0.85, "valence": 0.35},
    "party": {"energy": 0.8, "danceability": 0.85, "valence": 0.7},
    "uplifting": {"energy": 0.7, "valence": 0.8},
    "epic": {"energy": 0.8, "valence": 0.6},
    "angry": {"energy": 0.9, "valence": 0.15},
    "happy": {"valence": 0.85, "energy": 0.65},
    "sad": {"valence": 0.15, "energy": 0.3},
    "melancholic": {"valence": 0.2, "energy": 0.3},
    "melancholy": {"valence": 0.2, "energy": 0.3},
    "dark": {"valence": 0.2, "energy": 0.5},
    "emotional": {"valence": 0.35, "energy": 0.45},
    "romantic": {"valence": 0.6, "acousticness": 0.45},
    # Calm / low energy
    "chill": {"energy": 0.3, "danceability": 0.35, "valence": 0.5},
    "chillout": {"energy": 0.25, "danceability": 0.3},
    "relaxing": {"energy": 0.2, "acousticness": 0.6},
    "relaxed": {"energy": 0.25, "acousticness": 0.55},
    "calm": {"energy": 0.2, "acousticness": 0.6, "valence": 0.5},
    "peaceful": {"energy": 0.15, "acousticness": 0.7, "valence": 0.6},
    "atmospheric": {"energy": 0.35, "acousticness": 0.5},
    "dreamy": {"energy": 0.25, "acousticness": 0.5, "valence": 0.5},
    "ambient": {"energy": 0.15, "acousticness": 0.5, "instrumentalness": 0.7},
    # Danceability
    "dance": {"danceability": 0.85, "energy": 0.7},
    "danceable": {"danceability": 0.85},
    "groovy": {"danceability": 0.8, "energy": 0.6},
    "funky": {"danceability": 0.8, "energy": 0.7},
    # Acousticness / electronic
    "acoustic": {"acousticness": 0.85, "instrumentalness": 0.3},
    "unplugged": {"acousticness": 0.9},
    "electronic": {"acousticness": 0.1, "energy": 0.6},
    "synthpop": {"acousticness": 0.1, "danceability": 0.7},
    "edm": {"acousticness": 0.05, "energy": 0.85, "danceability": 0.8},
    # Instrumentalness / vocals
    "instrumental": {"instrumentalness": 0.9},
    "vocals": {"instrumentalness": 0.1},
    "vocal": {"instrumentalness": 0.1},
    "female vocal": {"instrumentalness": 0.05},
    "female vocals": {"instrumentalness": 0.05},
    "male vocal": {"instrumentalness": 0.05},
    "male vocals": {"instrumentalness": 0.05},
    "no vocal": {"instrumentalness": 0.85},
    "no vocals": {"instrumentalness": 0.85},
    # Genre hints
    "rock": {"energy": 0.7, "acousticness": 0.2},
    "metal": {"energy": 0.9, "acousticness": 0.1, "valence": 0.3},
    "pop": {"danceability": 0.65, "valence": 0.6, "energy": 0.6},
    "hip-hop": {"danceability": 0.75, "energy": 0.65, "acousticness": 0.15},
    "hip hop": {"danceability": 0.75, "energy": 0.65, "acousticness": 0.15},
    "rap": {"danceability": 0.7, "energy": 0.65, "acousticness": 0.1},
    "jazz": {"acousticness": 0.6, "instrumentalness": 0.4},
    "classical": {"acousticness": 0.85, "instrumentalness": 0.8, "energy": 0.3},
    "folk": {"acousticness": 0.7, "energy": 0.35},
    "country": {"acousticness": 0.5, "valence": 0.55},
    "blues": {"acousticness": 0.5, "valence": 0.3, "energy": 0.4},
    "soul": {"valence": 0.5, "acousticness": 0.4, "energy": 0.5},
    "r&b": {"danceability": 0.65, "valence": 0.5},
    "rnb": {"danceability": 0.65, "valence": 0.5},
    "punk": {"energy": 0.85, "acousticness": 0.1},
    "indie": {"acousticness": 0.4, "energy": 0.5},
    "alternative": {"energy": 0.55, "acousticness": 0.3},
    "house": {"danceability": 0.85, "energy": 0.75, "acousticness": 0.05},
    "techno": {"danceability": 0.8, "energy": 0.8, "acousticness": 0.05, "instrumentalness": 0.6},
    "trance": {"energy": 0.75, "danceability": 0.7, "acousticness": 0.05, "instrumentalness": 0.5},
    "reggae": {"danceability": 0.7, "valence": 0.6, "energy": 0.5},
    "latin": {"danceability": 0.8, "energy": 0.65, "valence": 0.7},
    "singer-songwriter": {"acousticness": 0.6, "instrumentalness": 0.1},
}

_ESTIMABLE_DIMS = ("energy", "valence", "danceability", "acousticness", "instrumentalness")


def get_category(tag: str) -> TagCategory:
    """Return the category for a tag (lowercase). Unknown tags map to genre."""
    return TAG_TO_CATEGORY.get(normalize_tag(tag), DEFAULT_CATEGORY)


def build_tag_categories(tags: list[str]) -> dict[str, str]:
    """Build tag -> category for a list of tags. Returns dict suitable for JSON."""
    return {tag: get_category(tag) for tag in tags}


def normalize_tag(tag: str) -> str:
    """Normalize tags for stable matching across UI and backend rules."""
    normalized = _WHITESPACE_RE.sub(" ", tag.strip().lower())
    return TAG_ALIASES.get(normalized, normalized)


def _collect_signals(tag: str, sums: dict[str, float], counts: dict[str, int]) -> None:
    """Accumulate feature signals for a tag. For compound tags like 'ambient house',
    also checks individual words so partial matches contribute."""
    key = normalize_tag(tag)
    signals = TAG_FEATURE_SIGNALS.get(key)
    if signals:
        for dim, val in signals.items():
            if dim in sums:
                sums[dim] += val
                counts[dim] += 1
        return

    for word in key.split():
        signals = TAG_FEATURE_SIGNALS.get(word)
        if signals:
            for dim, val in signals.items():
                if dim in sums:
                    sums[dim] += val
                    counts[dim] += 1


def estimate_features_from_tags(tags: list[str], bpm: float | None = None) -> AudioFeatures:
    """Approximate audio features from Last.fm tags (+ optional Deezer BPM).

    For each dimension, averages the signals from all matching tags.
    Compound tags (e.g. 'ambient house') are split so individual words
    can match. Dimensions with no signal default to 0.5 (neutral).
    """
    from backend.models import AudioFeatures as AF

    sums: dict[str, float] = {d: 0.0 for d in _ESTIMABLE_DIMS}
    counts: dict[str, int] = {d: 0 for d in _ESTIMABLE_DIMS}

    for tag in tags:
        _collect_signals(tag, sums, counts)

    values: dict[str, float | None] = {"tempo": bpm}
    for dim in _ESTIMABLE_DIMS:
        values[dim] = sums[dim] / counts[dim] if counts[dim] else 0.5

    return AF(**values)


def tag_alignment_score(seed_tags: list[str], candidate_tags: list[str]) -> float:
    """Return normalized alignment score between seed and candidate tags (0..1)."""
    seed_norm = {normalize_tag(tag) for tag in seed_tags if tag.strip()}
    cand_norm = {normalize_tag(tag) for tag in candidate_tags if tag.strip()}
    if not seed_norm or not cand_norm:
        return 0.0
    overlap = len(seed_norm & cand_norm)
    return overlap / max(1, min(len(seed_norm), 8))

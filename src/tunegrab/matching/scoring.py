"""Candidate scoring per docs/ARCHITECTURE.md.

| Signal | Rule |
|---|---|
| ISRC exact match | instant 100 |
| Title (token_set_ratio of normalised titles) | 40% |
| Artist (best overlap of artist sets) | 30% |
| Duration | 30%: full <= 3 s, linear to 0 at >= 20 s (official music videos up to
  90 s longer get at least half: intros/outros) |
| Source | +5 for YouTube Music "song" results |
| Penalty | -30 for live/cover/remix/... in the candidate but not the Spotify title |
| Different artist | -20 when the artist similarity is below 50 |

Artist overlap: exact-ish name match scores up to 100; one name contained in the other
("Prince" / "Prince & The Revolution") scores 85; an uploader that only *mentions* the
artist in the video title scores 70 (so official uploads win over lyric/vinyl channels).
"""

from __future__ import annotations

import re
import unicodedata

from rapidfuzz import fuzz

from tunegrab.models import Candidate, Track

TITLE_WEIGHT = 40.0
ARTIST_WEIGHT = 30.0
DURATION_WEIGHT = 30.0
SONG_BONUS = 5.0
PENALTY = 30.0
DURATION_FULL_S = 3.0
DURATION_ZERO_S = 20.0
MUSIC_VIDEO_PADDING_S = 90.0
SUBSET_ARTIST_SCORE = 85.0
TITLE_ONLY_ARTIST_SCORE = 70.0
ARTIST_MISMATCH_BELOW = 50.0
ARTIST_MISMATCH_PENALTY = 20.0
REVIEW_BELOW = 85.0  # accepted matches under this are shown as "Check this match"

# Matched on lowercase, accent-free text with "-" turned into spaces.
PENALTY_WORDS: tuple[str, ...] = (
    "live",
    "cover",
    "remix",
    "sped up",
    "slowed",
    "nightcore",
    "karaoke",
    "instrumental",
    "8d",
    # versions/uploads that are not the original recording either:
    "re recorded",
    "rerecorded",
    "reverb",
    "bass boosted",
    "acapella",
    "a cappella",
    "vinyl",
    "tutorial",
    "lesson",
    "tab",
    "play along",
    "sing along",
    "singalong",
    "piano",
    "guitar",
    "drum",
    "reaction",
    "mashup",
    "parody",
    "lofi",
    "lo fi",
    # cover-factory wording:
    "made famous by",
    "as made famous",
    "in the style of",
    "originally performed",
    "tribute",
    # other recordings of the same song:
    "extended",
    "acoustic",
    "unplugged",
    "demo",
)
_PENALTY_PATTERNS = {w: re.compile(rf"(?<![\w]){re.escape(w)}(?![\w])") for w in PENALTY_WORDS}

_FEAT_BRACKET = re.compile(
    r"[\(\[]\s*(?:feat\.?|ft\.?|featuring|with)\s[^\)\]]*[\)\]]", re.IGNORECASE
)
_FEAT_TAIL = re.compile(r"\s(?:feat\.?|ft\.?|featuring)\s.*$", re.IGNORECASE)
_REMASTER_TAIL = re.compile(r"\s+-\s+[^-]*\bremaster(?:ed)?\b.*$", re.IGNORECASE)
_REMASTER_BRACKET = re.compile(r"[\(\[][^\)\]]*\bremaster(?:ed)?\b[^\)\]]*[\)\]]", re.IGNORECASE)
_JUNK_BRACKET = re.compile(
    r"[\(\[]\s*(?:official\s*)?(?:music\s*|lyric\s*|lyrics\s*|audio\s*|hd\s*|hq\s*|4k\s*|"
    r"visuali[sz]er\s*|video\s*|mv\s*|m/v\s*)+(?:video)?\s*[\)\]]",
    re.IGNORECASE,
)
_JUNK_WORDS = re.compile(
    r"\b(?:official\s+(?:music\s+)?(?:video|audio)|lyric video)\b", re.IGNORECASE
)
_MUSIC_VIDEO = re.compile(
    r"\b(?:official\s+(?:music\s+)?video|music\s+video|m/v|mv)\b", re.IGNORECASE
)
_PUNCT = re.compile(r"[^\w\s]")
_SPACES = re.compile(r"\s+")


_LATIN_END = "\u0250"  # end of Latin Extended-B


def strip_accents(text: str) -> str:
    """Drop accents from Latin letters (Beyoncé -> Beyonce) but keep marks that make
    a different letter in other scripts (Japanese が stays が, Russian й stays й)."""
    kept: list[str] = []
    for ch in unicodedata.normalize("NFKD", text):
        if unicodedata.combining(ch) and kept and kept[-1] < _LATIN_END:
            continue
        kept.append(ch)
    return unicodedata.normalize("NFC", "".join(kept))


def normalise(text: str) -> str:
    """Lowercase, drop feat./remaster/[Official Video] noise, punctuation and accents."""
    s = _FEAT_BRACKET.sub(" ", text)
    s = _REMASTER_BRACKET.sub(" ", s)
    s = _REMASTER_TAIL.sub(" ", s)
    s = _JUNK_BRACKET.sub(" ", s)
    s = _JUNK_WORDS.sub(" ", s)
    s = _FEAT_TAIL.sub(" ", s)
    s = strip_accents(s).lower().replace("&", " and ")
    s = _PUNCT.sub(" ", s.replace("_", " "))
    return _SPACES.sub(" ", s).strip()


def _penalty_text(text: str) -> str:
    return _SPACES.sub(" ", strip_accents(text).lower().replace("-", " "))


def penalty_words(
    track_title: str,
    candidate_title: str,
    track_artists: list[str] | None = None,
    candidate_artists: list[str] | None = None,
) -> list[str]:
    """Version words in the candidate (title or artist names, e.g. "Sunfly Karaoke")
    that the Spotify track's title and artists don't have."""
    cand = _penalty_text(" ".join([candidate_title, *(candidate_artists or [])]))
    orig = _penalty_text(" ".join([track_title, *(track_artists or [])]))
    return [
        word
        for word, pattern in _PENALTY_PATTERNS.items()
        if pattern.search(cand) and not pattern.search(orig)
    ]


def title_similarity(track: Track, candidate: Candidate) -> float:
    a, b = normalise(track.title), normalise(candidate.title)
    if not a or not b:  # e.g. titles made only of symbols / non-word chars
        return float(fuzz.ratio(track.title.lower(), candidate.title.lower()))
    return float(fuzz.token_set_ratio(a, b))


def _contains_words(haystack: str, needle: str) -> bool:
    return f" {needle} " in f" {haystack} "


def artist_similarity(track: Track, candidate: Candidate) -> float:
    """How well the candidate's artists match ours (0-100); see the module docstring."""
    ours = [n for a in track.artists if (n := normalise(a))]
    theirs = [n for a in candidate.artists if (n := normalise(a))]
    if not ours:
        return 50.0
    best = max((float(fuzz.token_sort_ratio(a, b)) for a in ours for b in theirs), default=0.0)
    if theirs:
        best = max(best, float(fuzz.token_sort_ratio(" ".join(ours), " ".join(theirs))))
    if best < SUBSET_ARTIST_SCORE and any(
        _contains_words(a, b) or _contains_words(b, a) for a in ours for b in theirs
    ):
        best = SUBSET_ARTIST_SCORE
    # For YT Music "songs" the artist field is authoritative: a song that merely names
    # our artist in its title is a cover ("... in the style of X").
    if best < TITLE_ONLY_ARTIST_SCORE and candidate.source != "ytmusic_song":
        cand_title = normalise(candidate.title)
        if any(_contains_words(cand_title, a) for a in ours):
            best = TITLE_ONLY_ARTIST_SCORE
    return best


def padded_music_video(track: Track, candidate: Candidate) -> bool:
    """An official music video that is longer than the song by an intro/outro."""
    if candidate.source == "ytmusic_song" or not track.duration_ms or not candidate.duration_s:
        return False
    extra = candidate.duration_s - track.duration_s
    return DURATION_FULL_S < extra <= MUSIC_VIDEO_PADDING_S and bool(
        _MUSIC_VIDEO.search(candidate.title)
    )


def duration_score(track: Track, candidate: Candidate) -> float | None:
    """1.0 for <= 3 s difference, linear to 0.0 at >= 20 s; None when unknown.
    Padded official music videos score at least 0.5 (as if the duration were unknown)."""
    if not track.duration_ms or not candidate.duration_s:
        return None
    diff = abs(track.duration_s - candidate.duration_s)
    if diff <= DURATION_FULL_S:
        value = 1.0
    elif diff >= DURATION_ZERO_S:
        value = 0.0
    else:
        value = 1.0 - (diff - DURATION_FULL_S) / (DURATION_ZERO_S - DURATION_FULL_S)
    return max(value, 0.5) if padded_music_video(track, candidate) else value


def review_note(track: Track, candidate: Candidate, value: float) -> str | None:
    """Why an accepted match deserves a second look, or None when it doesn't. A version
    word costs 30 points, so every penalised match is also under REVIEW_BELOW."""
    if candidate.source == "manual" or value >= REVIEW_BELOW:
        return None
    bad = penalty_words(track.title, candidate.title, track.artists, candidate.artists)
    if bad:
        return (
            f"The video says \u201c{bad[0]}\u201d and the song doesn't: it may be another version."
        )
    return f"Low match score ({value:.0f} of 100): it may be the wrong recording."


def score(track: Track, candidate: Candidate) -> tuple[float, str]:
    """Return (score 0-100, human-readable reason)."""
    if track.isrc and candidate.isrc and track.isrc.upper() == candidate.isrc.upper():
        return 100.0, "ISRC match"
    title = title_similarity(track, candidate)
    artist = artist_similarity(track, candidate)
    duration = duration_score(track, candidate)
    duration_part = DURATION_WEIGHT * (0.5 if duration is None else duration)
    total = TITLE_WEIGHT * title / 100 + ARTIST_WEIGHT * artist / 100 + duration_part
    reasons = [f"title {title:.0f}", f"artist {artist:.0f}"]
    if duration is None:
        reasons.append("duration unknown")
    else:
        diff = abs(track.duration_s - candidate.duration_s)
        reasons.append(
            f"duration Δ{diff:.0f}s"
            + (" (music video)" if padded_music_video(track, candidate) else "")
        )
    if candidate.source == "ytmusic_song":
        total += SONG_BONUS
        reasons.append("+5 YT Music song")
    bad = penalty_words(track.title, candidate.title, track.artists, candidate.artists)
    if bad:
        total -= PENALTY
        reasons.append(f"-30 '{bad[0]}' version")
    if artist < ARTIST_MISMATCH_BELOW:
        total -= ARTIST_MISMATCH_PENALTY
        reasons.append("-20 different artist")
    return max(0.0, min(100.0, total)), ", ".join(reasons)

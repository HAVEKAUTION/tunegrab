import json
from typing import Any

import pytest

from tests.conftest import FIXTURES
from tunegrab.matching.scoring import (
    artist_similarity,
    duration_score,
    normalise,
    penalty_words,
    score,
)
from tunegrab.models import Candidate, Track

THRESHOLD = 70.0
CASES: list[dict[str, Any]] = json.loads((FIXTURES / "matching_cases.json").read_text("utf-8"))


def track(title: str, artists: list[str], duration_s: float, **extra: Any) -> Track:
    return Track(title=title, artists=artists, duration_ms=int(duration_s * 1000), **extra)


def cand(
    title: str, artists: list[str], duration_s: int, source: str = "ytmusic_song", **extra: Any
) -> Candidate:
    return Candidate(
        video_id="x" * 11,
        title=title,
        artists=artists,
        duration_s=duration_s,
        source=source,
        **extra,
    )  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Song (feat. Someone)", "song"),
        ("Song [ft. A & B]", "song"),
        ("Song (with Friend)", "song"),
        ("Song feat. X", "song"),
        ("Song - Remastered 2011", "song"),
        ("Song - 2016 Remaster", "song"),
        ("Song (2009 Remastered Version)", "song"),
        ("Song [Official Video]", "song"),
        ("Song (Official Music Video)", "song"),
        ("Song (Lyrics)", "song"),
        ("Song (Official Audio)", "song"),
        ("Beyoncé – Café", "beyonce cafe"),
        ("Rock & Roll", "rock and roll"),
        ("  Hello,   World!!  ", "hello world"),
        ("アイドル", "アイトル"),  # NFKD splits the voiced mark; both sides get the same
    ],
)
def test_normalise(raw: str, expected: str) -> None:
    assert normalise(raw) == expected


def test_duration_score() -> None:
    t = track("x", ["a"], 200)
    assert duration_score(t, cand("x", ["a"], 203)) == 1.0
    assert duration_score(t, cand("x", ["a"], 220)) == 0.0
    assert duration_score(t, cand("x", ["a"], 212)) == pytest.approx(1 - 9 / 17)
    assert duration_score(t, cand("x", ["a"], 0)) is None
    assert duration_score(track("x", ["a"], 0), cand("x", ["a"], 200)) is None


def test_penalty_words() -> None:
    assert penalty_words("Song", "Song (Live)") == ["live"]
    assert penalty_words("Song - Live", "Song (Live)") == []
    assert penalty_words("Alive", "Alive") == []
    assert penalty_words("Song", "Song - Sped-Up") == ["sped up"]
    assert penalty_words("Song", "Song", ["A"], ["Sunfly Karaoke"]) == ["karaoke"]
    assert penalty_words("Piano Man", "Piano Man") == []


def test_artist_similarity_rules() -> None:
    t = track("Kiss", ["Prince"], 226)
    assert artist_similarity(t, cand("Kiss", ["Prince"], 0)) == 100
    assert artist_similarity(t, cand("Kiss", ["Prince & The Revolution"], 0)) == 85
    assert artist_similarity(t, cand("Prince - Kiss", ["Lyrics Channel"], 0, "youtube")) == 70
    # for YT Music songs the artist field is authoritative:
    assert artist_similarity(t, cand("Kiss (in the style of Prince)", ["X"], 0)) < 50
    assert artist_similarity(track("x", [], 1), cand("x", ["a"], 0)) == 50


def test_isrc_wins() -> None:
    t = track("A", ["B"], 100, isrc="USX1")
    value, reason = score(t, cand("Totally different", ["C"], 999, isrc="usx1"))
    assert (value, reason) == (100.0, "ISRC match")


def test_perfect_match_scores_100_and_reason() -> None:
    value, reason = score(track("Song", ["Artist"], 200), cand("Song", ["Artist"], 201))
    assert value == 100
    assert reason == "title 100, artist 100, duration Δ1s, +5 YT Music song"


def test_score_is_clamped() -> None:
    value, _ = score(track("A", ["B"], 100), cand("Zzz Live Karaoke", ["Q"], 999))
    assert value == 0.0


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_matching_cases(case: dict[str, Any]) -> None:
    t = track(**case["track"])
    candidates = [cand(**c) for c in case["candidates"]]
    scored = [score(t, c) for c in candidates]
    best = max(range(len(scored)), key=lambda i: scored[i][0])
    detail = "\n".join(
        f"{c.title} / {c.artists}: {s:.1f} ({r})"
        for c, (s, r) in zip(candidates, scored, strict=True)
    )
    if case["expect"] is None:
        assert scored[best][0] < THRESHOLD, detail
    else:
        assert best == case["expect"], detail
        assert scored[best][0] >= THRESHOLD, detail


def test_enough_cases() -> None:
    assert len(CASES) >= 30

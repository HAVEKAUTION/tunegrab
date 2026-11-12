from typing import Any

import pytest

from tests.fakes import FakeAudio, FakeYTMusicClient, candidate_for, make_track
from tunegrab.library.archive import Archive
from tunegrab.matching.base import SearchError, build_query, parse_duration
from tunegrab.matching.matcher import Matcher
from tunegrab.matching.youtube import candidate_from_info, clean_channel
from tunegrab.matching.ytmusic import (
    YTMusicClient,
    YTMusicProvider,
    best_thumbnail,
    candidate_from_result,
    track_from_candidate,
)


def test_parse_duration_and_query() -> None:
    assert parse_duration("3:45") == 225
    assert parse_duration("1:02:03") == 3723
    assert parse_duration("") == parse_duration("abc") == 0
    t = make_track(1, artists=["A", "B", "C"])
    assert build_query(t) == "A B Song 1"


def test_ytmusic_result_mapping() -> None:
    result = {
        "videoId": "v1",
        "title": "T",
        "duration": "2:05",
        "artists": [{"name": "X"}, {"name": None}],
        "album": {"name": "Alb"},
        "thumbnails": [
            {"url": "https://lh3/a=w60-h60", "width": 60, "height": 60},
            {"url": "https://lh3/a=w120-h120", "width": 120, "height": 120},
        ],
    }
    c = candidate_from_result(result, "ytmusic_song")
    assert c is not None
    assert (c.video_id, c.duration_s, c.artists, c.album) == ("v1", 125, ["X"], "Alb")
    assert c.thumbnail_url == "https://lh3/a=w1200-h1200"
    assert candidate_from_result({"title": "no id"}, "ytmusic_song") is None
    assert best_thumbnail(None) is None


def test_ytmusic_provider_and_errors() -> None:
    provider = YTMusicProvider(YTMusicClient(FakeYTMusicClient()), "songs")
    [c] = provider.search(make_track(1))
    assert c.source == "ytmusic_song" and provider.name == "ytmusic_song"
    assert (
        YTMusicProvider(YTMusicClient(FakeYTMusicClient()), "videos").search_query("q")[0].source
        == "ytmusic_video"
    )

    class Exploding:
        def search(self, *a: Any, **k: Any) -> Any:
            raise KeyError("parse error")

    with pytest.raises(SearchError):
        YTMusicProvider(YTMusicClient(Exploding()), "songs").search_query("q")


def test_track_from_candidate() -> None:
    c = candidate_for(make_track(1), album="LP", thumbnail_url="t")
    t = track_from_candidate(c)
    assert (t.title, t.album, t.album_artist, t.duration_ms, t.cover_url) == (
        "Song 1",
        "LP",
        "Artist 1",
        200_000,
        "t",
    )
    assert t.spotify_id is None and t.key.startswith("q:")


def test_youtube_info_mapping() -> None:
    assert clean_channel("Daft Punk - Topic") == "Daft Punk"
    assert clean_channel("DaftPunkVEVO") == "DaftPunk"
    c = candidate_from_info(
        {
            "id": "abc",
            "title": "Song",
            "duration": 200.4,
            "channel": "Band - Topic",
            "ie_key": "Youtube",
        }
    )
    assert c is not None and c.artists == ["Band"] and c.duration_s == 200
    rich = candidate_from_info(
        {"id": "abc", "title": "Vid", "track": "Song", "artists": ["A"], "album": "LP"}
    )
    assert rich is not None and (rich.title, rich.artists, rich.album) == ("Song", ["A"], "LP")
    assert candidate_from_info({"id": "x", "title": "t", "ie_key": "YoutubeTab"}) is None


def test_matcher_stops_early_on_strong_match() -> None:
    first, second = FakeAudio("one"), FakeAudio("two")
    match = Matcher([first, second]).match(make_track(1))
    assert match.score == 100 and (first.calls, second.calls) == (1, 0)


def test_matcher_tries_next_provider_and_survives_errors() -> None:
    t = make_track(1)
    weak = FakeAudio(
        "weak", results={t.title: [candidate_for(t, artists=["Nobody"], duration_s=10)]}
    )
    good = FakeAudio("good")
    match = Matcher([FakeAudio("broken", fail=True), weak, good]).match(t)
    assert match.candidate is not None and match.score == 100 and good.calls == 1


def test_matcher_no_results() -> None:
    match = Matcher([FakeAudio(results={}), FakeAudio("b", fail=True)]).match(make_track(1))
    assert match.candidate is None and match.score == 0
    assert "no search results" in match.reason and "boom" in match.reason


def test_matcher_cache(tmp_path: Any) -> None:
    archive = Archive(tmp_path / "a.db")
    audio = FakeAudio()
    matcher = Matcher([audio], cache=archive)
    t = make_track(1)
    assert not matcher.match(t).cached
    again = matcher.match(t)
    assert again.cached and audio.calls == 1
    assert not matcher.match(t, use_cache=False).cached and audio.calls == 2


def test_matcher_does_not_cache_rejects(tmp_path: Any) -> None:
    archive = Archive(tmp_path / "a.db")
    t = make_track(1)
    bad = FakeAudio(results={t.title: [candidate_for(t, title="Other", artists=["Z"])]})
    Matcher([bad], cache=archive).match(t)
    assert archive.get_match(t.key) is None

"""Real-network smoke tests. Run with: uv run pytest -m live -q"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from tunegrab.config import Settings
from tunegrab.metadata.spotify_embed import SpotifyEmbedProvider
from tunegrab.net import HttpClient
from tunegrab.pipeline import RATE_LIMIT_GAVE_UP
from tunegrab.services import build_services
from tunegrab.urls import parse_spotify_url

pytestmark = pytest.mark.live

TRACK = "https://open.spotify.com/track/0DiWol3AO6WpXZgp0goxAV"  # Daft Punk - One More Time
ALBUM = "https://open.spotify.com/album/2noRn2Aes5aoNVsU6iWThc"  # Discovery


def test_embed_track_and_album() -> None:
    provider = SpotifyEmbedProvider(HttpClient())
    track = provider.get_track(parse_spotify_url(TRACK).id or "")
    assert "One More Time" in track.title and "Daft Punk" in track.artists
    assert track.album and track.duration_ms and track.cover_url
    album = provider.get_album(parse_spotify_url(ALBUM).id or "")
    assert album.name == "Discovery" and len(album.tracks) >= 14


def test_match_finds_the_song(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, output_dir=tmp_path)
    services = build_services(settings)
    try:
        collection = services.resolver.resolve(TRACK)
        match = services.matcher.match(collection.tracks[0], use_cache=False)
        assert match.accepted(settings.match_threshold), match.reason
    finally:
        services.close()


@pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("deno")), reason="needs ffmpeg + deno"
)
@pytest.mark.skipif(
    os.environ.get("GITHUB_ACTIONS") == "true",
    reason="YouTube asks datacenter IPs (GitHub runners) to sign in; run this test locally",
)
def test_full_download(tmp_path: Path) -> None:
    import asyncio

    from mutagen.id3 import ID3

    settings = Settings(_env_file=None, output_dir=tmp_path, threads=1)
    services = build_services(settings)
    try:
        pipeline = services.pipeline()
        # Don't sit out YouTube's hour-long block in a test: give up after the strikes.
        pipeline.rate_limit_pause = pipeline.rate_limit_block = 0.0
        job = asyncio.run(pipeline.run(pipeline.build_job(TRACK)))
    finally:
        services.close()
    if job.error == RATE_LIMIT_GAVE_UP:
        pytest.skip("YouTube is rate-limiting this connection right now; try again in an hour")
    task = job.tasks[0]
    assert task.status.value == "done", task.error
    assert task.path
    path = Path(task.path)
    assert path.exists() and path.suffix == ".mp3"
    tags = ID3(path)
    assert "One More Time" in str(tags["TIT2"]) and tags.getall("APIC")

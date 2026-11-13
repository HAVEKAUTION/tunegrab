"""Measure TuneGrab against the performance targets in the product spec (section 7).

Builds a throwaway profile with a 10,000-song download history (files are empty
placeholders), starts the real server on it and times:
  - `serve` start until /api/health answers      (target <= 2 s)
  - GET /api/library for 10,000 songs            (target <= 1 s)
  - server memory (RSS) after those requests     (target <= 300 MB during a job)

Browser-side numbers (tab switch, library filter per keystroke, tab memory) need a
browser; the script prints the page URL and keeps the server up with --keep.

Run: uv run python scripts/benchmark.py [--songs 10000] [--keep]
"""

from __future__ import annotations

import argparse
import os
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def build_profile(root: Path, songs: int) -> None:
    os.environ["TUNEGRAB_DATA_DIR"] = str(root / "data")
    from tunegrab.config import Settings
    from tunegrab.library.archive import Archive
    from tunegrab.models import Track

    settings = Settings(_env_file=None, output_dir=root / "Music")
    archive = Archive(settings.resolved_archive_path)
    music = root / "Music"
    for i in range(songs):
        artist, album = f"Artist {i // 120:03d}", f"Album {i // 12:04d}"
        folder = music / artist / album
        if i % 12 == 0:
            folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{i % 12 + 1:02d} Song {i}.m4a"
        path.touch()
        track = Track(
            title=f"Song {i}",
            artists=[artist],
            album=album,
            album_artist=artist,
            track_number=i % 12 + 1,
            duration_ms=200_000,
            spotify_id=f"{i:022d}",
        )
        archive.record_download(track, path, f"vid{i:08d}", "m4a", source="Opus 129 kbps")
    archive.close()


def rss_mb(pid: int) -> float | None:
    status = Path(f"/proc/{pid}/status")
    if status.exists():  # Linux
        for line in status.read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    if sys.platform == "win32":  # working set, as Task Manager shows it
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        digits = "".join(ch for ch in out.rsplit(",", 1)[-1] if ch.isdigit())
        return int(digits) / 1024 if digits else None
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--songs", type=int, default=10_000)
    parser.add_argument("--keep", action="store_true", help="leave the server running")
    args = parser.parse_args()

    root = Path(tempfile.mkdtemp(prefix="tunegrab-bench-"))
    env = {
        **os.environ,
        "TUNEGRAB_CONFIG_DIR": str(root / "config"),
        "TUNEGRAB_DATA_DIR": str(root / "data"),
        "TUNEGRAB_LOG_DIR": str(root / "logs"),
        "TUNEGRAB_NO_KEYRING": "1",
        "TUNEGRAB_CHECK_UPDATES": "false",
        "TUNEGRAB_OUTPUT_DIR": str(root / "Music"),
    }
    os.environ.update({k: v for k, v in env.items() if k.startswith("TUNEGRAB_")})
    started = time.perf_counter()
    build_profile(root, args.songs)
    print(f"profile with {args.songs} songs built in {time.perf_counter() - started:.1f} s: {root}")

    port = free_port()
    command = [sys.executable, "-m", "tunegrab", "serve", "--port", str(port), "--no-open"]
    started = time.perf_counter()
    server = subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    base = f"http://127.0.0.1:{port}"
    try:
        with httpx.Client(timeout=30) as http:
            while True:
                if server.poll() is not None:
                    print(server.stderr.read().decode(errors="replace") if server.stderr else "")
                    raise SystemExit("server exited")
                try:
                    if http.get(f"{base}/api/health").status_code == 200:
                        break
                except httpx.TransportError:
                    time.sleep(0.05)
            ready = time.perf_counter() - started
            times = []
            for _ in range(5):
                t0 = time.perf_counter()
                body = http.get(f"{base}/api/library").json()
                times.append(time.perf_counter() - t0)
            count = len(body["tracks"])
        memory = rss_mb(server.pid)
        rows = [
            ("serve -> /api/health", f"{ready:.2f} s", "<= 2 s", ready <= 2),
            (
                f"/api/library, {count} songs (median of 5)",
                f"{statistics.median(times):.2f} s",
                "<= 1 s",
                statistics.median(times) <= 1,
            ),
            (
                "server RSS after library requests",
                f"{memory:.0f} MB" if memory is not None else "n/a",
                "<= 300 MB",
                memory is None or memory <= 300,
            ),
        ]
        for name, value, target, ok in rows:
            print(f"{'OK ' if ok else 'OVER'} {name:42} {value:>14}  target {target}", flush=True)
        if args.keep:
            print(f"server running at {base}/#library (Ctrl+C to stop)", flush=True)
            server.wait()
        return 0 if all(r[3] for r in rows) else 1
    finally:
        if server.poll() is None:
            server.terminate()
            server.wait(10)


if __name__ == "__main__":
    sys.exit(main())

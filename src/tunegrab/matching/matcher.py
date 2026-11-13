"""Pick the best candidate for a track across providers, with an SQLite cache."""

from __future__ import annotations

import logging
from typing import Protocol

from tunegrab.matching.base import AudioProvider, SearchError
from tunegrab.matching.scoring import score
from tunegrab.models import Alternative, Candidate, Match, Track

log = logging.getLogger(__name__)

STRONG_MATCH = 90.0
MAX_ALTERNATIVES = 3  # next-best videos kept with a match, for "Check this match"


class MatchCache(Protocol):
    def get_match(self, key: str) -> tuple[Candidate, float, str] | None: ...

    def put_match(
        self,
        key: str,
        candidate: Candidate,
        score: float,
        reason: str,
        manual: bool = False,
        alternatives: list[Alternative] | None = None,
    ) -> None: ...


def alternatives_to(best: Match, ranked: list[Match]) -> list[Alternative]:
    """The next-best distinct videos after ``best`` (the same video can come from two
    providers; its higher score is kept)."""
    chosen = best.candidate.video_id if best.candidate else None
    seen: set[str] = set()
    found: list[Alternative] = []
    for match in sorted(ranked, key=lambda m: m.score, reverse=True):
        candidate = match.candidate
        if candidate is None or candidate.video_id == chosen or candidate.video_id in seen:
            continue
        seen.add(candidate.video_id)
        found.append(Alternative(candidate=candidate, score=match.score, reason=match.reason))
        if len(found) == MAX_ALTERNATIVES:
            break
    return found


class Matcher:
    def __init__(
        self,
        providers: list[AudioProvider],
        threshold: float = 70.0,
        cache: MatchCache | None = None,
        strong: float = STRONG_MATCH,
    ) -> None:
        self.providers = providers
        self.threshold = threshold
        self.cache = cache
        self.strong = strong

    def rank(self, track: Track, candidates: list[Candidate]) -> list[Match]:
        matches = []
        for candidate in candidates:
            value, reason = score(track, candidate)
            matches.append(Match(track=track, candidate=candidate, score=value, reason=reason))
        return sorted(matches, key=lambda m: m.score, reverse=True)

    def match(self, track: Track, use_cache: bool = True, exclude: set[str] | None = None) -> Match:
        """Best candidate for ``track``. ``exclude`` skips video IDs that already failed
        (e.g. unavailable in this country); the cache is bypassed when it's given."""
        if exclude:
            use_cache = False
        if use_cache and self.cache is not None:
            hit = self.cache.get_match(track.key)
            if hit is not None:
                candidate, value, reason = hit
                return Match(
                    track=track, candidate=candidate, score=value, reason=reason, cached=True
                )

        best: Match | None = None
        everything: list[Match] = []
        errors: list[str] = []
        for provider in self.providers:
            try:
                candidates = provider.search(track)
            except SearchError as exc:
                log.info("%s search failed for %s: %s", provider.name, track.display, exc)
                errors.append(str(exc))
                continue
            if exclude:
                candidates = [c for c in candidates if c.video_id not in exclude]
            ranked = self.rank(track, candidates)
            everything += ranked
            if ranked and (best is None or ranked[0].score > best.score):
                best = ranked[0]
            if best is not None and best.score >= self.strong:
                break

        if best is None:
            reason = "no search results" + (f" ({'; '.join(errors)})" if errors else "")
            return Match(track=track, candidate=None, score=0.0, reason=reason)
        best.alternatives = alternatives_to(best, everything)
        if self.cache is not None and best.candidate is not None and best.accepted(self.threshold):
            self.cache.put_match(
                track.key, best.candidate, best.score, best.reason, alternatives=best.alternatives
            )
        return best

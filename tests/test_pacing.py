"""The YouTube pacer (token bucket) with a fake clock."""

from __future__ import annotations

import pytest

from tunegrab import pacing
from tunegrab.pacing import GUEST_PER_HOUR as GUEST  # read before conftest zeroes it
from tunegrab.pacing import Pacer


def make(per_hour: float, burst: int) -> tuple[Pacer, list[float]]:
    clock = [0.0]
    return Pacer(per_hour, burst=burst, clock=lambda: clock[0]), clock


def test_burst_then_paced() -> None:
    pacer, _ = make(3600, burst=3)  # one start per second after the burst
    assert [pacer.reserve() for _ in range(3)] == [0.0, 0.0, 0.0]
    assert pacer.reserve() == pytest.approx(1.0)
    assert pacer.reserve() == pytest.approx(2.0)  # each caller books its own slot


def test_tokens_refill_over_time_up_to_the_burst() -> None:
    pacer, clock = make(3600, burst=2)
    pacer.reserve()
    pacer.reserve()
    clock[0] += 10.0  # far more than needed: refills only to the burst size
    assert [pacer.reserve() for _ in range(2)] == [0.0, 0.0]
    assert pacer.reserve() == pytest.approx(1.0)


def test_guest_budget_is_250_an_hour() -> None:
    assert GUEST == 250
    pacer, _ = make(GUEST, burst=0)
    assert pacer.reserve() == pytest.approx(3600 / 250)


def test_zero_rate_means_unlimited() -> None:
    pacer, _ = make(0, burst=0)
    assert pacer.unlimited and pacer.reserve() == 0.0


def test_shared_pacer_follows_the_cookie_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pacing, "_shared", None)
    monkeypatch.setattr(pacing, "GUEST_PER_HOUR", 250)
    monkeypatch.setattr(pacing, "SIGNED_IN_PER_HOUR", 1000)
    guest = pacing.youtube_pacer(signed_in=False)
    assert guest.per_hour == 250
    assert pacing.youtube_pacer(signed_in=True) is guest and guest.per_hour == 1000

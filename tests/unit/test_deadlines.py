"""Settlement period timing, especially on the days it goes wrong.

The clock-change days are the point of this module. Every other day is
arithmetic; those two are where an assumption about 48 periods, or about
subtracting aware datetimes, produces a wrong answer on a day that often
carries unusual prices.

Dates used throughout:

    2026-03-29  clocks forward, 23 hours, 46 periods
    2026-10-25  clocks back,    25 hours, 50 periods
    2026-09-01  ordinary,       24 hours, 48 periods, BST in force
    2026-01-15  ordinary,       24 hours, 48 periods, GMT in force
"""

from __future__ import annotations

import datetime as dt

import pytest

from consus_elexon_settlement import deadlines
from consus_elexon_settlement.deadlines import LONDON, UTC

SPRING = dt.date(2026, 3, 29)     # 46 periods
AUTUMN = dt.date(2026, 10, 25)    # 50 periods
BST_DAY = dt.date(2026, 9, 1)     # 48, clocks forward one hour
GMT_DAY = dt.date(2026, 1, 15)    # 48, UTC equals local


# --- how many periods -------------------------------------------------------

def test_ordinary_day_has_48_periods():
    assert deadlines.periods_in_day(BST_DAY) == 48
    assert deadlines.periods_in_day(GMT_DAY) == 48


def test_spring_forward_day_has_46():
    """The clocks go forward at 01:00, so 01:00 and 01:30 never happen."""
    assert deadlines.periods_in_day(SPRING) == 46


def test_autumn_back_day_has_50():
    """The clocks go back at 02:00, so 01:00 and 01:30 happen twice."""
    assert deadlines.periods_in_day(AUTUMN) == 50


def test_period_counts_hold_across_years():
    """The rule is derived from the timezone database, not hardcoded, so it
    should keep working when the dates move."""
    assert deadlines.periods_in_day(dt.date(2027, 3, 28)) == 46
    assert deadlines.periods_in_day(dt.date(2027, 10, 31)) == 50


# --- where a period starts --------------------------------------------------

def test_period_one_starts_at_local_midnight():
    """Periods run from local midnight, not UTC midnight.

    On a BST day that is 23:00 UTC the previous evening, which is the trap:
    a naive UTC calculation puts period 1 an hour late all summer.
    """
    start = deadlines.period_start(BST_DAY, 1)
    assert start.astimezone(LONDON).hour == 0
    assert start.astimezone(UTC).hour == 23
    assert start.astimezone(UTC).date() == dt.date(2026, 8, 31)


def test_period_one_is_utc_midnight_in_winter():
    start = deadlines.period_start(GMT_DAY, 1)
    assert start == dt.datetime(2026, 1, 15, 0, 0, tzinfo=UTC)


def test_periods_are_always_half_an_hour():
    """Including across a clock change. The local wall clock jumps; the
    period does not get longer or shorter."""
    for date in (SPRING, AUTUMN, BST_DAY):
        total = deadlines.periods_in_day(date)
        for period in range(1, total):
            gap = (deadlines.period_start(date, period + 1)
                   - deadlines.period_start(date, period))
            assert gap == dt.timedelta(minutes=30), f"{date} period {period}"


def test_spring_skips_the_missing_hour():
    """Period 2 ends at 01:00 local, which does not exist, so period 3 starts
    at 02:00 local. In UTC the sequence is unbroken."""
    p2 = deadlines.period_start(SPRING, 2).astimezone(LONDON)
    p3 = deadlines.period_start(SPRING, 3).astimezone(LONDON)
    assert p2.strftime("%H:%M") == "00:30"
    assert p3.strftime("%H:%M") == "02:00"


def test_autumn_repeats_the_extra_hour():
    """Periods 3 and 5 both read 01:00 local. They are an hour apart in UTC,
    which is why settlement counts periods rather than clock times."""
    p3 = deadlines.period_start(AUTUMN, 3)
    p5 = deadlines.period_start(AUTUMN, 5)
    assert p3.astimezone(LONDON).strftime("%H:%M") == "01:00"
    assert p5.astimezone(LONDON).strftime("%H:%M") == "01:00"
    assert p5 - p3 == dt.timedelta(hours=1)


def test_last_period_ends_at_next_local_midnight():
    for date in (SPRING, AUTUMN, BST_DAY, GMT_DAY):
        last = deadlines.periods_in_day(date)
        end = deadlines.period_end(date, last)
        assert end.astimezone(LONDON).hour == 0
        assert end.astimezone(LONDON).date() == date + dt.timedelta(days=1)


def test_period_out_of_range_is_rejected():
    """Period 47 exists on most days and not on 29 March. Asking for it should
    fail rather than silently returning a time in the next day."""
    with pytest.raises(deadlines.DeadlineError, match="does not exist"):
        deadlines.period_start(SPRING, 47)

    with pytest.raises(deadlines.DeadlineError, match="does not exist"):
        deadlines.period_start(BST_DAY, 49)

    # But 49 and 50 are real on the long day.
    assert deadlines.period_start(AUTUMN, 50)


def test_period_zero_is_rejected():
    """Periods are numbered from 1. Zero is an off-by-one, not a period."""
    with pytest.raises(deadlines.DeadlineError):
        deadlines.period_start(BST_DAY, 0)


# --- gate closure -----------------------------------------------------------

def test_gate_closure_is_an_hour_before_the_period():
    assert (deadlines.period_start(BST_DAY, 37)
            - deadlines.gate_closure(BST_DAY, 37)) == dt.timedelta(hours=1)


def test_gate_closure_for_early_periods_falls_the_previous_day():
    """Period 1 closes at 23:00 the night before, in local terms. A sweep that
    only looks at today would miss it."""
    closure = deadlines.gate_closure(GMT_DAY, 1)
    assert closure.astimezone(LONDON).date() == GMT_DAY - dt.timedelta(days=1)


def test_time_to_gate_closure_goes_negative_after_it_passes():
    closure = deadlines.gate_closure(BST_DAY, 37)
    before = closure - dt.timedelta(minutes=5)
    after = closure + dt.timedelta(minutes=5)

    assert deadlines.time_to_gate_closure(BST_DAY, 37, before) == dt.timedelta(minutes=5)
    assert deadlines.time_to_gate_closure(BST_DAY, 37, after) == dt.timedelta(minutes=-5)
    assert not deadlines.is_closed(BST_DAY, 37, before)
    assert deadlines.is_closed(BST_DAY, 37, after)


def test_gate_closure_at_the_exact_moment_is_closed():
    """A submission arriving exactly on the deadline is late. Treating the
    boundary as open would be optimistic in the one direction that costs
    money."""
    closure = deadlines.gate_closure(BST_DAY, 37)
    assert deadlines.is_closed(BST_DAY, 37, closure)


# --- urgency ----------------------------------------------------------------

def test_urgency_levels():
    closure = deadlines.gate_closure(BST_DAY, 37)

    def at(minutes_before):
        return deadlines.urgency(
            BST_DAY, 37, closure - dt.timedelta(minutes=minutes_before)
        )

    assert at(120).level == "OK"
    assert at(30).level == "WARNING"
    assert at(10).level == "CRITICAL"
    assert at(-5).level == "MISSED"


def test_critical_threshold_leaves_time_for_the_manual_fallback():
    """Fifteen minutes is chosen because that is roughly how long a manual
    submission through the central system web interface takes. Escalating
    later would mean escalating too late to act on."""
    assert deadlines.Urgency.ESCALATE_AT == dt.timedelta(minutes=15)

    closure = deadlines.gate_closure(BST_DAY, 37)
    assert deadlines.urgency(
        BST_DAY, 37, closure - dt.timedelta(minutes=15)
    ).critical


def test_urgency_reads_sensibly():
    closure = deadlines.gate_closure(BST_DAY, 37)
    u = deadlines.urgency(BST_DAY, 37, closure - dt.timedelta(minutes=90))
    assert "1h 30m to gate closure" in str(u)

    missed = deadlines.urgency(BST_DAY, 37, closure + dt.timedelta(minutes=20))
    assert "gate closed 20 min ago" in str(missed)


# --- open periods -----------------------------------------------------------

def test_open_periods_excludes_closed_ones():
    now = dt.datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    periods = deadlines.open_periods(now, horizon=dt.timedelta(hours=6))

    assert periods
    for date, period in periods:
        assert not deadlines.is_closed(date, period, now)
        assert deadlines.time_to_gate_closure(date, period, now) <= dt.timedelta(hours=6)


def test_open_periods_spans_midnight():
    """Late in the evening the open periods are mostly tomorrow's. A sweep
    that only considered today would go quiet exactly when the next day's
    submissions are being prepared."""
    now = dt.datetime(2026, 9, 1, 22, 0, tzinfo=UTC)
    periods = deadlines.open_periods(now, horizon=dt.timedelta(hours=6))
    assert any(date > dt.date(2026, 9, 1) for date, _ in periods)


def test_open_periods_handles_the_long_day():
    """Periods 49 and 50 exist on 25 October and must appear."""
    now = dt.datetime(2026, 10, 24, 21, 0, tzinfo=UTC)
    periods = deadlines.open_periods(now, horizon=dt.timedelta(hours=30))
    assert (AUTUMN, 49) in periods
    assert (AUTUMN, 50) in periods
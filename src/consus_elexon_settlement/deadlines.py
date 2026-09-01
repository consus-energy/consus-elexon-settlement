"""Settlement period timing and Gate Closure.

Everything in this system is measured against Gate Closure: one hour before
the settlement period it applies to. Miss it and the position is unhedged and
cashed out at the imbalance price, with no correction possible afterwards.

Three things make this harder than adding an hour:

1.  Settlement periods are half-hours, numbered from 1, in LOCAL time. Period 1
    starts at 00:00 local, not 00:00 UTC. On a British Summer Time day the
    period boundaries move relative to UTC.

2.  Clock-change days have 46 or 50 periods, not 48. In March the clocks go
    forward and two periods do not exist; in October they go back and two
    happen twice. Code that assumes 48 is wrong twice a year, on days that
    often carry unusual prices.

3.  The deadline that matters is not a fixed age. A file sent twenty minutes
    ago for a period closing in ten minutes is urgent; the same file for
    tomorrow is not. Urgency is a function of the period, not of the file.

The dependency on zoneinfo is deliberate: Europe/London knows when the clocks
change and we should not. Hardcoding the last Sunday in March is the kind of
thing that works until the rule changes.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")
UTC = dt.timezone.utc

# BSC Section X-2: Gate Closure is one hour before the Settlement Period.
GATE_CLOSURE_LEAD = dt.timedelta(hours=1)

PERIOD_LENGTH = dt.timedelta(minutes=30)

# The periods a normal, short and long day carry. Named rather than inlined so
# a comparison against 48 is visibly a bug rather than plausible.
NORMAL_DAY = 48
SHORT_DAY = 46
LONG_DAY = 50


class DeadlineError(ValueError):
    pass

def periods_in_day(settlement_date: dt.date) -> int:
    """How many settlement periods this date has.

    Derived from the clock rather than from a rule about the last Sunday in
    March: zoneinfo knows when the clocks change and we should not.

    Both midnights are converted to UTC before subtracting. Python subtracts
    two aware datetimes sharing a tzinfo as WALL CLOCK time, so the naive form
    returns 24 hours on every day including the ones that are 23 or 25. That
    is the whole bug this function exists to avoid, so it would have been a
    poor place to reproduce it.
    """
    start = _local_midnight(settlement_date).astimezone(UTC)
    end = _local_midnight(settlement_date + dt.timedelta(days=1)).astimezone(UTC)
    half_hours = (end - start) / PERIOD_LENGTH
    if half_hours not in (SHORT_DAY, NORMAL_DAY, LONG_DAY):
        raise DeadlineError(
            f"{settlement_date} computes to {half_hours} periods, which is not "
            f"46, 48 or 50. Check the timezone database."
        )
    return int(half_hours)


def period_start(settlement_date: dt.date, settlement_period: int) -> dt.datetime:
    """When a settlement period begins, in UTC.

    Periods are numbered from 1 and run from local midnight. Arithmetic is
    done in UTC after resolving local midnight, so a period that spans a clock
    change is still half an hour long.
    """
    total = periods_in_day(settlement_date)
    if not 1 <= settlement_period <= total:
        raise DeadlineError(
            f"period {settlement_period} does not exist on {settlement_date}, "
            f"which has {total}"
        )
    start = _local_midnight(settlement_date).astimezone(UTC)
    return start + (settlement_period - 1) * PERIOD_LENGTH


def period_end(settlement_date: dt.date, settlement_period: int) -> dt.datetime:
    return period_start(settlement_date, settlement_period) + PERIOD_LENGTH


def gate_closure(settlement_date: dt.date, settlement_period: int) -> dt.datetime:
    """The moment after which nothing can be submitted for this period."""
    return period_start(settlement_date, settlement_period) - GATE_CLOSURE_LEAD


def time_to_gate_closure(
    settlement_date: dt.date, settlement_period: int, now: dt.datetime | None = None
) -> dt.timedelta:
    """How long is left. Negative once the gate has closed."""
    return gate_closure(settlement_date, settlement_period) - (now or _now())


def is_closed(
    settlement_date: dt.date, settlement_period: int, now: dt.datetime | None = None
) -> bool:
    return time_to_gate_closure(settlement_date, settlement_period, now) <= dt.timedelta()


@dataclass(frozen=True)
class Urgency:
    """How pressing an outstanding submission is.

    Separated from the sweep so the judgement is testable without a database,
    and so the thresholds live in one place rather than being scattered
    through alerting rules.
    """

    settlement_date: dt.date
    settlement_period: int
    remaining: dt.timedelta

    # Escalate to a human at fifteen minutes: enough time to submit manually
    # through the central system web interface, which is the documented
    # fallback and takes a few minutes to perform.
    ESCALATE_AT = dt.timedelta(minutes=15)
    WARN_AT = dt.timedelta(minutes=45)

    @property
    def closed(self) -> bool:
        return self.remaining <= dt.timedelta()

    @property
    def critical(self) -> bool:
        """Past the point where a human could still fix it by hand."""
        return not self.closed and self.remaining <= self.ESCALATE_AT

    @property
    def warning(self) -> bool:
        return not self.closed and self.ESCALATE_AT < self.remaining <= self.WARN_AT

    @property
    def level(self) -> str:
        if self.closed:
            return "MISSED"
        if self.critical:
            return "CRITICAL"
        if self.warning:
            return "WARNING"
        return "OK"

    def __str__(self) -> str:
        if self.closed:
            return (
                f"{self.settlement_date} period {self.settlement_period}: "
                f"gate closed {_describe(-self.remaining)} ago"
            )
        return (
            f"{self.settlement_date} period {self.settlement_period}: "
            f"{_describe(self.remaining)} to gate closure"
        )


def urgency(
    settlement_date: dt.date, settlement_period: int, now: dt.datetime | None = None
) -> Urgency:
    return Urgency(
        settlement_date=settlement_date,
        settlement_period=settlement_period,
        remaining=time_to_gate_closure(settlement_date, settlement_period, now),
    )


def open_periods(
    now: dt.datetime | None = None, horizon: dt.timedelta = dt.timedelta(hours=24)
) -> list[tuple[dt.date, int]]:
    """Every settlement period whose gate has not yet closed, within `horizon`.

    Used by the sweep to decide what it is worth chasing: an outstanding
    submission for a period that has already closed cannot be fixed, and one
    beyond the horizon is not yet pressing.
    """
    now = now or _now()
    result: list[tuple[dt.date, int]] = []
    today = now.astimezone(LONDON).date()

    for offset in (0, 1, 2):
        day = today + dt.timedelta(days=offset)
        for period in range(1, periods_in_day(day) + 1):
            closure = gate_closure(day, period)
            if now < closure <= now + horizon:
                result.append((day, period))
    return result


def _local_midnight(day: dt.date) -> dt.datetime:
    """Midnight local time, as an aware datetime.

    fold=0 resolves the ambiguity on the October clock-change day, where
    01:00 local happens twice. Midnight itself is never ambiguous, but being
    explicit costs nothing and documents that the case was considered.
    """
    return dt.datetime(day.year, day.month, day.day, tzinfo=LONDON, fold=0)


def _describe(delta: dt.timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60}h {minutes % 60:02d}m"


def _now() -> dt.datetime:
    return dt.datetime.now(UTC)
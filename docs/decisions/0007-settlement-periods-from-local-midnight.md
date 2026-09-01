# ADR-0007: Settlement periods run from local midnight

## Status

Accepted, August 2026.

## Context

A settlement period is a half hour. Periods are numbered from 1 and run from
midnight LOCAL time, not UTC. Gate Closure is one hour before the period it
applies to.

Two consequences follow, and both are easy to get wrong.

Through British Summer Time, period 1 begins at 23:00 UTC the previous
evening. A calculation in UTC puts every period an hour late for seven months
of the year.

Clock-change days do not have 48 periods. In March the clocks go forward and
two periods do not exist, giving 46. In October they go back and two happen
twice, giving 50. Code that assumes 48 is wrong twice a year, on days that
often carry unusual prices.

There is a third trap that is not obvious. Python subtracts two aware
datetimes sharing a tzinfo as WALL CLOCK time. So this returns 24 hours on
every day of the year, including the ones that are 23 or 25 hours long:

    end = datetime(2026, 3, 30, tzinfo=LONDON)
    start = datetime(2026, 3, 29, tzinfo=LONDON)
    end - start        # 1 day, not 23 hours

The first implementation of `periods_in_day` contained exactly that, and
returned 48 for every date.

## Decision

`deadlines.py` resolves local midnight in Europe/London, converts to UTC, and
does all arithmetic there.

The number of periods in a day is derived from the clock rather than from a
rule: the difference between consecutive local midnights, in half hours. The
timezone database knows when the clocks change; we do not need to.

`period_start` rejects a period that does not exist on the given date, rather
than returning a time in the following day.

## Consequences

Adding half an hour is always adding half an hour, even across a clock change.
The wall clock jumps; the period does not change length.

Asking for period 47 on 29 March raises rather than quietly returning
midnight. That is the difference between a caught bug and a misfiled volume.

The rule keeps working if the clock-change dates move, because nothing about
the last Sunday in March appears in the code.

## Alternatives considered

**Hardcode the clock-change dates.** Works until the rule changes, and the
failure would be a silent off-by-one on two days a year.

**Work entirely in local time.** Removes the conversion but reintroduces the
wall-clock subtraction problem everywhere, and makes storage ambiguous on the
October day when 01:00 happens twice.

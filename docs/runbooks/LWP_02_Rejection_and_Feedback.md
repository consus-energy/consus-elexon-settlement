# LWP 02 — Handling rejection and feedback

**When.** Feedback arrives. Collected every five minutes.
**Who.** Automated recording; a person decides what to do about it.
**Deadline.** Whatever remains before Gate Closure. Usually very little.

---

## What arrives

| Flow | From | Means |
|---|---|---|
| E0281 | ECVAA | ECVN accepted |
| E0091 | ECVAA | ECVN rejected, with a reason |
| E0521 | ECVAA | WMAN rejected, per period or per BM Unit |
| P0330 | SVAA | expected volume accepted |
| P0329 | SVAA | expected volume rejected |
| P0331 | SVAA | expected volume accepted but zero for a period |
| P0284 | SVAA | delivered volume confirmed |
| P0283 | SVAA | delivered volume rejected |

The gateway records each against the submission it answers, and moves the
intent state. You are alerted only when something needs a decision.

---

## When a rejection arrives

**First, how long is left.** If Gate Closure has passed, a correction cannot be
submitted. Record it and move on; do not spend the time working out what went
wrong until afterwards.

**If time remains**, read the reason. It is 80 characters of free text and it
is all you get.

| Reason indicates | Do |
|---|---|
| Credit cover insufficient | Reduce the volume or do not resubmit. Check cover before the next period. |
| Authorisation not in effect | The ECVNAA is wrong or expired. Cannot be fixed before Gate Closure. |
| BM Unit not registered | Registration issue, not a submission issue. See LWP 05. |
| Volume or format | Correct and resubmit as a new revision. |
| Not understood | Treat as unrecoverable for this period and use LWP 03 if time allows. |

**Correcting.** A rejected submission is not retried. The content was refused,
so the same content will be refused again. The trading platform publishes a new
revision, which takes a new reference code and a new sequence number.

---

## Partial rejection

A WMAN or an expected volume can be rejected for some settlement periods and
accepted for others. The gateway records this per period. Do not treat a
partial rejection as total: the accepted periods stand and resubmitting them
would duplicate.

---

## A warning rather than a rejection

P0331 means a period was accepted with an expected volume of zero. The
submission stands. It usually means the forecast produced nothing rather than
genuinely expecting nothing. Investigate before the deviation is measured
against it; do not resubmit automatically.

---

## Exceptions

**Feedback for something we have no record of sending.** The gateway raises
this rather than guessing. Check the archive by filename. If it is genuinely
not ours, tell the Service Desk; if it is ours and the record is missing, that
is a defect and the record needs correcting before submission continues.

**A rejection matching more than one submission.** The gateway refuses rather
than picking one, because applying it to the wrong one marks a live position as
failed and leaves the failed one looking healthy. Resolve by hand from the
archive.

**No feedback at all.** The sweep alerts after the grace period. Silence is
handled the same as rejection: if the deadline is close, use LWP 03.

---

| | |
|---|---|
| Version | 1.0 |
| Owner | CTO |
| Reviewed by | CEO |

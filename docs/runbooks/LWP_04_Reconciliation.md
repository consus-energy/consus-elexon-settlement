# LWP 04 — Reconciliation

**When.** Daily, and at each settlement run.
**Who.** CTO.
**Why.** Because nothing else would notice a submission that was accepted but
settled differently from what we intended.

---

## What is compared

Four views of the same period, which should agree:

| View | Source |
|---|---|
| What we traded | the trading platform |
| What we notified | the gateway's record of what it sent |
| What was accepted | feedback received from central systems |
| What was settled | settlement reports |

---

## Daily

Check that every intent for yesterday reached ACTED, meaning all three flows
were accepted rather than merely sent.

```
gcloud run jobs execute settlement-test-migrate --region europe-west2 --args="dr" --wait
```

Anything still open is either awaiting feedback, which is normal for a few
hours, or stuck, which is not. Anything MISSED is a period we did not submit
for; it should already have raised an alert at the time, and if it did not,
that is a finding about the alerting rather than about the period.

---

## On settlement reports

Compare the delivered volumes we notified against the volumes settled. They
will not always match exactly and that is expected: SVAA caps the delivered
volume by the metered volume, so a claim larger than the meter shows is
reduced rather than rejected.

| Difference | Means |
|---|---|
| Settled equals notified | Nothing to do. |
| Settled less than notified | Our delivered volume exceeded what the meter supports. Check the dispatch record against the meter for that period. |
| Settled zero | No expected volume was in effect, or the WMAN was not accepted. Check both. |
| Exception report received | SVAA could not process the pair at all. See the reason and LWP 05. |

---

## What to do with a difference

Record it. A single period differing by a small margin is not worth
investigating; the same difference recurring is. The question to answer is
whether the cause is measurement, our expected volume, or our dispatch, and
those have different remedies.

Where the expected volume is consistently wrong, that is a forecasting problem
and it matters beyond settlement: our methodology states that the counterfactual
is site load with the battery idle, and its accuracy is the evidence supporting
that position.

---

## Exceptions

**Settled volume with no corresponding notification.** Should not occur. Stop
and check the sequence position against the archive before submitting again.

**Notification with no settled volume.** Usually the WMAN was rejected, so SVAA
never learned we were active. Check the feedback for that period.

**Persistent difference across many periods.** Treat as a defect rather than a
settlement query. Record it, and do not adjust submissions to compensate.

---

| | |
|---|---|
| Version | 1.0 |
| Owner | CTO |
| Reviewed by | CEO |
| Note | Settlement reports are received once operating. Until then this procedure covers the first three views only. |

# LWP 01 — Submitting notifications

**When.** Every settlement period in which we take a position.
**Who.** Automated. A person is involved only when something fails.
**Deadline.** Gate Closure, one hour before the settlement period.

---

## What happens without anyone doing anything

The trading platform publishes a decision. The gateway records it, checks the
deadline has not passed, and builds three notifications:

| Flow | To | What it says |
|---|---|---|
| WMAN | ECVAA | this BM Unit is wholesale-active in this period |
| ECVN | ECVAA | the contracted volume |
| SEV | SVAA | what the site would have done without us |

Each is signed, encrypted and sent. The gateway records what it sent and waits
for acceptance. Acceptance usually arrives within 15 minutes; the code allows
20 before declaring a system failure.

An intent is not complete until all three are **accepted**. Sent is not
accepted, and the distinction matters: a rejection can still arrive after a
successful send.

---

## When to intervene

You will know because an alert arrives. There are three:

| Alert | Means | Do |
|---|---|---|
| Submission at gate closure | 15 minutes left, something unacknowledged | Below |
| Job failed | collect or sweep exited non-zero | Read the logs, decide if it affects a deadline |
| Sending unencrypted | keys not configured | Stop. Nothing should be sent. |

**If the alert says gate closure, do not attempt to diagnose.** Go to LWP 03,
manual fallback. Measured recovery times are 15 minutes for the gateway and 28
for the database; both exceed the time you have.

---

## Checking what happened

```
gcloud run jobs execute settlement-test-migrate --region europe-west2 --args="dr" --wait
gcloud logging read 'resource.labels.job_name="settlement-test-sweep"' \
  --limit 30 --format="value(textPayload)" --freshness=1h
```

`dr` prints the sequence position per channel and the most recent files. The
sweep log lists anything outstanding with its urgency.

---

## Exceptions

**A flow failed to send.** The file is built and archived; only transport
failed. Retry sends the same bytes under the same sequence number. Never
rebuild: a rebuild takes a second sequence number, leaves a permanent gap at
the first, and the reference code collides with the record already written.

**A flow was rejected.** See LWP 02.

**The deadline passed with something outstanding.** Record it in the exception
log with the settlement date, period, what was outstanding and why. The
position is unhedged and will be cashed out at the imbalance price. Nothing can
be submitted for that period now.

**Two submissions for the same period.** Should be impossible: the intent is
identified by settlement date, period, BM Unit and revision, and a repeat sends
nothing. If it happens, stop automated submission and check the sequence
position against the archive before resuming.

---

| | |
|---|---|
| Version | 1.0 |
| Owner | CTO |
| Reviewed by | CEO |

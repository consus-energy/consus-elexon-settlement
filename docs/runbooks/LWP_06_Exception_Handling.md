# LWP 06 — Exception handling

**When.** Anything unexpected. This is the procedure that covers the gaps in
the others.
**Who.** CTO records and resolves; CEO reviews at the monthly management
review.

---

## What counts

Anything that did not go as the procedure describes. A rejected notification is
not an exception, because LWP 02 covers it. A rejection we cannot explain is.

Three questions decide what happens next.

**Does it affect a settlement obligation?** If a deadline is at risk, act
first and record second. Everything else can wait ten minutes.

**Is it recurring?** A single occurrence is an exception. The same exception
three times is a defect, and the remedy is different: fix the system, not the
instance.

**Did a control fail?** If something should have prevented it or should have
detected it, that control is reviewed whether or not the exception itself
caused harm.

---

## Recording

In the exception log, at the time rather than afterwards. The log covers exceptions arising in operation; defects found during testing are recorded in the testing register instead, so that the two do not hold the same item twice.

| Field | |
|---|---|
| Date and time | |
| What happened | Plainly. Not the diagnosis. |
| Settlement impact | Which periods, or none |
| Action taken | Including anything done outside the normal route |
| Control that should have prevented or detected it | |
| Outcome | |

---

## Severity

**High** — prevented us meeting a BSC obligation, caused incorrect settlement
data to be submitted, or left a failure undetected. Resolved before the next
settlement day, or escalated to the CEO with what we are doing about it.

**Medium** — caused incorrect behaviour that was detected and corrected in
time. Resolved within the week.

**Low** — no effect on settlement accuracy or obligation compliance. Reviewed
monthly.

---

## Emergency change

An exception may require a change that cannot wait for peer review. The
Information Security and Operations Policy section 1.5 permits this: the CTO
may approve and deploy, the automated tests still run, and the change is
recorded here at the time and reviewed retrospectively.

Where the failure affects our ability to meet Gate Closure, use LWP 03 rather
than attempting a fix. Recovery takes longer than the time available.

---

## Review

Every exception is reviewed at the monthly management review, covering whether
the response was right, whether the control that failed has been corrected, and
whether the same exception is recurring.

An exception that is closed without a cause identified is recorded as such
rather than presented as resolved.

---

## Notifying others

| Who | When |
|---|---|
| Elexon Service Desk | We could not meet an obligation, or we need something from central systems to resolve it |
| Counterparty | A position they are party to is affected |
| Customer | Dispatch at their site was affected |

---

| | |
|---|---|
| Version | 1.0 |
| Owner | CTO |
| Reviewed by | CEO |

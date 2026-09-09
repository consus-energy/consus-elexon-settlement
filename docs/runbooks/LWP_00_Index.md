# Local Working Procedures

The procedures a person follows to operate as a Virtual Trading Party.

Consus has a single technical operator. These are written for that reality:
short, specific, and covering what to do when something fails rather than
describing what the system does when it works. Where the system does the work,
the procedure says so and moves on to the part a person is needed for.

| Ref | Procedure | When |
|---|---|---|
| LWP 01 | Submitting notifications | Every settlement period we trade |
| LWP 02 | Handling rejection and feedback | Feedback arrives |
| LWP 03 | Manual submission fallback | No Consus system available |
| LWP 04 | Reconciliation | Daily, and at settlement |
| LWP 05 | Registration | Adding a site or changing an authorisation |
| LWP 06 | Exception handling | Anything the others do not cover |

## Conventions

**Deadlines are stated first** in each procedure, because everything else
depends on how much time is left.

**Each procedure ends with exceptions** — what to do when that procedure does
not go as described. Anything not covered there goes to LWP 06.

**Where recovery competes with a deadline, the deadline wins.** Measured
recovery times are 15 minutes for the gateway and 28 for the database. Gate
Closure is one hour before the settlement period, and a failure detected close
to it leaves less than either. LWP 03 is the answer, not diagnosis.

## Holding and review

LWP 01, 02, 04, 05 and 06 are held in version control alongside the systems
they describe, so procedure and system change together.

LWP 03 is different. It is used when no Consus system is available, so it is
also held in the password manager reachable from a phone, with a printed copy
at each director's home. A procedure reachable only through the systems it
replaces is of no use.

Each is reviewed annually, after any material change to the system it
describes, and after any exception that shows it to be wrong. Each is tested by
someone other than its author; the outcome is recorded in the testing register.

# LWP walkthrough record

Confirming the Local Working Procedures can be followed by someone other than
their author. One question per procedure, chosen where getting it wrong costs
something.


Date: 03/09/2026   Read by: William Moore   Recorded by: Ethan McNeil

---

**1. LWP 01.** A notification failed to send. Do you rebuild the file or resend
the one already built?


Resend. The file already exists and was archived; only sending it failed. If we rebuild it we get a new sequence number and leave a gap at the old one, and gaps can't be fixed afterwards.
---

**2. LWP 02.** A notification was rejected. Do you retry it?


No. They rejected the content, so sending the same thing again gets rejected again. It needs to go back to the trading side as a new revision with a new reference.
---

**3. LWP 04.** The settled volume is less than what we notified. What does that
mean?


We claimed more than the meter supports. SVAA caps it at the metered volume rather than throwing it out. So check what the battery actually did against the meter for that period.
---

**4. LWP 05.** How long does an MSID Pair allocation take, and what has to
happen first?


Five working days. And we need the customer's written consent first, because the MPAN is theirs.
---

**5. LWP 06.** Something unexpected happens and a deadline is at risk. Record
it first or act first?

Act. Deal with the deadline, then write it up. The record can wait ten minutes; the deadline can't.
Answer: _______________________________________________  ☐ right ☐ not

---

**Findings.** Anything answered wrongly or uncertainly, and what in the
procedure caused it.

_________________________________________________________________

_________________________________________________________________

**Outcome.**   Procedures can be followed as written
              

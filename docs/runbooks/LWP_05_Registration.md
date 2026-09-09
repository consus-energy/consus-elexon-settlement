# LWP 05 — Registration

**When.** Adding a site, changing a BM Unit, or changing an authorisation.
**Who.** CTO.
**Lead time.** Five working days for an MSID Pair allocation. Plan around it.

---

## Adding a site

Order matters. Each step depends on the one before.

**1. Customer consent.** The MSID belongs to the customer, not to us. Consent to
allocate it to our Secondary BM Unit is obtained in writing before anything is
submitted.

**2. Check the metering.** The MSID must be registered, half-hourly capable,
not disconnected, and in the GSP Group we will name. A pair must have an Import
MSID; an Export MSID is optional.

**3. Allocate the MSID Pair** to the Trading Secondary BM Unit through the
Self-Service Gateway, under BSCP602. Five working days.

**4. Confirm the allocation** before trading the site. An allocation submitted
is not an allocation in effect.

**5. Register the pair as Non-Baselined**, which is the default for a VTP
Trading Secondary BM Unit. We submit expected volumes rather than using the
baselining solution.

**6. Record it** — the site, MSID Pair, GSP Group, BM Unit and effective date.

---

## Secondary BM Unit format

`V__?CNRG???`

`V_` marks a Secondary BM Unit. `_?` is the GSP Group letter, which is
determined by where the site connects and is not a choice. `CNRG` is our MPID.
The last three digits are ours to assign.

The GSP Group letter must match the MSIDs in the pair. The first two digits of
an MPAN identify the distributor, which maps to the GSP Group.

---

## Changing an authorisation

The ECVN Agent Authorisation is established manually under BSCP71. The key
arrives once, in E0071, and is required on every subsequent ECVN. Without it no
notification can be submitted at all.

When an E0071 is received, confirm the key was captured. The gateway writes it
to the secret store and refuses rather than discarding it if the store is not
configured.

---

## Exceptions

**Allocation rejected.** Read the reason. Usually the MSID is not half-hourly,
is not in the GSP Group claimed, or is already allocated to another party. None
of these is fixable by resubmitting the same request.

**Allocation not confirmed within five working days.** Chase before trading the
site. Trading an unallocated pair produces volumes that settle nowhere.

**A site leaves.** Deallocate before the customer's arrangements change, and
stop trading it first. A pair that is traded after deallocation produces a
delivered volume for a pair we no longer hold.

---

| | |
|---|---|
| Version | 1.0 |
| Owner | CTO |
| Reviewed by | CEO |

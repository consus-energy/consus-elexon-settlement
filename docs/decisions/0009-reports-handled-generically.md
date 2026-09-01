# ADR-0009: Reports are recorded, not modelled

## Status

Accepted, August 2026.

## Context

Our CVA scope covers 23 flows, but that count understates the modelling work.
CRA-I014 is 15 sub-flows and SAA-I014 is 13, so the real total is closer to
forty distinct file structures.

They divide cleanly. Some change what we believe about a submission: E0281
acceptance, E0091 rejection, E0521 exception, the SVAA feedback flows, and
E0071, which carries the ECVNAA Key.

The rest are reports. E0131 authorisation, E0141 notification, E0221 forward
contract, P0285 exception, P0288 consumption, P0333 baselining, and the CRA
and SAA reports. They confirm what central systems believe. None changes a
submission's state.

Building domain types for all of them is weeks of work for data nothing reads.

## Decision

Two classes of handler.

Flows that change state get a bespoke parser and a domain type: `inbound/
ecvaa.py` and `inbound/svaa.py`.

Reports get `ReportHandler`, which records that the file arrived, parsed
against its spec, was acknowledged, and how many records it contained. It does
not interpret the content.

The bytes are archived either way, so a report can be parsed later from the
archive without having been modelled in advance.

## Consequences

Qualification is satisfied: the file was received, parsed, acknowledged and
retained, which is what BSC Section U1.6 requires.

Reports are registered rather than left unhandled. That distinction matters --
an unhandled file means Elexon sent something we were not expecting, which is
worth investigating. A registered report means we expected it and chose not to
act.

When a report is actually needed -- reconciling P0333 against our own expected
volumes, say -- it gets a parser then, reading historic files from the
archive.

Storing a record count rather than a parsed tree means no stored data to
migrate when a spec version changes.

## Alternatives considered

**Model everything up front.** Weeks of work producing types nothing consumes,
and each would need revisiting when its flow version changed.

**Ignore reports entirely.** Leaves them unhandled, which is indistinguishable
from an unexpected file, and loses the record that they arrived.

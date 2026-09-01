# ADR-0001: The flow spec is generated from the IDD spreadsheet

## Status

Accepted, August 2026.

## Context

Every file we exchange with Elexon is defined in the NETA Interface Definition
and Design Document. Part 1 comes in two forms: a PDF narrative and a
spreadsheet. The spreadsheet is authoritative for physical structure -- record
types, field order, data types, cardinality, valid sets.

We are in CVA scope for 23 flows across four central systems, and the four
systems between them define over a hundred. Each has several nested record
types with tens of fields. The SVAA tab alone yields 44 flows.

Transcribing that by hand is thousands of field definitions. A single wrong
length or a field in the wrong position produces a file Elexon rejects, and
the rejection reason is 80 characters of free text arriving hours later.

## Decision

`tools/gen_spec.py` reads the IDD spreadsheet and emits Python modules --
`spec.py`, `spec_svaa.py`, `spec_cra.py`, `spec_saa.py` -- containing the flow
definitions as data. Nothing in the spec modules is hand-written.

The builder and parser in `idd/file.py` are generic: they walk the spec rather
than knowing about any particular flow. Adding a flow is a spec entry, not a
module.

## Consequences

A new IDD version is a regeneration, not a transcription exercise. The diff
between generated files shows exactly what Elexon changed, which is more
useful than a changelog.

The generated modules are large and are not read by humans. That is fine, but
it means a bug in the generator is invisible on inspection and only surfaces
in a round-trip test. Round-trip tests are therefore not optional.

The SVAA tab omits N-numbers for every field, so the generator synthesises
stable item ids from the item names. Those ids appear throughout the SVAA flow
modules and are not the ones in the IDD. Anyone comparing code against the
document needs to know that.

## Alternatives considered

**Hand-written spec modules.** Rejected on volume and on the failure mode: a
transcription error is silent until Elexon rejects a file.

**Parsing the spreadsheet at runtime.** Rejected because it makes the
spreadsheet a deployment dependency and moves a whole class of failure from
build time to Gate Closure.

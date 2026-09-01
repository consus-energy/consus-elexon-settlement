# ADR-0002: A file is built once; retries send the archived bytes

## Status

Accepted, August 2026.

## Context

Sequence numbers must be contiguous per channel (IDD 2.2.8). ECVAA uses gaps
to detect files lost in transit, and stops processing at the gap. A gap cannot
be corrected retrospectively: the remedy is a manual agreement with Elexon.

Transport fails for ordinary reasons -- a network blip, an FTP timeout -- and
the obvious response is to rebuild the file and send it again.

That is exactly wrong. Rebuilding allocates a second sequence number, leaving
the first unused, which is the gap ECVAA stops at.

## Decision

Building and sending are separate operations with different multiplicities.

    reserve -> build -> archive     happens once
    send                            may happen many times

`Sender.send` reserves a number, builds the bytes, writes them immutably to
the archive, then delivers. `Sender.retry` reads the bytes back out of the
archive and delivers those. It does not rebuild.

The archive is authoritative. What it holds is what went on the wire.

## Consequences

A retry is guaranteed byte-identical, including the checksum. Even if an input
changed between attempts, the file does not.

Recovery after a crash mid-send is straightforward: the file is archived and
its state is SEND_FAILED, so `retry` picks it up.

The archive must be written before sending, which adds a round trip to the
critical path before Gate Closure. Acceptable: the alternative failure is
unrecoverable.

An archive write that fails means the file cannot be sent at all. That is the
correct behaviour -- sending something we have no record of would be worse.

## Alternatives considered

**Rebuild on retry.** The natural implementation, and the one that produces
permanent sequence gaps.

**Hold the bytes in memory.** Survives a transport failure but not a process
restart, which is when a retry is most likely to be needed.

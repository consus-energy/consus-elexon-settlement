# ADR-0011: gpg instead of XSec

## Status

Accepted, September 2026. Supersedes the XSec integration in ADR-0002's
transport layer.

## Context

BSCP70 Appendix 1 requires XSec encryption software, supplied by BSC CSA with
the communications order. Every file exchanged with central systems is signed
and encrypted through it.

XSec is Windows-only. Its user guide states it is "explicitly not supported on
any .NET based environment on any non-Microsoft Windows Operating System". It
is a Windows Service that watches directories rather than a library, so
integration means writing a file, polling an output folder, and checking an
error folder.

Our gateway is Python on Linux containers. Using XSec would have meant:

- a Windows VM in the send path, patched by us rather than managed
- shared storage between the Linux jobs and that node
- a single point of failure, or XSec's hot-standby cluster, which is two VMs
- a component with no test coverage, because XSec cannot run in CI
- amendments to the architecture diagram, system inventory, business
  continuity plan, and two QRA answers

We built that path: a Windows VM under Terraform, an XSecCipher driving the
watched directories, and a key exchange completed through XSecManager.

Elexon's communications team then confirmed: "it is not a requirement to use
XSec, only to be compatible with XSec. We only support the XSec software but
we can provide some notes on using gpg under linux."

They supplied the exact gpg invocations.

## Decision

gpg, with the parameters Elexon specified.

    encrypt:  sign with our private key, SHA1 digest, zlib compression,
              armoured; then encrypt to their public key, CAST5 cipher, ZIP
              compression, force-mdc, armoured
    decrypt:  decrypt with our private key, then verify their signature

Interoperability was confirmed in both directions with Central Services before
this decision was taken. They decrypted a file we produced; we decrypted a
file they produced, with a good signature.

The Windows VM, XSecCipher and its tests are removed.

## Consequences

Encryption is a synchronous call inside the container we already build, test
and deploy. No Windows node, no shared storage, no extra failure point in the
send path.

It is testable. `test_gpg.py` generates a throwaway key pair and round-trips a
file, so the encryption path is covered in CI. XSec never could be.

The algorithm parameters are not modern and are not ours. SHA1, CAST5 and
1024-bit RSA are deprecated, and gpg refuses them without
`--allow-old-cipher-algos`. Central Services' key was generated in 2008 and
cannot be changed without every participant re-keying, so the constraint is
theirs and permanent.

That makes the gpg version worth pinning in the image. Each release narrows
what is permitted, and a base image that silently upgrades gpg is one that
will eventually be unable to talk to Elexon -- surfacing at Gate Closure.

We are outside Elexon's support boundary. They support XSec; they provided
notes on gpg. If something breaks at three in the morning, the answer will be
ours to find. That is the trade for removing a Windows machine from a
settlement path.

The Cipher protocol still takes a filename, which gpg does not need. It is
kept because a protocol that changes shape per implementation is not a
protocol, and a future transport may need it again.

## Alternatives considered

**Run XSec on a Windows VM.** Built, then removed. Defensible -- it is the
supported path -- but it adds a machine we patch, a storage bridge, an
untestable component, and a failure point, all in the path of every file
before Gate Closure.

**Ask Elexon to modernise the algorithms.** Not available: their key is shared
across the market, and changing it would require every participant to
re-exchange.
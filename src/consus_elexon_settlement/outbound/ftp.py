"""FTP transport to and from the central systems.

IDD 2.3: outbound is push-only. Participant systems push files to the central
systems and take the FTP success code as confirmation of sending -- not of
receipt, which arrives later as an ADT. Inbound offers push or pull; under
pull, deleting the file from the source directory is how we confirm we have
it.

Two things about this module are deliberately conservative.

UPLOAD IS ATOMIC. A file is uploaded under a temporary name and renamed into
place. Without that, a poller on the far end can collect a half-written file,
reject it as malformed, and consume our sequence number for a file we sent
correctly. The rename is the only operation the FTP protocol offers that is
atomic from the reader's point of view.

DELETION FOLLOWS THE READ, NOT THE OTHER WAY ROUND. A file is only removed
from the inbound directory once its bytes are held. Deleting first would lose
a rejection we never saw, and rejections are the messages that matter.

FTPS or plain FTP is not yet confirmed -- it is a question outstanding with
Elexon. `tls` therefore defaults to True: defaulting to the insecure option
would be the wrong way round, and a connection that fails loudly is better
than one that silently sends settlement data in the clear.
"""

from __future__ import annotations

import ftplib
import io
import logging
from dataclasses import dataclass, field

from .transport import TransportError

log = logging.getLogger(__name__)

# Uploaded files carry this suffix until complete, then are renamed into
# place. Chosen to be obviously not a settlement filename: IDD 2.2.5 names are
# 14 characters, so anything longer cannot be mistaken for a real one.
PARTIAL_SUFFIX = ".partial"


@dataclass
class FtpTransport:
    """Push files to a central system, collect what is waiting.

    Connections are opened per operation rather than held. A long-lived FTP
    connection across a poller that runs every five minutes is a connection
    that will be dead when it matters, and reconnecting costs a fraction of a
    second against a deadline measured in an hour.
    """

    host: str
    username: str
    password: str
    outbound_dir: str
    inbound_dir: str
    port: int = 21
    tls: bool = True
    passive: bool = True
    timeout_seconds: float = 60.0
    # Files collected but not deleted, for the pull method where the far end
    # expects deletion to confirm receipt. Set False if Elexon push to us.
    delete_after_collect: bool = True

    _sent: list[str] = field(default_factory=list, init=False)

    def send(self, filename: str, payload: bytes) -> None:
        """Push one file, atomically.

        Uploaded under a temporary name and renamed into place, so a poller on
        the far end never sees a partial file. Returning normally means the
        server accepted the transfer -- not that anything has read it.
        """
        if len(filename) > 14:
            # IDD 2.2.5. Not enforced by the protocol, but a name the far end
            # rejects is a wasted sequence number, and better caught here.
            raise TransportError(
                f"filename {filename!r} is {len(filename)} characters; "
                f"IDD 2.2.5 allows 14"
            )

        temporary = f"{filename}{PARTIAL_SUFFIX}"

        with self._connect() as ftp:
            ftp.cwd(self.outbound_dir)

            try:
                ftp.storbinary(f"STOR {temporary}", io.BytesIO(payload))
            except ftplib.all_errors as exc:
                raise TransportError(f"uploading {filename}: {exc}") from exc

            try:
                ftp.rename(temporary, filename)
            except ftplib.all_errors as exc:
                # The partial file is left behind deliberately. Removing it
                # would hide evidence of a failure that needs looking at, and
                # its suffix means nothing will collect it.
                raise TransportError(
                    f"renaming {temporary} to {filename}: {exc}. The partial "
                    f"file remains in {self.outbound_dir} for investigation."
                ) from exc

        self._sent.append(filename)
        log.info("sent %s (%d bytes)", filename, len(payload))

    def collect(self) -> list[tuple[str, bytes]]:
        """Retrieve everything waiting, oldest name first.

        One failed file does not stop the rest. A malformed or unreadable file
        must not prevent the next being collected, because that next one may
        be a rejection needing action before Gate Closure.
        """
        collected: list[tuple[str, bytes]] = []

        with self._connect() as ftp:
            ftp.cwd(self.inbound_dir)

            try:
                names = sorted(ftp.nlst())
            except ftplib.error_perm as exc:
                # 550 on an empty directory is common and not an error.
                if str(exc).startswith("550"):
                    return []
                raise TransportError(f"listing {self.inbound_dir}: {exc}") from exc

            for name in names:
                if name in (".", "..") or name.endswith(PARTIAL_SUFFIX):
                    continue

                buffer = io.BytesIO()
                try:
                    ftp.retrbinary(f"RETR {name}", buffer.write)
                except ftplib.all_errors as exc:
                    # Left in place, so the next run retries it. Logged rather
                    # than raised: the files after it still need collecting.
                    log.error("could not retrieve %s: %s", name, exc)
                    continue

                collected.append((name, buffer.getvalue()))

                # Only after the bytes are held. Deleting first would lose a
                # rejection we never saw.
                if self.delete_after_collect:
                    try:
                        ftp.delete(name)
                    except ftplib.all_errors as exc:
                        # We have the file; failing to delete means we will
                        # see it again. Worth knowing, not worth failing on --
                        # the receiver records by filename and will recognise
                        # the repeat.
                        log.warning("collected %s but could not delete it: %s",
                                    name, exc)

        if collected:
            log.info("collected %d file(s)", len(collected))
        return collected

    def _connect(self) -> ftplib.FTP:
        cls = ftplib.FTP_TLS if self.tls else ftplib.FTP
        try:
            ftp = cls(timeout=self.timeout_seconds)
            ftp.connect(self.host, self.port)
            ftp.login(self.username, self.password)
            if self.tls:
                # Without this the control channel is encrypted and the data
                # channel is not, which is the worst of both: it looks secure
                # and sends the file in the clear.
                ftp.prot_p()  # type: ignore[union-attr]
            ftp.set_pasv(self.passive)
            return ftp
        except ftplib.all_errors as exc:
            raise TransportError(
                f"connecting to {self.host}:{self.port} as {self.username}: {exc}"
            ) from exc
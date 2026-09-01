"""Raw file archive.

Every file we send and every file we receive is stored as bytes, unmodified,
with the database row pointing at it. This is audit evidence for qualification
first and debugging second.

Two invariants:

  * **Write once.** An object is never overwritten. Uploads are conditional on
    the object not already existing, so a retry that re-uploads the same
    generation fails loudly rather than quietly replacing settlement evidence.
  * **Bytes are authoritative.** What we archive is what went on the wire,
    including the checksum we computed. Retries re-send the archived bytes;
    nothing is regenerated, because regenerating would allocate a second
    sequence number and leave a permanent gap.

Two implementations behind one protocol: `GcsArchive` for real use and
`LocalArchive` for tests and local development. The protocol keeps the calling
code free of a GCS dependency, which is what makes the state machine testable
without cloud credentials.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

Direction = Literal["outbound", "inbound"]


class ArchiveError(RuntimeError):
    pass


class AlreadyArchived(ArchiveError):
    """An object exists at that key. Never overwrite settlement evidence."""


def object_key(
    direction: Direction, created: dt.datetime, filename: str, file_id: int
) -> str:
    """Date-partitioned key, unique per file row.

    The file id disambiguates: IDD filenames need only be unique across
    central systems within a month (2.2.5), and a corrected file after a
    header NACK reuses its sequence number, so filename alone is not a key.
    """
    if created.tzinfo is None:
        raise ArchiveError("created must be timezone-aware")
    stamp = created.astimezone(dt.timezone.utc)
    return (
        f"{direction}/{stamp:%Y/%m/%d}/{filename}-{file_id:012d}"
    )


class Archive(Protocol):
    def put(self, key: str, payload: bytes) -> str:
        """Store bytes, returning a URI. Raises AlreadyArchived if key exists."""

    def get(self, uri: str) -> bytes:
        """Retrieve archived bytes."""

    def exists(self, key: str) -> bool: ...


@dataclass
class GcsArchive:
    """Google Cloud Storage, versioned bucket with retention."""

    bucket_name: str
    _client: object | None = None

    @property
    def client(self):
        if self._client is None:
            from google.cloud import storage  # imported lazily: tests need no GCS

            self._client = storage.Client()
        return self._client

    @property
    def bucket(self):
        return self.client.bucket(self.bucket_name)

    def put(self, key: str, payload: bytes) -> str:
        from google.api_core.exceptions import PreconditionFailed

        blob = self.bucket.blob(key)
        try:
            # if_generation_match=0 means "only if this object does not exist".
            blob.upload_from_string(
                payload, content_type="text/plain", if_generation_match=0
            )
        except PreconditionFailed as exc:
            raise AlreadyArchived(f"gs://{self.bucket_name}/{key} already exists") from exc
        return f"gs://{self.bucket_name}/{key}"

    def get(self, uri: str) -> bytes:
        bucket_name, key = _split_uri(uri)
        if bucket_name != self.bucket_name:
            raise ArchiveError(f"{uri} is not in bucket {self.bucket_name}")
        blob = self.bucket.blob(key)
        if not blob.exists():
            raise ArchiveError(f"{uri} not found")
        return blob.download_as_bytes()

    def exists(self, key: str) -> bool:
        return self.bucket.blob(key).exists()


@dataclass
class LocalArchive:
    """Filesystem-backed archive for tests and local runs.

    Same write-once semantics as GCS, so tests exercise the real failure mode.
    """

    root: Path

    def _path(self, key: str) -> Path:
        if key.startswith("/") or ".." in key.split("/"):
            raise ArchiveError(f"unsafe key: {key!r}")
        return self.root / key

    def put(self, key: str, payload: bytes) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # 'xb' fails if the file exists: the local equivalent of
            # if_generation_match=0.
            with path.open("xb") as handle:
                handle.write(payload)
        except FileExistsError as exc:
            raise AlreadyArchived(f"file://{path} already exists") from exc
        return f"file://{path}"

    def get(self, uri: str) -> bytes:
        path = Path(uri.removeprefix("file://"))
        if not path.is_file():
            raise ArchiveError(f"{uri} not found")
        return path.read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()


def _split_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith("gs://"):
        raise ArchiveError(f"not a GCS URI: {uri!r}")
    bucket, _, key = uri.removeprefix("gs://").partition("/")
    if not bucket or not key:
        raise ArchiveError(f"malformed GCS URI: {uri!r}")
    return bucket, key

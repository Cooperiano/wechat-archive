"""Read-only SQLCipher4 database snapshots with authenticated WAL replay.

Source DB/WAL files are opened only in binary read mode. SQLite is used only on
the completed plaintext copy with mode=ro and immutable=1. No source SHM is
opened, and no lock/checkpoint/pragma is issued to the WeChat database.

Assumptions: WCDB SQLCipher4, 4096-byte pages, 16-byte salt, 80-byte reserve,
SHA512 HMAC with little-endian page numbers, and an already-derived raw key.
WAL format follows https://www.sqlite.org/fileformat.html#walformat.
"""
from __future__ import annotations

import hashlib
import hmac
import importlib.util
import os
import sqlite3
import struct
import sys
import tempfile
import time
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

PAGE_SIZE = 4096
SALT_SIZE = 16
RESERVE_SIZE = 80
HMAC_SIZE = 64
WAL_HEADER_SIZE = 32
WAL_FRAME_HEADER_SIZE = 24
WAL_MAGIC_LITTLE = 0x377F0682
WAL_MAGIC_BIG = 0x377F0683
SQLITE_MAX_PGNO = 0xFFFFFFFE
JOURNAL_MAGIC = bytes.fromhex("d9d505f920a163d7")


class SnapshotError(RuntimeError):
    """Snapshot failed verification; the previous output remains intact."""


class SourceChangedError(SnapshotError):
    """Source changed during capture and should be retried."""


@dataclass(frozen=True)
class WalFrame:
    pgno: int
    db_size: int
    data: bytes = field(repr=False)


@dataclass(frozen=True)
class WalState:
    frames: tuple[WalFrame, ...] = ()
    committed_frames: int = 0
    db_size: int = 0
    stale_tail: bool = False
    truncated_tail: bool = False


def wal_checksum(data: bytes, endian: str, state: tuple[int, int] = (0, 0)) -> tuple[int, int]:
    """SQLite's rolling checksum; values on disk are always big-endian."""
    if endian not in ("<", ">") or len(data) % 8:
        raise ValueError("Invalid WAL checksum input")
    s0, s1 = state
    for first, second in struct.iter_unpack(endian + "II", data):
        s0 = (s0 + first + s1) & 0xFFFFFFFF
        s1 = (s1 + second + s0) & 0xFFFFFFFF
    return s0, s1


def parse_wal(data: bytes, page_size: int = PAGE_SIZE) -> WalState:
    """Verify current-generation frames and locate the last commit boundary.

    SQLite may leave frames from a previous generation physically in the file.
    A salt mismatch ends the current generation. A checksum failure with matching
    salts is rejected, including failures in uncommitted frames. Partial final
    frames are accepted only after a valid commit and never replayed.
    """
    if not data:
        return WalState()
    if len(data) < WAL_HEADER_SIZE:
        raise SnapshotError("Incomplete WAL header")
    magic, version, wal_page_size, _, salt1, salt2, check0, check1 = struct.unpack(
        ">8I", data[:WAL_HEADER_SIZE]
    )
    if magic not in (WAL_MAGIC_LITTLE, WAL_MAGIC_BIG):
        raise SnapshotError("Unsupported WAL magic")
    if version != 3007000 or wal_page_size != page_size:
        raise SnapshotError("Unsupported WAL format or page size")
    endian = "<" if magic == WAL_MAGIC_LITTLE else ">"
    checksum = wal_checksum(data[:24], endian)
    if checksum != (check0, check1):
        raise SnapshotError("WAL header checksum failed")
    offset = WAL_HEADER_SIZE
    frame_size = WAL_FRAME_HEADER_SIZE + page_size
    frames: list[WalFrame] = []
    committed_frames = db_size = 0
    stale_tail = truncated_tail = False
    while offset < len(data):
        remaining = len(data) - offset
        if remaining < frame_size:
            if committed_frames == 0:
                raise SnapshotError("Incomplete WAL frame before a commit")
            truncated_tail = True
            break
        header = data[offset:offset + WAL_FRAME_HEADER_SIZE]
        pgno, frame_db_size, fsalt1, fsalt2, check0, check1 = struct.unpack(
            ">6I", header
        )
        if (fsalt1, fsalt2) != (salt1, salt2):
            stale_tail = True
            break
        if not 1 <= pgno <= SQLITE_MAX_PGNO or frame_db_size > SQLITE_MAX_PGNO:
            raise SnapshotError("Invalid WAL page number or commit size")
        page = data[offset + WAL_FRAME_HEADER_SIZE:offset + frame_size]
        checksum = wal_checksum(header[:8], endian, checksum)
        checksum = wal_checksum(page, endian, checksum)
        if checksum != (check0, check1):
            raise SnapshotError("WAL frame checksum failed")
        frames.append(WalFrame(pgno, frame_db_size, page))
        if frame_db_size:
            committed_frames = len(frames)
            db_size = frame_db_size
        offset += frame_size
    return WalState(tuple(frames), committed_frames, db_size, stale_tail, truncated_tail)


def load_cipher_module(path: str | Path | None = None) -> ModuleType:
    """Import the existing audited AES implementation without invoking its CLI."""
    if path is None and sys.platform == 'darwin':
        from .cipher import load_cipher
        return load_cipher()
    from .cipher import load_cipher
    return load_cipher()


class SQLCipherPageReader:
    """Authenticate every page before handing it to the existing AES routine."""

    def __init__(self, raw_key: bytes, first_page: bytes, cipher_module: ModuleType):
        if len(raw_key) != 32 or len(first_page) != PAGE_SIZE:
            raise SnapshotError("Invalid key length or first database page")
        if not cipher_module.verify_enc_key(raw_key, first_page):
            raise SnapshotError("Database first-page authentication failed (db page 1)")
        self._raw_key = raw_key
        self._salt = first_page[:SALT_SIZE]
        mac_salt = bytes(value ^ 0x3A for value in self._salt)
        self._mac_key = hashlib.pbkdf2_hmac("sha512", raw_key, mac_salt, 2, dklen=32)
        self._cipher = cipher_module

    def decrypt(self, page: bytes, pgno: int) -> bytes:
        if len(page) != PAGE_SIZE or not 1 <= pgno <= SQLITE_MAX_PGNO:
            raise SnapshotError("Invalid encrypted page")
        if pgno == 1 and page[:SALT_SIZE] != self._salt:
            raise SnapshotError("Database page salt changed")
        start = SALT_SIZE if pgno == 1 else 0
        signed_data = page[start:PAGE_SIZE - HMAC_SIZE]
        mac = hmac.new(self._mac_key, signed_data, hashlib.sha512)
        mac.update(struct.pack("<I", pgno))
        if not hmac.compare_digest(mac.digest(), page[PAGE_SIZE - HMAC_SIZE:]):
            raise SnapshotError("Encrypted page authentication failed")
        plaintext = self._cipher._decrypt_page(self._raw_key, page, pgno)
        if len(plaintext) != PAGE_SIZE:
            raise SnapshotError("Cipher returned an invalid page")
        return plaintext


def _signature(path: Path) -> tuple[int, int, int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino


def _read_optional(path: Path) -> bytes:
    try:
        with path.open("rb") as source:
            return source.read()
    except FileNotFoundError:
        return b""


def _file_digest(path: Path, *, optional: bool = False) -> bytes:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except FileNotFoundError:
        if not optional:
            raise
    return digest.digest()


def _capture_source(path: Path) -> tuple[bytes, bytes]:
    """Capture twice, detecting content changes even when timestamps are delayed."""
    wal_path = Path(str(path) + "-wal")
    journal_path = Path(str(path) + "-journal")
    sources = (path, wal_path, journal_path)
    before = tuple(_signature(item) for item in sources)
    with path.open("rb") as source:
        database = source.read()
    wal = _read_optional(wal_path)
    # Rollback recovery is intentionally not implemented. Fail rather than
    # silently treating a hot rollback database as a committed WAL database.
    journal_header = b""
    try:
        with journal_path.open("rb") as journal:
            journal_header = journal.read(8)
    except FileNotFoundError:
        pass
    if journal_header == JOURNAL_MAGIC:
        raise SourceChangedError("Active rollback journal is unsupported")
    if hashlib.sha256(database).digest() != _file_digest(path):
        raise SourceChangedError("Database changed during capture")
    if hashlib.sha256(wal).digest() != _file_digest(wal_path, optional=True):
        raise SourceChangedError("WAL changed during capture")
    if before != tuple(_signature(item) for item in sources):
        raise SourceChangedError("Source metadata changed during capture")
    return database, wal


def _decrypt_from(reader: SQLCipherPageReader, page: bytes, pgno: int, origin: str) -> bytes:
    try:
        return reader.decrypt(page, pgno)
    except SnapshotError as error:
        raise SnapshotError(f"{error} ({origin} page {pgno})") from None


def _logical_page_count(first_page: bytes, physical_pages: int) -> tuple[int, bool]:
    """Trust SQLite's logical length only when its version counter is current."""
    if first_page[:16] != b"SQLite format 3\x00" or struct.unpack_from(">H", first_page, 16)[0] != PAGE_SIZE:
        raise SnapshotError("Invalid decrypted SQLite header (db page 1)")
    header_pages = struct.unpack_from(">I", first_page, 28)[0]
    header_valid = (
        0 < header_pages <= SQLITE_MAX_PGNO
        and first_page[24:28] == first_page[92:96]
    )
    return (header_pages, True) if header_valid else (physical_pages, False)


def _write_snapshot(path: Path, database: bytes, wal: WalState, reader: SQLCipherPageReader) -> dict:
    physical_pages = len(database) // PAGE_SIZE
    first_page = _decrypt_from(reader, database[:PAGE_SIZE], 1, "db")
    logical_pages, header_valid = _logical_page_count(first_page, physical_pages)
    final_pages = wal.db_size if wal.committed_frames else logical_pages
    committed = wal.frames[:wal.committed_frames]
    committed_page_numbers = {frame.pgno for frame in committed}
    extra_pages = {pgno for pgno in committed_page_numbers if physical_pages < pgno <= final_pages}
    if final_pages > physical_pages and len(extra_pages) != final_pages - physical_pages:
        raise SnapshotError("WAL does not contain all newly allocated pages")
    zero_pages_ignored = 0
    with path.open("wb") as output:
        for index in range(physical_pages):
            pgno = index + 1
            page = database[index * PAGE_SIZE:(index + 1) * PAGE_SIZE]
            # WCDB preallocates zero-filled physical tails. A zero used page is
            # always an error; newly used preallocation must be covered by WAL.
            if pgno > logical_pages and header_valid and page == b"\x00" * PAGE_SIZE:
                if pgno <= final_pages and pgno not in committed_page_numbers:
                    raise SnapshotError(f"Unallocated page lacks committed WAL coverage (db page {pgno})")
                zero_pages_ignored += 1
                continue
            plaintext = first_page if pgno == 1 else _decrypt_from(reader, page, pgno, "db")
            # Authenticate nonzero physical pages beyond the logical EOF too.
            if pgno <= final_pages:
                output.seek(index * PAGE_SIZE)
                output.write(plaintext)
        for index, frame in enumerate(wal.frames):
            plaintext = _decrypt_from(reader, frame.data, frame.pgno, "wal")
            if index < wal.committed_frames and frame.pgno <= final_pages:
                output.seek((frame.pgno - 1) * PAGE_SIZE)
                output.write(plaintext)
        output.truncate(final_pages * PAGE_SIZE)
        output.flush()
        os.fsync(output.fileno())
    return {
        "snapshot_pages": final_pages,
        "logical_db_pages": logical_pages,
        "logical_header_valid": header_valid,
        "ignored_zero_pages": zero_pages_ignored,
    }


def validate_snapshot(path: str | Path) -> str:
    """Use immutable SQLite so validation cannot create WAL/SHM sidecars."""
    uri = Path(path).resolve().as_uri() + "?mode=ro&immutable=1"
    try:
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            checks = connection.execute("PRAGMA quick_check").fetchall()
    except sqlite3.Error:
        raise SnapshotError("SQLite snapshot validation failed") from None
    if checks != [("ok",)]:
        raise SnapshotError("SQLite snapshot quick_check failed")
    return "ok"


def snapshot_database(
    input_path: str | Path,
    output_path: str | Path,
    raw_key: bytes,
    *,
    cipher_module: ModuleType | None = None,
    cipher_path: str | Path | None = None,
    retries: int = 3,
    retry_delay: float = 0.15,
) -> dict:
    """Produce an authenticated plaintext snapshot, atomically preserving output.

    Keys are accepted only in memory and never included in errors or metrics.
    Repeated source writes, corrupt ciphertext/WAL, or invalid SQLite structures
    raise SnapshotError without replacing any previous validated output.
    """
    source = Path(input_path).resolve()
    destination = Path(output_path).resolve()
    protected = {source, *(Path(str(source) + suffix) for suffix in ("-wal", "-shm", "-journal"))}
    if destination in protected:
        raise SnapshotError("Snapshot output overlaps the source database")
    if destination.exists() and any(
        item.exists() and os.path.samefile(destination, item) for item in protected
    ):
        raise SnapshotError("Snapshot output aliases the source database")
    if len(raw_key) != 32:
        raise SnapshotError("Invalid raw key length")
    if retries < 1 or retry_delay < 0:
        raise ValueError("Invalid retry configuration")
    module = cipher_module or load_cipher_module(cipher_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    last_error: SnapshotError | None = None
    for attempt in range(1, retries + 1):
        temporary: Path | None = None
        try:
            database, wal_bytes = _capture_source(source)
            if not database or len(database) % PAGE_SIZE:
                raise SnapshotError("Database size is not a whole number of pages")
            wal = parse_wal(wal_bytes)
            reader = SQLCipherPageReader(raw_key, database[:PAGE_SIZE], module)
            descriptor, name = tempfile.mkstemp(prefix=".wechat-snapshot-", suffix=".db", dir=destination.parent)
            os.close(descriptor)
            temporary = Path(name)
            write_metrics = _write_snapshot(temporary, database, wal, reader)
            quick_check = validate_snapshot(temporary)
            sha256 = _file_digest(temporary).hex()
            os.replace(temporary, destination)
            temporary = None
            return {
                "db_pages": len(database) // PAGE_SIZE,
                "wal_frames": len(wal.frames),
                "committed_frames": wal.committed_frames,
                **write_metrics,
                "snapshot_sha256": sha256,
                "quick_check": quick_check,
                "stale_tail": wal.stale_tail,
                "truncated_tail": wal.truncated_tail,
                "attempts": attempt,
                "source_consistency": "double_read",
            }
        except SnapshotError as error:
            last_error = error
        except OSError:
            last_error = SnapshotError("Snapshot source or output is temporarily unavailable")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        if attempt < retries:
            time.sleep(retry_delay)
    raise last_error or SnapshotError("Snapshot capture failed")

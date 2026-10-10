"""Focused tests using generated SQLite fixtures; never reads user databases."""
from __future__ import annotations

import hashlib
import hmac
import sqlite3
import struct
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from wechat_archive import snapshot


def fixture_pages(directory: Path, value: str) -> bytes:
    path = directory / (value + ".db")
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("PRAGMA page_size=4096")
        connection.execute("CREATE TABLE messages (value TEXT)")
        connection.execute("INSERT INTO messages VALUES (?)", (value,))
    return path.read_bytes()


def synthetic_wal(frames: list[tuple[int, int, bytes]], endian: str = "<") -> bytes:
    magic = snapshot.WAL_MAGIC_LITTLE if endian == "<" else snapshot.WAL_MAGIC_BIG
    first = struct.pack(">6I", magic, 3007000, 4096, 1, 123, 456)
    checksum = snapshot.wal_checksum(first, endian)
    data = bytearray(first + struct.pack(">2I", *checksum))
    for pgno, db_size, page in frames:
        header = struct.pack(">4I", pgno, db_size, 123, 456)
        checksum = snapshot.wal_checksum(header[:8], endian, checksum)
        checksum = snapshot.wal_checksum(page, endian, checksum)
        data.extend(header + struct.pack(">2I", *checksum) + page)
    return bytes(data)


class PlaintextPageReader:
    def __init__(self, *_args):
        pass

    def decrypt(self, page: bytes, _pgno: int) -> bytes:
        return page


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.directory_context = tempfile.TemporaryDirectory()
        self.directory = Path(self.directory_context.name)
        self.raw_key = b"test-only-generated-fixture-key!"
        self.assertEqual(len(self.raw_key), 32)

    def tearDown(self):
        self.directory_context.cleanup()

    def capture_plaintext_fixture(self, source: Path, output: Path, page_reader=PlaintextPageReader) -> dict:
        with patch.object(snapshot, "SQLCipherPageReader", page_reader):
            return snapshot.snapshot_database(
                source, output, self.raw_key, cipher_module=SimpleNamespace(), retries=1
            )

    def test_no_wal_snapshot_preserves_source_and_creates_no_sidecars(self):
        source = self.directory / "source.db"
        source.write_bytes(fixture_pages(self.directory, "original"))
        original = source.read_bytes()
        output = self.directory / "archive.db"
        result = self.capture_plaintext_fixture(source, output)
        self.assertEqual(output.read_bytes(), original)
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(result["quick_check"], "ok")
        self.assertEqual(result["wal_frames"], 0)
        self.assertEqual(result["committed_frames"], 0)
        self.assertEqual(result["snapshot_sha256"], hashlib.sha256(original).hexdigest())
        for item in (source, output):
            self.assertFalse(Path(str(item) + "-shm").exists())
            self.assertFalse(Path(str(item) + "-wal").exists())

    def test_last_uncommitted_frame_is_authenticated_but_not_applied(self):
        source = self.directory / "source.db"
        old = fixture_pages(self.directory, "before")
        new = fixture_pages(self.directory, "committed")
        uncommitted = fixture_pages(self.directory, "pending")
        source.write_bytes(old)
        frames = [(1, 0, new[:4096]), (2, 2, new[4096:8192]), (2, 0, uncommitted[4096:8192])]
        wal_path = Path(str(source) + "-wal")
        wal_bytes = synthetic_wal(frames)
        wal_path.write_bytes(wal_bytes)
        output = self.directory / "archive.db"
        with patch.object(
            PlaintextPageReader, "decrypt", autospec=True,
            side_effect=lambda _reader, page, _pgno: page,
        ) as decrypt:
            result = self.capture_plaintext_fixture(source, output)
        self.assertEqual(decrypt.call_count, len(old) // 4096 + 3)
        self.assertEqual(result["wal_frames"], 3)
        self.assertEqual(result["committed_frames"], 2)
        self.assertEqual(output.read_bytes(), new)
        self.assertEqual(wal_path.read_bytes(), wal_bytes)
        with closing(sqlite3.connect(output.as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
            self.assertEqual(connection.execute("SELECT value FROM messages").fetchall(), [("committed",)])

    def test_corrupt_wal_checksum_rejected_and_old_archive_preserved(self):
        source = self.directory / "source.db"
        database = fixture_pages(self.directory, "old")
        source.write_bytes(database)
        wal = bytearray(synthetic_wal([(2, 2, database[4096:8192])]))
        wal[-1] ^= 1
        Path(str(source) + "-wal").write_bytes(wal)
        output = self.directory / "archive.db"
        output.write_bytes(b"previous verified archive")
        with self.assertRaisesRegex(snapshot.SnapshotError, "checksum failed"):
            self.capture_plaintext_fixture(source, output)
        self.assertEqual(output.read_bytes(), b"previous verified archive")
        self.assertEqual(list(self.directory.glob(".wechat-snapshot-*")), [])

    def test_both_checksum_endiannesses_and_tail_policy(self):
        page = b"x" * 4096
        for endian in ("<", ">"):
            with self.subTest(endian=endian):
                data = synthetic_wal([(1, 1, page)], endian)
                result = snapshot.parse_wal(data + b"partial")
                self.assertTrue(result.truncated_tail)
                self.assertEqual(result.committed_frames, 1)
                stale = struct.pack(">6I", 2, 0, 999, 888, 0, 0) + page
                result = snapshot.parse_wal(data + stale)
                self.assertTrue(result.stale_tail)
                self.assertEqual(len(result.frames), 1)
        with self.assertRaises(snapshot.SnapshotError):
            snapshot.parse_wal(synthetic_wal([(1, 0, page)]) + b"partial")

    def test_wal_written_by_sqlite_is_accepted_without_touching_source_sidecars(self):
        source = self.directory / "live.db"
        output = self.directory / "archive.db"
        with closing(sqlite3.connect(source)) as connection:
            connection.execute("PRAGMA page_size=4096")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE messages (value TEXT)")
            connection.execute("INSERT INTO messages VALUES ('baseline')")
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.execute("INSERT INTO messages VALUES ('new message')")
            connection.commit()
            paths = [source, Path(str(source) + "-wal"), Path(str(source) + "-shm")]
            before = [item.read_bytes() for item in paths]
            result = self.capture_plaintext_fixture(source, output)
            self.assertGreater(result["committed_frames"], 0)
            self.assertEqual([item.read_bytes() for item in paths], before)
            with closing(sqlite3.connect(output.as_uri() + "?mode=ro&immutable=1", uri=True)) as archive:
                self.assertEqual(archive.execute("SELECT value FROM messages").fetchall(), [("baseline",), ("new message",)])

    def test_zero_preallocation_is_ignored_only_past_trusted_logical_end(self):
        source = self.directory / "preallocated.db"
        original = fixture_pages(self.directory, "original")
        source.write_bytes(original + b"\x00" * (3 * 4096))
        output = self.directory / "archive.db"
        result = self.capture_plaintext_fixture(source, output)
        self.assertEqual(output.read_bytes(), original)
        self.assertEqual(result["db_pages"], len(original) // 4096 + 3)
        self.assertEqual(result["logical_db_pages"], len(original) // 4096)
        self.assertEqual(result["ignored_zero_pages"], 3)
        self.assertTrue(result["logical_header_valid"])

        class RejectZeroReader(PlaintextPageReader):
            def decrypt(self, page, pgno):
                if page == b"\x00" * 4096:
                    raise snapshot.SnapshotError("Encrypted page authentication failed")
                return page

        source.write_bytes(original[:4096] + b"\x00" * 4096)
        with self.assertRaisesRegex(snapshot.SnapshotError, r"db page 2"):
            self.capture_plaintext_fixture(source, output, RejectZeroReader)
        self.assertEqual(output.read_bytes(), original)

    def test_nonzero_tail_is_authenticated_even_past_logical_end(self):
        source = self.directory / "preallocated.db"
        original = fixture_pages(self.directory, "original")
        source.write_bytes(original + b"x" * 4096)

        class RejectNonzeroTail(PlaintextPageReader):
            def decrypt(self, page, pgno):
                if pgno > len(original) // 4096:
                    raise snapshot.SnapshotError("Encrypted page authentication failed")
                return page

        with self.assertRaisesRegex(snapshot.SnapshotError, r"db page 3"):
            self.capture_plaintext_fixture(source, self.directory / "archive.db", RejectNonzeroTail)

    def test_committed_wal_must_cover_newly_used_zero_preallocation(self):
        source = self.directory / "preallocated.db"
        original = fixture_pages(self.directory, "original")
        source.write_bytes(original + b"\x00" * (3 * 4096))
        growth = self.directory / "growth.db"
        with closing(sqlite3.connect(growth)) as connection, connection:
            connection.execute("PRAGMA page_size=4096")
            connection.execute("CREATE TABLE messages (value TEXT)")
            connection.execute("INSERT INTO messages VALUES (?)", ("x" * 10000,))
        grown = growth.read_bytes()
        grown_pages = len(grown) // 4096
        self.assertGreater(grown_pages, len(original) // 4096)
        frames = [(pgno, grown_pages if pgno == grown_pages else 0, grown[(pgno - 1) * 4096:pgno * 4096])
                  for pgno in range(1, grown_pages + 1)]
        wal_path = Path(str(source) + "-wal")
        wal_path.write_bytes(synthetic_wal(frames))
        output = self.directory / "archive.db"
        result = self.capture_plaintext_fixture(source, output)
        self.assertEqual(output.read_bytes(), grown)
        self.assertEqual(result["snapshot_pages"], grown_pages)
        missing = [frame for frame in frames if frame[0] != 3]
        wal_path.write_bytes(synthetic_wal(missing))
        with self.assertRaisesRegex(snapshot.SnapshotError, r"coverage \(db page 3\)"):
            self.capture_plaintext_fixture(source, output)
        self.assertEqual(output.read_bytes(), grown)

    def test_page_authentication_covers_ciphertext_and_page_number(self):
        salt = bytes(range(16))
        mac_salt = bytes(value ^ 0x3A for value in salt)
        mac_key = hashlib.pbkdf2_hmac("sha512", self.raw_key, mac_salt, 2, dklen=32)

        def signed_page(pgno):
            data = bytearray(b"x" * 4096)
            if pgno == 1:
                data[:16] = salt
            start = 16 if pgno == 1 else 0
            mac = hmac.new(mac_key, data[start:-64], hashlib.sha512)
            mac.update(struct.pack("<I", pgno))
            data[-64:] = mac.digest()
            return bytes(data)

        module = SimpleNamespace(verify_enc_key=lambda *_: True, _decrypt_page=lambda _, page, _pgno: page)
        reader = snapshot.SQLCipherPageReader(self.raw_key, signed_page(1), module)
        page = signed_page(2)
        self.assertEqual(reader.decrypt(page, 2), page)
        with self.assertRaisesRegex(snapshot.SnapshotError, "authentication failed"):
            reader.decrypt(page, 3)
        damaged = bytearray(page)
        damaged[100] ^= 1
        with self.assertRaisesRegex(snapshot.SnapshotError, "authentication failed"):
            reader.decrypt(bytes(damaged), 2)

    def test_output_cannot_overwrite_source_or_source_sidecars(self):
        source = self.directory / "source.db"
        source.write_bytes(fixture_pages(self.directory, "original"))
        for suffix in ("", "-wal", "-shm", "-journal"):
            with self.subTest(suffix=suffix), self.assertRaises(snapshot.SnapshotError):
                self.capture_plaintext_fixture(source, Path(str(source) + suffix))


if __name__ == "__main__":
    unittest.main()

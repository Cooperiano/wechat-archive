"""Synthetic Keychain/reference transaction checks; no real vault is accessed."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

from wechat_archive import security_macos as security


class FakeVault:
    def __init__(self):
        self.items = {}
        self.locked = False
        self.fail_add = False
        self.fail_delete = False

    def add(self, account, data):
        if self.locked or self.fail_add:
            raise security.KeychainError("keychain_access_denied", -25293)
        if account in self.items:
            raise security.KeychainError("keychain_record_collision", -25299)
        self.items[account] = data

    def read(self, account):
        if self.locked:
            raise security.KeychainError("keychain_locked_or_interaction_required", -25308)
        if account not in self.items:
            raise security.KeychainError("keychain_item_missing", -25300)
        return self.items[account]

    def delete(self, account):
        if self.fail_delete:
            raise security.KeychainError("keychain_access_denied", -25293)
        self.items.pop(account, None)


class KeychainStorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "keys.keychain.json"
        self.vault = FakeVault()
        self.patch = patch.object(security, "_get_vault", return_value=self.vault)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.payload = {"db_dir": "/synthetic/account/db_storage",
                        "keys_by_salt": {"synthetic_salt": "SYNTHETIC_SECRET_KEY"},
                        "passphrase": "SYNTHETIC_SECRET_PASSPHRASE"}

    def save(self, payload=None):
        security.save_keys(self.path, self.payload if payload is None else payload)
        return json.loads(self.path.read_bytes())

    def test_complete_payload_roundtrip_and_reference_contains_no_private_fields(self):
        reference = self.save()
        self.assertEqual(security.load_keys(self.path), self.payload)
        self.assertEqual(set(reference), {"format", "version", "service", "account"})
        raw = self.path.read_text()
        self.assertNotIn("SECRET", raw)
        self.assertNotIn("db_dir", raw)
        self.assertNotIn("synthetic", raw)
        self.assertEqual(reference["service"], security.SERVICE)
        self.assertTrue(reference["account"].startswith(security._path_identity(self.path) + ":"))
        self.assertEqual(list(self.vault.items.values()), [
            json.dumps(self.payload, ensure_ascii=False, allow_nan=False,
                       separators=(",", ":")).encode("utf-8")])

    def test_replace_retires_old_record_only_after_reference_commit(self):
        first = self.save()
        second = self.save({"keys_by_salt": {"another": "new"}})
        self.assertNotEqual(first["account"], second["account"])
        self.assertNotIn(first["account"], self.vault.items)
        self.assertIn(second["account"], self.vault.items)
        self.assertEqual(security.load_keys(self.path), {"keys_by_salt": {"another": "new"}})

    def test_reference_replace_failure_preserves_previous_payload_and_cleans_new_item(self):
        self.save()
        old_file = self.path.read_bytes()
        old_records = dict(self.vault.items)
        with patch.object(security.os, "replace", side_effect=OSError("unsafe-secret-detail")):
            with self.assertRaises(security.KeychainError) as error:
                security.save_keys(self.path, {"changed": True})
        self.assertEqual(str(error.exception), "keychain_reference_write_failed")
        self.assertEqual(self.path.read_bytes(), old_file)
        self.assertEqual(self.vault.items, old_records)
        self.assertEqual(security.load_keys(self.path), self.payload)
        self.assertFalse([p for p in self.path.parent.glob(".keychain-ref-*") if p.suffix != '.lock'])

    def test_file_fsync_failure_preserves_previous_payload(self):
        self.save()
        previous = self.path.read_bytes()
        with patch.object(security.os, "fsync", side_effect=OSError("unsafe-detail")):
            with self.assertRaises(security.KeychainError):
                security.save_keys(self.path, {"changed": True})
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertEqual(len(self.vault.items), 1)
        self.assertEqual(security.load_keys(self.path), self.payload)

    def test_first_reference_write_failure_leaves_no_reference_or_secret_file(self):
        with patch.object(security.tempfile, "mkstemp", side_effect=OSError("unsafe-detail")):
            with self.assertRaises(security.KeychainError):
                security.save_keys(self.path, self.payload)
        self.assertFalse(self.path.exists())
        self.assertFalse(self.vault.items)
        self.assertFalse([p for p in self.path.parent.iterdir() if p.suffix != '.lock'])

    def test_failed_vault_add_preserves_previous_reference(self):
        self.save()
        previous = self.path.read_bytes()
        self.vault.fail_add = True
        with self.assertRaises(security.KeychainError):
            security.save_keys(self.path, {"changed": True})
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertEqual(len(self.vault.items), 1)

    def test_locked_vault_fails_closed_for_load_and_save(self):
        self.save()
        previous = self.path.read_bytes()
        self.vault.locked = True
        with self.assertRaises(security.KeychainError) as error:
            security.load_keys(self.path)
        self.assertEqual(error.exception.status, -25308)
        with self.assertRaises(security.KeychainError):
            security.save_keys(self.path, {"changed": True})
        self.assertEqual(self.path.read_bytes(), previous)

    def test_missing_vault_record_fails_closed_without_replacing_reference(self):
        self.save()
        previous = self.path.read_bytes()
        self.vault.items.clear()
        with self.assertRaises(security.KeychainError) as error:
            security.load_keys(self.path)
        self.assertEqual(error.exception.reason, "keychain_item_missing")
        with self.assertRaises(security.KeychainError):
            security.save_keys(self.path, {"changed": True})
        self.assertEqual(self.path.read_bytes(), previous)

    def test_reference_copied_to_different_path_is_rejected(self):
        self.save()
        other = self.path.parent / "moved.keys.json"
        shutil.copyfile(self.path, other)
        if os.name == "posix":
            other.chmod(0o600)
        with self.assertRaises(security.KeychainError) as error:
            security.load_keys(other)
        self.assertEqual(error.exception.reason, "keychain_reference_invalid")

    def test_malformed_or_foreign_reference_is_not_overwritten(self):
        self.path.write_text('{"keys_by_salt":{"should":"never-write-here"}}')
        if os.name == "posix":
            self.path.chmod(0o600)
        previous = self.path.read_bytes()
        with self.assertRaises(security.KeychainError):
            self.save()
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertFalse(self.vault.items)

    def test_missing_reference_error_is_sanitized(self):
        with self.assertRaises(security.KeychainError) as error:
            security.load_keys(self.path)
        self.assertEqual(str(error.exception), "keychain_reference_missing")
        self.assertNotIn(str(self.path), str(error.exception))

    def test_malformed_vault_payload_does_not_return_partial_data(self):
        reference = self.save()
        self.vault.items[reference["account"]] = b'{"keys": "bad"'
        with self.assertRaises(security.KeychainError) as error:
            security.load_keys(self.path)
        self.assertEqual(error.exception.reason, "keychain_payload_invalid")

    def test_payload_validation_precedes_any_vault_write(self):
        for payload in (["not", "a", "dict"], {"bad": float("nan")}, {"bad": object()}):
            with self.assertRaises(security.KeychainError):
                security.save_keys(self.path, payload)
        self.assertFalse(self.path.exists())
        self.assertFalse(self.vault.items)

    def test_directory_fsync_failure_retains_old_record_without_undoing_commit(self):
        first = self.save()
        with patch.object(security, "_sync_directory", return_value=False):
            second = self.save({"changed": True})
        self.assertIn(first["account"], self.vault.items)
        self.assertIn(second["account"], self.vault.items)
        self.assertEqual(security.load_keys(self.path), {"changed": True})

    def test_old_cleanup_failure_does_not_fail_successful_commit(self):
        first = self.save()
        self.vault.fail_delete = True
        second = self.save({"changed": True})
        self.assertIn(first["account"], self.vault.items)
        self.assertIn(second["account"], self.vault.items)
        self.assertEqual(security.load_keys(self.path), {"changed": True})

    @unittest.skipUnless(os.name == "posix", "POSIX file modes required")
    def test_reference_is_mode_0600_and_unsafe_mode_fails_closed(self):
        self.save()
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.path.chmod(0o644)
        with self.assertRaises(security.KeychainError) as error:
            security.load_keys(self.path)
        self.assertEqual(error.exception.reason, "keychain_reference_permissions_unsafe")

    @unittest.skipIf(sys.platform == "darwin", "No real native Keychain access in synthetic tests")
    def test_native_vault_is_unavailable_on_other_platforms(self):
        with self.assertRaises(security.KeychainError) as error:
            security._NativeKeychain()
        self.assertEqual(error.exception.reason, "macos_keychain_unavailable")


if __name__ == "__main__":
    unittest.main()

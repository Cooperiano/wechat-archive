"""Keep the complete private key payload in the current user's macOS Keychain.

The file at ``key_path`` is only an opaque reference, never a secret export.
Each save adds an immutable Keychain item, then atomically commits its reference.
Native bindings are lazy so importing this module on other platforms is harmless.
"""
from contextlib import contextmanager
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import threading
import uuid


SERVICE = "org.julian.wechatarchive.keys"
REFERENCE_FORMAT = "wechat-archive-macos-keychain"
MAX_PAYLOAD_BYTES = 8 * 1024 * 1024
_REFERENCE_KEYS = {"format", "version", "service", "account"}
_lock = threading.RLock()
_native_lock = threading.RLock()
_vault = None


class KeychainError(RuntimeError):
    """A deliberately sanitized error, with no key material or personal paths."""

    def __init__(self, reason: str, status: int | None = None):
        self.reason = reason
        self.status = status
        suffix = "" if status is None else f" (OSStatus {status})"
        super().__init__(reason + suffix)


def _check_status(status: int) -> None:
    if status == 0:
        return
    reason = {
        -25300: "keychain_item_missing",
        -25308: "keychain_locked_or_interaction_required",
        -25293: "keychain_access_denied",
        -25299: "keychain_record_collision",
    }.get(status, "keychain_operation_failed")
    raise KeychainError(reason, status)


class _NativeKeychain:
    """Minimal Security.framework generic-password binding; no subprocesses."""

    def __init__(self):
        if sys.platform != "darwin":
            raise KeychainError("macos_keychain_unavailable")
        try:
            self.cf = ctypes.CDLL(
                "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
            self.security = ctypes.CDLL(
                "/System/Library/Frameworks/Security.framework/Security")
            self._bind()
        except (OSError, AttributeError, ValueError):
            raise KeychainError("macos_keychain_unavailable") from None

    def _bind(self):
        ptr = ctypes.c_void_p
        self.cf.CFDictionaryCreateMutable.argtypes = [ptr, ctypes.c_long, ptr, ptr]
        self.cf.CFDictionaryCreateMutable.restype = ptr
        self.cf.CFDictionarySetValue.argtypes = [ptr, ptr, ptr]
        self.cf.CFDictionarySetValue.restype = None
        self.cf.CFStringCreateWithCString.argtypes = [ptr, ctypes.c_char_p, ctypes.c_uint32]
        self.cf.CFStringCreateWithCString.restype = ptr
        self.cf.CFDataCreate.argtypes = [ptr, ptr, ctypes.c_long]
        self.cf.CFDataCreate.restype = ptr
        self.cf.CFDataGetLength.argtypes = [ptr]
        self.cf.CFDataGetLength.restype = ctypes.c_long
        self.cf.CFDataGetBytePtr.argtypes = [ptr]
        self.cf.CFDataGetBytePtr.restype = ptr
        self.cf.CFGetTypeID.argtypes = [ptr]
        self.cf.CFGetTypeID.restype = ctypes.c_ulong
        self.cf.CFDataGetTypeID.argtypes = []
        self.cf.CFDataGetTypeID.restype = ctypes.c_ulong
        self.cf.CFRelease.argtypes = [ptr]
        self.cf.CFRelease.restype = None
        self.security.SecItemAdd.argtypes = [ptr, ctypes.POINTER(ptr)]
        self.security.SecItemAdd.restype = ctypes.c_int32
        self.security.SecItemCopyMatching.argtypes = [ptr, ctypes.POINTER(ptr)]
        self.security.SecItemCopyMatching.restype = ctypes.c_int32
        self.security.SecItemDelete.argtypes = [ptr]
        self.security.SecItemDelete.restype = ctypes.c_int32
        self.security.SecKeychainGetUserInteractionAllowed.argtypes = [ctypes.POINTER(ctypes.c_ubyte)]
        self.security.SecKeychainGetUserInteractionAllowed.restype = ctypes.c_int32
        self.security.SecKeychainSetUserInteractionAllowed.argtypes = [ctypes.c_ubyte]
        self.security.SecKeychainSetUserInteractionAllowed.restype = ctypes.c_int32
        # These exported symbols are structures, not CF object pointer variables.
        self.key_callbacks = ctypes.addressof(
            ctypes.c_byte.in_dll(self.cf, "kCFTypeDictionaryKeyCallBacks"))
        self.value_callbacks = ctypes.addressof(
            ctypes.c_byte.in_dll(self.cf, "kCFTypeDictionaryValueCallBacks"))
        self.true = ctypes.c_void_p.in_dll(self.cf, "kCFBooleanTrue").value
        self.false = ctypes.c_void_p.in_dll(self.cf, "kCFBooleanFalse").value
        self.constants = {
            name: ctypes.c_void_p.in_dll(self.security, name).value
            for name in (
                "kSecClass", "kSecClassGenericPassword", "kSecAttrService",
                "kSecAttrAccount", "kSecAttrLabel", "kSecAttrSynchronizable",
                "kSecUseAuthenticationUI", "kSecUseAuthenticationUIFail",
                "kSecValueData", "kSecReturnData", "kSecMatchLimit", "kSecMatchLimitOne",
            )
        }
        if not self.true or not self.false or not all(self.constants.values()):
            raise KeychainError("macos_keychain_unavailable")

    def _set(self, query, name, value):
        self.cf.CFDictionarySetValue(query, self.constants[name], value)

    def _set_string(self, query, name, value):
        item = self.cf.CFStringCreateWithCString(None, value.encode("utf-8"), 0x08000100)
        if not item:
            raise KeychainError("keychain_native_allocation_failed")
        try:
            self._set(query, name, item)
        finally:
            self.cf.CFRelease(item)

    @contextmanager
    def _query(self, account, data=None, return_data=False):
        query = self.cf.CFDictionaryCreateMutable(
            None, 0, self.key_callbacks, self.value_callbacks)
        if not query:
            raise KeychainError("keychain_native_allocation_failed")
        try:
            self._set(query, "kSecClass", self.constants["kSecClassGenericPassword"])
            self._set_string(query, "kSecAttrService", SERVICE)
            self._set_string(query, "kSecAttrAccount", account)
            # Use the ordinary file-based login Keychain; do not sync to iCloud.
            self._set(query, "kSecAttrSynchronizable", self.false)
            self._set(query, "kSecUseAuthenticationUI", self.constants["kSecUseAuthenticationUIFail"])
            if data is not None:
                self._set_string(query, "kSecAttrLabel", "WeChat Archive local keys")
                buffer = ctypes.create_string_buffer(data)
                item = self.cf.CFDataCreate(None, buffer, len(data))
                if not item:
                    raise KeychainError("keychain_native_allocation_failed")
                try:
                    self._set(query, "kSecValueData", item)
                finally:
                    self.cf.CFRelease(item)
            if return_data:
                self._set(query, "kSecReturnData", self.true)
                self._set(query, "kSecMatchLimit", self.constants["kSecMatchLimitOne"])
            yield query
        finally:
            self.cf.CFRelease(query)

    @contextmanager
    def _no_interaction(self):
        # The process-wide flag also suppresses dialogs from the file-based API.
        with _native_lock:
            previous = ctypes.c_ubyte()
            _check_status(self.security.SecKeychainGetUserInteractionAllowed(ctypes.byref(previous)))
            _check_status(self.security.SecKeychainSetUserInteractionAllowed(0))
            try:
                yield
            finally:
                self.security.SecKeychainSetUserInteractionAllowed(previous.value)

    def add(self, account: str, data: bytes) -> None:
        with self._no_interaction(), self._query(account, data=data) as query:
            _check_status(self.security.SecItemAdd(query, None))

    def read(self, account: str) -> bytes:
        result = ctypes.c_void_p()
        with self._no_interaction(), self._query(account, return_data=True) as query:
            try:
                _check_status(self.security.SecItemCopyMatching(query, ctypes.byref(result)))
                if not result.value or self.cf.CFGetTypeID(result) != self.cf.CFDataGetTypeID():
                    raise KeychainError("keychain_payload_invalid")
                size = self.cf.CFDataGetLength(result)
                if size < 0 or size > MAX_PAYLOAD_BYTES:
                    raise KeychainError("keychain_payload_invalid")
                return ctypes.string_at(self.cf.CFDataGetBytePtr(result), size)
            finally:
                if result.value:
                    self.cf.CFRelease(result)

    def delete(self, account: str) -> None:
        with self._no_interaction(), self._query(account) as query:
            status = self.security.SecItemDelete(query)
            if status != -25300:
                _check_status(status)


def _get_vault():
    global _vault
    if _vault is None:
        _vault = _NativeKeychain()
    return _vault


def _path_identity(path: Path) -> str:
    return hashlib.sha256(os.fsencode(path.expanduser().resolve())).hexdigest()


def _validate_reference(path: Path, reference: object) -> dict:
    if not isinstance(reference, dict) or set(reference) != _REFERENCE_KEYS:
        raise KeychainError("keychain_reference_invalid")
    account = reference.get("account")
    if (reference.get("format") != REFERENCE_FORMAT
            or type(reference.get("version")) is not int or reference["version"] != 1
            or reference.get("service") != SERVICE or not isinstance(account, str)
            or re.fullmatch(re.escape(_path_identity(path)) + r":[0-9a-f]{32}", account) is None):
        raise KeychainError("keychain_reference_invalid")
    return reference


def _check_private_file(info) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise KeychainError("keychain_reference_invalid")
    if os.name == "posix" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise KeychainError("keychain_reference_permissions_unsafe")


def _read_reference(path: Path) -> dict:
    descriptor = None
    try:
        if path.is_symlink():
            raise KeychainError("keychain_reference_invalid")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        _check_private_file(os.fstat(descriptor))
        with os.fdopen(descriptor, "rb") as reader:
            descriptor = None
            raw = reader.read(4097)
        if len(raw) > 4096:
            raise KeychainError("keychain_reference_invalid")
        return _validate_reference(path, json.loads(raw))
    except FileNotFoundError:
        raise KeychainError("keychain_reference_missing") from None
    except (OSError, ValueError, UnicodeError):
        raise KeychainError("keychain_reference_invalid") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


@contextmanager
def _reference_lock(path: Path):
    # Serialize readers and writers so old-item retirement cannot race a reader.
    with _lock:
        if sys.platform != "darwin":
            yield
            return
        import fcntl
        descriptor = None
        try:
            try:
                lock_path = path.parent / (".keychain-ref-" + _path_identity(path)[:24] + ".lock")
                descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
                _check_private_file(os.fstat(descriptor))
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            except OSError:
                raise KeychainError("keychain_reference_lock_failed") from None
            yield
        finally:
            if descriptor is not None:
                os.close(descriptor)


def _decode_payload(data: bytes) -> dict:
    try:
        if not isinstance(data, bytes) or len(data) > MAX_PAYLOAD_BYTES:
            raise ValueError()
        payload = json.loads(data)
        if not isinstance(payload, dict):
            raise ValueError()
        return payload
    except (ValueError, UnicodeError):
        raise KeychainError("keychain_payload_invalid") from None


def load_keys(path: Path) -> dict:
    path = Path(path).expanduser().absolute()
    with _reference_lock(path):
        reference = _read_reference(path)
        return _decode_payload(_get_vault().read(reference["account"]))


def _sync_directory(path: Path) -> bool:
    # Directory fsync is unsupported on some filesystems. Retain the previous
    # vault record in that case rather than removing its last durable copy.
    if os.name != "posix":
        return True
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        os.fsync(descriptor)
        return True
    except OSError:
        return False
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _retire(vault, account) -> None:
    try:
        vault.delete(account)
    except Exception:
        # A leftover immutable Keychain record is safer than a failed commit.
        pass


def save_keys(path: Path, payload: dict) -> None:
    path = Path(path).expanduser().absolute()
    try:
        if not isinstance(payload, dict):
            raise ValueError()
        data = json.dumps(payload, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")).encode("utf-8")
        if len(data) > MAX_PAYLOAD_BYTES:
            raise ValueError()
    except (ValueError, TypeError, UnicodeError):
        raise KeychainError("keychain_payload_invalid") from None
    with _reference_lock(path):
        vault = _get_vault()
        old = None
        try:
            old = _read_reference(path)
        except KeychainError as error:
            if error.reason != "keychain_reference_missing":
                raise
        if old is not None:
            # Fail closed on locked, missing or malformed existing vault data.
            _decode_payload(vault.read(old["account"]))
        account = _path_identity(path) + ":" + uuid.uuid4().hex
        reference = {"format": REFERENCE_FORMAT, "version": 1,
                     "service": SERVICE, "account": account}
        vault.add(account, data)
        temporary = None
        committed = False
        try:
            descriptor, temporary = tempfile.mkstemp(prefix=".keychain-ref-", dir=path.parent)
            with os.fdopen(descriptor, "wb") as writer:
                if os.name == "posix":
                    os.fchmod(writer.fileno(), 0o600)
                writer.write(json.dumps(reference, separators=(",", ":")).encode("ascii"))
                writer.flush()
                os.fsync(writer.fileno())
            os.replace(temporary, path)
            committed = True
            if _sync_directory(path.parent) and old is not None:
                _retire(vault, old["account"])
        except OSError:
            raise KeychainError("keychain_reference_write_failed") from None
        finally:
            if temporary is not None:
                try:
                    Path(temporary).unlink(missing_ok=True)
                except OSError:
                    pass
            if not committed:
                _retire(vault, account)

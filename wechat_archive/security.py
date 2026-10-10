"""Protect private state with the current user's native OS credential store."""
import ctypes
import ctypes.wintypes as wt
import json
import os
import tempfile
import sys
from pathlib import Path


class Blob(ctypes.Structure):
    _fields_ = [("size", wt.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _transform(data: bytes, decrypt: bool) -> bytes:
    buf = ctypes.create_string_buffer(data)
    incoming = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = Blob()
    api = ctypes.WinDLL("crypt32", use_last_error=True)
    arguments = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                 ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(Blob)]
    if decrypt:
        fn = api.CryptUnprotectData
        description = ctypes.c_void_p()
        second = ctypes.byref(description)
    else:
        fn = api.CryptProtectData
        description = None
        second = None
    fn.argtypes = arguments
    fn.restype = wt.BOOL
    if not fn(ctypes.byref(incoming), second, None, None, None, 1, ctypes.byref(outgoing)):
        raise ctypes.WinError(ctypes.get_last_error())
    kernel = ctypes.WinDLL("kernel32")
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    try:
        return ctypes.string_at(outgoing.data, outgoing.size)
    finally:
        kernel.LocalFree(outgoing.data)
        if description:
            kernel.LocalFree(description)


def load_keys(path: Path) -> dict:
    if sys.platform == 'darwin':
        from .security_macos import load_keys as mac_load_keys
        return mac_load_keys(Path(path))
    if sys.platform != 'win32':
        raise RuntimeError('Native credential storage requires Windows or macOS')
    return json.loads(_transform(path.read_bytes(), True))


def save_keys(path: Path, payload: dict) -> None:
    if sys.platform == 'darwin':
        from .security_macos import save_keys as mac_save_keys
        mac_save_keys(Path(path), payload)
        return
    if sys.platform != 'win32':
        raise RuntimeError('Native credential storage requires Windows or macOS')
    data = _transform(json.dumps(payload).encode("utf-8"), False)
    descriptor, name = tempfile.mkstemp(prefix=".keys-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as writer:
            writer.write(data)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)

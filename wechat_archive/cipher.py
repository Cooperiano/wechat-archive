"""OS AES adapters with the authenticated SQLCipher page contract."""
from __future__ import annotations
import ctypes
import ctypes.util
from functools import lru_cache
import hashlib
import hmac
import struct
import sys


@lru_cache(maxsize=1)
def native_mac():
    library = ctypes.CDLL(ctypes.util.find_library('System') or '/usr/lib/libSystem.B.dylib')
    library.CCCrypt.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
                               ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
                               ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
                               ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    library.CCCrypt.restype = ctypes.c_int32
    return library


def aes_cbc_decrypt(key, iv, data):
    if len(key) != 32 or len(iv) != 16 or not data or len(data) % 16:
        raise ValueError('invalid_aes_input')
    if sys.platform == 'win32':
        from .vendor.wcdb_windows import aes_cbc_decrypt as decrypt
        return decrypt(key, iv, data)
    if sys.platform != 'darwin':
        raise RuntimeError('native_decryption_requires_macos_or_windows')
    buffers = [ctypes.create_string_buffer(value) for value in (key, iv, data)]
    output = ctypes.create_string_buffer(len(data) + 16)
    used = ctypes.c_size_t()
    status = native_mac().CCCrypt(1, 0, 0, buffers[0], len(key), buffers[1],
                                  buffers[2], len(data), output, len(output), ctypes.byref(used))
    if status != 0 or used.value != len(data):
        raise RuntimeError('native_decryption_failed')
    return output.raw[:used.value]


def verify_enc_key(key, page):
    if len(key) != 32 or len(page) != 4096:
        return False
    salt = bytes(v ^ 0x3a for v in page[:16])
    mac_key = hashlib.pbkdf2_hmac('sha512', key, salt, 2, 32)
    digest = hmac.new(mac_key, page[16:-64] + struct.pack('<I', 1), hashlib.sha512).digest()
    return hmac.compare_digest(digest, page[-64:])


def _decrypt_page(key, page, pgno):
    start = 16 if pgno == 1 else 0
    plaintext = aes_cbc_decrypt(key, page[-80:-64], page[start:-80])
    return (b'SQLite format 3\x00' if pgno == 1 else b'') + plaintext + b'\0' * 80


def load_cipher():
    return sys.modules[__name__]

"""Authenticated credential encryption; the master key never lives in SQLite."""

import ctypes
import json
import os
from pathlib import Path
import stat

from cryptography.fernet import Fernet, InvalidToken

from . import private_files


def _dpapi(data, decrypt=False):
    """Protect a Windows key file for the current user, without showing UI."""
    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_uint32),
                    ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = (crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData)
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_uint32, ctypes.POINTER(Blob)]
    function.restype = ctypes.c_int
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    if not function(ctypes.byref(source), None, None, None, None, 1,
                    ctypes.byref(target)):
        raise RuntimeError("Windows could not unlock the encryption key for this account")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel32.LocalFree(target.data)


class CredentialCipher:
    def __init__(self, key):
        try:
            self._fernet = Fernet(key)
        except (ValueError, TypeError) as exc:
            raise ValueError("invalid encryption master key (expected a Fernet key)") from exc

    def encrypt(self, instance_id, secret):
        # Bind ciphertext to its row so swapping two blobs cannot swap accounts.
        payload = json.dumps({"id": instance_id, "secret": secret}).encode("utf-8")
        return self._fernet.encrypt(payload)

    def decrypt(self, instance_id, ciphertext):
        try:
            payload = json.loads(self._fernet.decrypt(bytes(ciphertext)))
            if payload["id"] != instance_id or not isinstance(payload["secret"], str):
                raise ValueError
            return payload["secret"]
        except (InvalidToken, ValueError, KeyError, TypeError) as exc:
            raise ValueError("stored credentials cannot be decrypted with this master key") from exc


def load_cipher(database, key_file="", existing=False):
    """Load env key, otherwise a separate protected file; never replace lost keys."""
    environment_key = os.environ.get("DASH_ENCRYPTION_KEY")
    if environment_key:
        try:
            key = environment_key.encode("ascii")
        except UnicodeError:
            raise ValueError("invalid encryption master key (expected a Fernet key)") from None
        return CredentialCipher(key)
    if database == ":memory:" and not key_file:
        return CredentialCipher(Fernet.generate_key())

    path = Path(key_file or (str(database) + ".key"))
    if path.is_symlink():
        raise ValueError("encryption key file must not be a symlink")
    if not path.exists():
        if existing:
            raise ValueError("encryption key is missing; restore its file or DASH_ENCRYPTION_KEY")
        key = Fernet.generate_key()
        contents = b"DPAPI1\n" + _dpapi(key) if os.name == "nt" else b"FERNET1\n" + key
        # Exclusive creation prevents a second process from overwriting the key.
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        # The mode above is owner-only on POSIX. Windows has no mode, so the
        # file would inherit whatever its folder allows.
        private_files.protect(path)
    elif os.name == "nt":
        private_files.protect(path)
    if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError("encryption key file must have owner-only permissions (chmod 600)")
    contents = path.read_bytes()
    if contents.startswith(b"DPAPI1\n") and os.name == "nt":
        key = _dpapi(contents[7:], decrypt=True)
    elif contents.startswith(b"FERNET1\n") and os.name != "nt":
        key = contents[8:]
    else:
        raise ValueError("encryption key file belongs to a different platform or is invalid")
    return CredentialCipher(key)

"""Keep the database and the key file private to the account that runs the daemon.

A file made by SQLite or ``open`` takes its permissions from the umask or from
the folder it sits in: world-readable on a default Linux umask, and writable by
every signed-in user in a folder under ``D:\\`` on Windows. The database holds
the configuration that decides which program the daemon runs and which address
receives a saved key, so another local user must not be able to edit it.

``create_private`` makes a file owner-only from the start, ``protect`` fixes one
that already exists, and ``exposure`` names what a file permission cannot fix:
a folder that other users can write to, where they could delete the database
and put their own in its place.
"""

import ctypes
import logging
import os
import stat
from pathlib import Path

log = logging.getLogger(__name__)

# Rights that let someone change or replace what is in a folder or a file:
# write data / add file, append / add subfolder, delete child, delete,
# change permissions, take ownership, generic write and generic all.
_WRITE_MASK = 0x2 | 0x4 | 0x40 | 0x10000 | 0x40000 | 0x80000 | 0x40000000 | 0x10000000
# Everyone, Authenticated Users and Users: groups that hold every local account.
_BROAD_GROUPS = {"S-1-1-0": "Everyone", "S-1-5-11": "Authenticated Users",
                 "S-1-5-32-545": "Users"}
_SYSTEM = "S-1-5-18"
_ADMINISTRATORS = "S-1-5-32-544"


def _is_memory(path):
    return str(path) in ("", ":memory:")


# -- Windows -----------------------------------------------------------------

if os.name == "nt":
    from ctypes import wintypes

    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _PVOID = ctypes.c_void_p
    _SE_FILE_OBJECT = 1
    _DACL_SECURITY_INFORMATION = 0x4
    _PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
    _SE_DACL_PROTECTED = 0x1000
    _TOKEN_QUERY = 0x8
    _TOKEN_USER = 1
    _ACL_SIZE_INFORMATION = 2
    _INHERIT_ONLY_ACE = 0x08
    _ACCESS_ALLOWED_ACE_TYPE = 0

    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.LocalFree.argtypes = [_PVOID]
    _kernel32.LocalFree.restype = _PVOID
    _advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                           ctypes.POINTER(wintypes.HANDLE)]
    _advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, _PVOID,
                                              wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    _advapi32.ConvertSidToStringSidW.argtypes = [_PVOID, ctypes.POINTER(wintypes.LPWSTR)]
    _advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, ctypes.POINTER(_PVOID),
        ctypes.POINTER(_PVOID), ctypes.POINTER(_PVOID), ctypes.POINTER(_PVOID),
        ctypes.POINTER(_PVOID)]
    _advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    _advapi32.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, _PVOID, _PVOID, _PVOID, _PVOID]
    _advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
    _advapi32.GetSecurityDescriptorControl.argtypes = [
        _PVOID, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD)]
    _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(_PVOID), ctypes.POINTER(wintypes.ULONG)]
    _advapi32.GetSecurityDescriptorDacl.argtypes = [
        _PVOID, ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(_PVOID),
        ctypes.POINTER(wintypes.BOOL)]
    _advapi32.GetAclInformation.argtypes = [_PVOID, _PVOID, wintypes.DWORD, ctypes.c_int]
    _advapi32.GetAce.argtypes = [_PVOID, wintypes.DWORD, ctypes.POINTER(_PVOID)]

    class _AclSize(ctypes.Structure):
        _fields_ = [("AceCount", wintypes.DWORD), ("AclBytesInUse", wintypes.DWORD),
                    ("AclBytesFree", wintypes.DWORD)]

    class _AceHeader(ctypes.Structure):
        _fields_ = [("AceType", ctypes.c_ubyte), ("AceFlags", ctypes.c_ubyte),
                    ("AceSize", wintypes.WORD), ("Mask", wintypes.DWORD)]

    def _sid_text(sid):
        text = wintypes.LPWSTR()
        if not _advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise OSError(ctypes.get_last_error(), "cannot read a security identifier")
        try:
            return text.value
        finally:
            _kernel32.LocalFree(ctypes.cast(text, _PVOID))

    def _current_user_sid():
        token = wintypes.HANDLE()
        if not _advapi32.OpenProcessToken(_kernel32.GetCurrentProcess(), _TOKEN_QUERY,
                                          ctypes.byref(token)):
            raise OSError(ctypes.get_last_error(), "cannot open the process token")
        try:
            size = wintypes.DWORD()
            _advapi32.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(size))
            buffer = ctypes.create_string_buffer(size.value)
            if not _advapi32.GetTokenInformation(token, _TOKEN_USER, buffer, size,
                                                 ctypes.byref(size)):
                raise OSError(ctypes.get_last_error(), "cannot read the account's identity")
            # TOKEN_USER starts with a SID_AND_ATTRIBUTES, whose first field is the SID.
            return _sid_text(ctypes.c_void_p.from_buffer(buffer).value)
        finally:
            _kernel32.CloseHandle(token)

    def _read_dacl(path):
        """``(protected, aces)`` for a path; ``aces`` is ``None`` for a null DACL.

        Each ACE is ``(sid_text, mask, flags)`` and only "allow" ACEs that apply
        to the object itself are listed.
        """
        dacl, descriptor = _PVOID(), _PVOID()
        code = _advapi32.GetNamedSecurityInfoW(
            str(path), _SE_FILE_OBJECT, _DACL_SECURITY_INFORMATION, None, None,
            ctypes.byref(dacl), None, ctypes.byref(descriptor))
        if code:
            raise OSError(code, "cannot read the permissions of a file")
        try:
            control, revision = wintypes.WORD(), wintypes.DWORD()
            _advapi32.GetSecurityDescriptorControl(descriptor, ctypes.byref(control),
                                                   ctypes.byref(revision))
            protected = bool(control.value & _SE_DACL_PROTECTED)
            if not dacl.value:
                return protected, None
            info = _AclSize()
            if not _advapi32.GetAclInformation(dacl, ctypes.byref(info), ctypes.sizeof(info),
                                               _ACL_SIZE_INFORMATION):
                raise OSError(ctypes.get_last_error(), "cannot read a permission list")
            aces = []
            for index in range(info.AceCount):
                ace = _PVOID()
                if not _advapi32.GetAce(dacl, index, ctypes.byref(ace)):
                    continue
                header = _AceHeader.from_address(ace.value)
                if (header.AceType != _ACCESS_ALLOWED_ACE_TYPE or
                        header.AceFlags & _INHERIT_ONLY_ACE):
                    continue
                # The SID follows the header and the mask, 8 bytes in.
                aces.append((_sid_text(ace.value + 8), header.Mask, header.AceFlags))
            return protected, aces
        finally:
            _kernel32.LocalFree(descriptor)

    def _apply_sddl(path, sddl):
        """Replace a path's permissions with the ones an SDDL string describes."""
        descriptor, size = _PVOID(), wintypes.ULONG()
        if not _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl, 1, ctypes.byref(descriptor), ctypes.byref(size)):
            raise OSError(ctypes.get_last_error(), "invalid permission description")
        try:
            present, defaulted, dacl = wintypes.BOOL(), wintypes.BOOL(), _PVOID()
            if not _advapi32.GetSecurityDescriptorDacl(
                    descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)):
                raise OSError(ctypes.get_last_error(), "invalid permission description")
            code = _advapi32.SetNamedSecurityInfoW(
                str(path), _SE_FILE_OBJECT,
                _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION,
                None, None, dacl, None)
            if code:
                raise OSError(code, "cannot change the permissions of a file")
        finally:
            _kernel32.LocalFree(descriptor)

    def _owner_only_sddl():
        # This account, the system and the administrators, as OpenSSH requires
        # of a private key. Inheritance from the folder is switched off.
        return "D:P(A;;FA;;;%s)(A;;FA;;;SY)(A;;FA;;;BA)" % _current_user_sid()

    def _protected_already(path):
        """True when the list is cut off from the folder and no broad group is in it.

        A grant to a named account, say a backup service, was somebody's choice
        and is left alone. Everyone, Authenticated Users and Users never are.
        """
        protected, aces = _read_dacl(path)
        return (protected and aces is not None and
                not any(sid in _BROAD_GROUPS for sid, _, _ in aces))

    def _restrict(path):
        if _protected_already(path):
            return False
        _apply_sddl(path, _owner_only_sddl())
        return True

    def _broad_write_access(path):
        """The broad groups that can change or replace what is at ``path``."""
        protected, aces = _read_dacl(path)
        if aces is None:
            return ["Everyone (no permission list at all)"]
        found = []
        for sid, mask, _ in aces:
            if sid in _BROAD_GROUPS and mask & _WRITE_MASK and _BROAD_GROUPS[sid] not in found:
                found.append(_BROAD_GROUPS[sid])
        return found

else:

    def _restrict(path):
        mode = stat.S_IMODE(os.stat(path).st_mode)
        if mode & 0o077:
            os.chmod(path, 0o600)
            return True
        return False

    def _broad_write_access(path):
        status = os.stat(path)
        mode = stat.S_IMODE(status.st_mode)
        if mode & 0o022 and not (stat.S_ISDIR(status.st_mode) and mode & stat.S_ISVTX):
            return ["other users"]
        return []


# -- The calls the daemon makes -----------------------------------------------


def create_private(path):
    """Create an empty file only this account can use; leave an existing one alone."""
    if _is_memory(path) or os.path.exists(path):
        return False
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    protect(path)
    return True


def protect(path):
    """Make an existing file owner-only. Never raises; returns whether it changed.

    A failure is logged and the daemon carries on: refusing to start over a
    permission it cannot change would be worse than the exposure.
    """
    if _is_memory(path) or not os.path.exists(path):
        return False
    try:
        return _restrict(path)
    except Exception as exc:
        log.warning("could not make %s private to this account (%s)",
                    Path(path).name, type(exc).__name__)
        return False


def exposure(path):
    """What lets other users change ``path``, or the folder it lives in.

    A private file in a folder other users can write to is still replaceable:
    they can delete it and put their own there.
    """
    if _is_memory(path):
        return []
    problems = []
    folder = Path(path).resolve().parent
    for target, label in ((Path(path), "the file"), (folder, "its folder")):
        if not target.exists():
            continue
        try:
            who = _broad_write_access(target)
        except Exception as exc:
            log.debug("cannot read the permissions of %s (%s)", target.name, type(exc).__name__)
            continue
        if who:
            problems.append("%s of %s can be changed by %s" %
                            (label, Path(path).name, ", ".join(who)))
    return problems


def check_private_files(paths):
    """Warn once for each private file whose surroundings other users can write to."""
    for path in paths:
        for problem in exposure(path):
            log.warning("%s: other accounts could replace it; move it to a folder "
                        "only this account can write to", problem)

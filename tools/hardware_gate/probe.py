"""Isolated, non-secret MicroPython capability and bounded filesystem probe.

Executed by presto_hardware_gate.py, never installed in the application.
"""

import binascii
import gc
import hashlib
import json
import os
import sys
import time

_probe_stage = "start"


def stage(name):
    global _probe_stage
    _probe_stage = name
    print("PRESTO_GATE_PHASE:" + name)


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        while True:
            block = stream.read(1024)
            if not block:
                break
            h.update(block)
    return binascii.hexlify(h.digest()).decode()


def run_probe(scratch, public_paths):
    stage("identity")
    if sys.implementation.name != "micropython" or "Presto" not in os.uname().machine:
        raise RuntimeError("wrong_board")
    if not scratch.startswith("/ai_dash_gate_") or len(scratch) != 30:
        raise ValueError("unsafe_scratch")
    try:
        os.stat(scratch)
    except OSError:
        pass
    else:
        raise RuntimeError("scratch_exists")
    result = {
        "machine": os.uname().machine,
        "version": sys.version,
        "implementation": list(sys.implementation.version),
        "memory_free_before_gc": gc.mem_free(),
        "statvfs": list(os.statvfs("/")),
        "capabilities": {},
        "public_before": {},
    }
    for path in public_paths:
        name = path.rsplit("/", 1)[-1].lower()
        if "secret" in name or name.startswith("config") or not (
            path in ("/main.py", "/ai_dash.py", "/ai_dash_files/revision.txt")
            or (path.startswith("/ai_dash_files/") and path.endswith(".py") and "/" not in path[len("/ai_dash_files/"):])
        ):
            raise ValueError("unsafe_public_path")
    stage("public_hashes")
    for path in public_paths:
        try:
            result["public_before"][path] = digest(path)
        except OSError:
            result["public_before"][path] = None
    stage("memory")
    gc.collect()
    result["memory_free_after_gc"] = gc.mem_free()
    result["sha256_vector_ok"] = binascii.hexlify(hashlib.sha256(b"abc").digest()).decode() == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    for name in ("ssl", "tls", "cryptolib", "requests", "socket", "select", "machine", "psram", "network"):
        stage("import_" + name)
        try:
            module = __import__(name)
            row = {"present": True}
            for attr in ("SSLContext", "PROTOCOL_TLS_CLIENT", "CERT_REQUIRED", "CERT_NONE", "sha256", "verify", "verify_signature", "RSA", "Ed25519", "aes", "reset", "soft_reset", "reset_cause", "WDT", "RTC", "mkramfs", "poll"):
                if hasattr(module, attr):
                    row[attr] = True
            if name in ("ssl", "tls"):
                try:
                    context = module.SSLContext(module.PROTOCOL_TLS_CLIENT)
                    row["context_methods"] = [a for a in ("load_verify_locations", "wrap_socket", "verify_mode", "check_hostname", "load_cert_chain", "get_ciphers") if hasattr(context, a)]
                    context.verify_mode = module.CERT_REQUIRED
                    row["cert_required_assignable"] = context.verify_mode == module.CERT_REQUIRED
                except Exception as exc:
                    row["context_error_type"] = type(exc).__name__
            if name == "network":
                try:
                    wlan = module.WLAN(module.STA_IF)
                    row["connected"] = wlan.isconnected()
                    row["active"] = wlan.active()
                except Exception as exc:
                    row["state_error_type"] = type(exc).__name__
            if name == "machine":
                row["rtc"] = list(module.RTC().datetime())
                row["reset_cause_value"] = module.reset_cause()
            result["capabilities"][name] = row
        except ImportError:
            result["capabilities"][name] = {"present": False}
    # Leave at least 1 MiB plus twice the bounded write size available.
    fs = result["statvfs"]
    free_bytes = (fs[1] or fs[0]) * fs[4]
    if free_bytes < 1572864:
        result["scratch_skipped"] = "insufficient_safe_headroom"
        return result
    # Only our fresh scratch directory may be modified or removed.
    stage("scratch_create")
    os.mkdir(scratch)
    created = []
    try:
        path = scratch + "/stream.bin"
        created.append(path)
        block = bytes(range(256)) * 4
        expected = hashlib.sha256()
        minimum = gc.mem_free()
        largest_write_us = 0
        started = time.ticks_us()
        stage("scratch_write")
        with open(path, "wb") as stream:
            for index in range(256):
                before = time.ticks_us()
                stream.write(block)
                largest_write_us = max(largest_write_us, time.ticks_diff(time.ticks_us(), before))
                expected.update(block)
                minimum = min(minimum, gc.mem_free())
        if hasattr(os, "sync"):
            os.sync()
        expected_hex = binascii.hexlify(expected.digest()).decode()
        result["stream_write"] = {
            "bytes": 262144,
            "chunk_bytes": 1024,
            "elapsed_us": time.ticks_diff(time.ticks_us(), started),
            "largest_write_us": largest_write_us,
            "minimum_free_bytes": minimum,
            "hash_matches": digest(path) == expected_hex,
        }
        stage("scratch_rename")
        renamed = scratch + "/renamed.bin"
        os.rename(path, renamed)
        created.remove(path)
        created.append(renamed)
        result["rename_preserved_hash"] = digest(renamed) == expected_hex
        replacement = scratch + "/replacement.bin"
        created.append(replacement)
        with open(replacement, "wb") as stream:
            stream.write(b"replacement")
        try:
            os.rename(replacement, renamed)
            created.remove(replacement)
            result["rename_over_existing"] = digest(renamed) == binascii.hexlify(hashlib.sha256(b"replacement").digest()).decode()
        except OSError as exc:
            result["rename_over_existing"] = False
            result["rename_over_existing_errno"] = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
    except Exception as exc:
        result["probe_error_type"] = type(exc).__name__
        result["failure_stage"] = _probe_stage
        if exc.args and isinstance(exc.args[0], int):
            result["failure_errno"] = exc.args[0]
    finally:
        stage("scratch_cleanup")
        for path in reversed(created):
            try:
                os.remove(path)
            except Exception as exc:
                result["cleanup_error_type"] = type(exc).__name__
        try:
            os.rmdir(scratch)
        except Exception as exc:
            result["cleanup_directory_error_type"] = type(exc).__name__
    stage("final_hashes")
    result["statvfs_after_cleanup"] = list(os.statvfs("/"))
    result["public_after"] = {}
    for path in public_paths:
        try:
            result["public_after"][path] = digest(path)
        except OSError:
            result["public_after"][path] = None
    result["public_unchanged"] = result["public_before"] == result["public_after"]
    return result

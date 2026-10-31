"""Synthetic TLS rejection and bounded download tests; no provider traffic."""

import binascii
import gc
import hashlib
import json
import machine
import network
import os
import socket
import time
import tls


def _case(settings, port, hostname, ca, path="/probe", destination=None, limit=65536, backend="tls"):
    raw = None
    wrapped = None
    began = time.ticks_ms()
    result = {"ok": False, "backend": backend}
    stage = "context"
    minimum = gc.mem_free()
    try:
        module = __import__(backend)
        context = module.SSLContext(module.PROTOCOL_TLS_CLIENT)
        context.verify_mode = module.CERT_REQUIRED
        stage = "load_ca"
        context.load_verify_locations(cadata=binascii.unhexlify(ca))
        stage = "connect"
        raw = socket.socket()
        raw.settimeout(6)
        raw.connect((settings["host"], port))
        stage = "wrap_socket"
        wrapped = context.wrap_socket(raw, server_hostname=hostname)
        stage = "request"
        wrapped.write(("GET " + path + " HTTP/1.0\r\nHost: presto-gate.invalid\r\n\r\n").encode())
        stage = "response"
        status = wrapped.readline()
        if not status.startswith(b"HTTP/1.0 200 "):
            result["reason"] = "http_status_rejected"
            return result
        length = None
        headers_bytes = len(status)
        while True:
            line = wrapped.readline()
            headers_bytes += len(line)
            if headers_bytes > 2048:
                result["reason"] = "headers_too_large"
                return result
            if line in (b"\r\n", b"\n", b""):
                break
            key, _, value = line.partition(b":")
            if key.lower() == b"content-length":
                if length is not None:
                    result["reason"] = "duplicate_length"
                    return result
                length = int(value.strip())
            if key.lower() == b"transfer-encoding":
                result["reason"] = "transfer_encoding_rejected"
                return result
        if length is None or not 0 <= length <= limit:
            result["reason"] = "length_rejected"
            return result
        h = hashlib.sha256()
        total = 0
        maximum_write_us = 0
        stream = open(destination, "wb") if destination else None
        try:
            while total < length:
                chunk = wrapped.read(min(1024, length - total))
                if not chunk:
                    result["reason"] = "truncated_body"
                    return result
                h.update(chunk)
                total += len(chunk)
                if stream:
                    tick = time.ticks_us()
                    stream.write(chunk)
                    maximum_write_us = max(maximum_write_us, time.ticks_diff(time.ticks_us(), tick))
                minimum = min(minimum, gc.mem_free())
        finally:
            if stream:
                stream.close()
        actual = binascii.hexlify(h.digest()).decode()
        result.update({"ok": True, "bytes": total, "sha256": actual,
                       "minimum_free_bytes": minimum,
                       "maximum_write_us": maximum_write_us})
        if destination and actual != settings["payload_sha256"]:
            result["ok"] = False
            result["reason"] = "hash_mismatch"
        return result
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        result["error_stage"] = stage
        # Only fixed classifications leave the board; never exception text.
        message = str(exc).lower()
        result["certificate_error"] = "cert" in message or "x509" in message or "verif" in message
        result["hostname_error"] = "hostname" in message or "host name" in message
        if exc.args and isinstance(exc.args[0], int):
            result["errno"] = exc.args[0]
        return result
    finally:
        result["elapsed_ms"] = time.ticks_diff(time.ticks_ms(), began)
        try:
            if wrapped:
                wrapped.close()
            elif raw:
                raw.close()
        except Exception:
            pass


def run_tls_probe(settings):
    if "Presto" not in os.uname().machine:
        raise RuntimeError("wrong_board")
    result = {"cases": {}, "original_rtc": list(machine.RTC().datetime()),
              "wifi_connected": network.WLAN(network.STA_IF).isconnected()}
    if not result["wifi_connected"]:
        result["blocked"] = "wifi_not_connected"
        return result
    ca = settings["ca_der_hex"]
    good = settings["valid_port"]
    name = "presto-gate.invalid"
    result["cases"]["original_clock"] = _case(settings, good, name, ca)
    result["cases"]["ssl_original_clock"] = _case(settings, good, name, ca, backend="ssl")
    machine.RTC().datetime(tuple(settings["rtc"]))
    result["test_clock"] = list(machine.RTC().datetime())
    for label, port, hostname, authority in (
        ("trusted", good, name, ca),
        ("wrong_hostname", good, "wrong-gate.invalid", ca),
        ("untrusted_ca", good, name, settings["other_ca_der_hex"]),
        ("expired", settings["expired_port"], name, ca),
        ("not_yet_valid", settings["future_port"], name, ca),
        ("missing_hostname", good, None, ca),
    ):
        gc.collect()
        result["cases"][label] = _case(settings, port, hostname, authority)
        result["cases"]["ssl_" + label] = _case(settings, port, hostname, authority, backend="ssl")
    backend = "tls" if result["cases"]["trusted"]["ok"] else "ssl"
    result["download_backend"] = backend
    scratch = settings["scratch"]
    if not scratch.startswith("/ai_dash_gate_") or len(scratch) != 30:
        raise ValueError("unsafe_scratch")
    fs = os.statvfs("/")
    if (fs[1] or fs[0]) * fs[4] < 1572864:
        result["blocked"] = "insufficient_safe_headroom"
        return result
    os.mkdir(scratch)
    created = []
    try:
        for label, path in (("stream", "/payload"), ("corrupt", "/corrupt"),
                            ("truncated", "/truncated"), ("oversized", "/oversized"),
                            ("redirect", "/redirect")):
            destination = scratch + "/" + label + ".bin"
            created.append(destination)
            gc.collect()
            result["cases"][label] = _case(settings, good, name, ca, path, destination, backend=backend)
    finally:
        for path in created:
            try:
                os.remove(path)
            except OSError:
                pass
        os.rmdir(scratch)
        # Keep calendar time close to the start value rather than leaving the
        # synthetic certificate tests' UTC setting behind as an app change.
        machine.RTC().datetime(tuple(result["original_rtc"]))
    result["scratch_removed"] = True
    prefix = "" if backend == "tls" else "ssl_"
    rejected_certificates = all(
        not result["cases"][prefix + label]["ok"]
        and result["cases"][prefix + label].get("error_type") in ("OSError", "ValueError")
        and result["cases"][prefix + label].get("error_stage") in ("wrap_socket", "request", "response")
        and result["cases"][prefix + label].get("errno") not in (110, 111, 113, 116)
        and (result["cases"][prefix + label].get("certificate_error")
             or (label == "wrong_hostname" and result["cases"][prefix + label].get("hostname_error")))
        for label in ("wrong_hostname", "untrusted_ca", "expired", "not_yet_valid"))
    result["all_security_vectors_pass"] = (
        result["cases"][prefix + "trusted"]["ok"] and result["cases"]["stream"]["ok"]
        and rejected_certificates and not result["cases"][prefix + "missing_hostname"]["ok"]
        and result["cases"]["corrupt"].get("reason") == "hash_mismatch"
        and result["cases"]["truncated"].get("reason") == "truncated_body"
        and result["cases"]["oversized"].get("reason") == "length_rejected"
        and result["cases"]["redirect"].get("reason") == "http_status_rejected")
    return result

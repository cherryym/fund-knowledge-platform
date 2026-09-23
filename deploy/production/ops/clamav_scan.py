"""Fail-closed ClamAV INSTREAM adapter. TCP endpoint stays on the private network."""
import socket
import struct


def reply(sock):
    result = bytearray()
    while b"\0" not in result:
        data = sock.recv(1024)
        if not data or len(result) + len(data) > 8192:
            raise RuntimeError("SCANNER_PROTOCOL_ERROR")
        result.extend(data)
    return bytes(result).split(b"\0", 1)[0].decode("utf-8", errors="strict")


def ping(settings):
    with socket.create_connection((settings.clamav_host, settings.clamav_port), timeout=10) as sock:
        sock.sendall(b"zPING\0")
        if reply(sock) != "PONG":
            raise RuntimeError("SCANNER_UNAVAILABLE")


def scan(*, path, filename, settings):
    if path.stat().st_size > settings.max_file_bytes:
        raise RuntimeError("SCANNER_FILE_TOO_LARGE")
    with socket.create_connection((settings.clamav_host, settings.clamav_port), timeout=10) as sock:
        # I/O limit for a scanner, not an LLM reasoning timeout.
        sock.settimeout(120)
        sock.sendall(b"zINSTREAM\0")
        total = 0
        with path.open("rb") as source:
            while chunk := source.read(65536):
                total += len(chunk)
                if total > settings.max_file_bytes:
                    raise RuntimeError("SCANNER_FILE_TOO_LARGE")
                sock.sendall(struct.pack("!I", len(chunk)) + chunk)
        sock.sendall(struct.pack("!I", 0))
        result = reply(sock)
    if result == "stream: OK":
        return {"scanner": "clamav-instream", "clean": True}
    if result.startswith("stream: ") and result.endswith(" FOUND"):
        return {"scanner": "clamav-instream", "clean": False}
    raise RuntimeError("SCANNER_PROTOCOL_ERROR")

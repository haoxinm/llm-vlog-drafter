"""multipart/form-data encoding (RFC 7578) with the standard library only."""

from __future__ import annotations

import secrets
from typing import Sequence


def _quote(value: str) -> str:
    """Header parameter value: no CR/LF, quotes and backslashes escaped."""
    return value.replace("\r", "").replace("\n", "").replace("\\", "\\\\").replace('"', '\\"')


def encode_multipart(fields: Sequence[tuple[str, str]], *, file_field: str, filename: str, content: bytes,
                     content_type: str = "application/octet-stream", boundary: str | None = None) -> tuple[bytes, str]:
    """Return (body, content-type header). Text fields come first (in the given order, repeats allowed), the file last.

    The boundary is random unless given; a boundary that occurs inside any part is never used (a fresh one is drawn; a
    caller-supplied one that collides raises ValueError).
    """
    chunks_text = [(_quote(n), v.encode("utf-8")) for n, v in fields]
    fname = _quote(filename)
    ctype = content_type.replace("\r", "").replace("\n", "")

    def clash(b: str) -> bool:
        needle = b.encode("ascii")
        return any(needle in v for _, v in chunks_text) or needle in content

    if boundary is not None:
        if clash(boundary):
            raise ValueError("the given multipart boundary occurs inside the payload")
    else:
        for _ in range(8):
            boundary = "----trip-vlog-" + secrets.token_hex(16)
            if not clash(boundary):
                break
        else:  # astronomically unlikely
            raise ValueError("could not find a multipart boundary absent from the payload")
    b = boundary.encode("ascii")
    out = bytearray()
    for name, value in chunks_text:
        out += b"--" + b + b'\r\nContent-Disposition: form-data; name="' + name.encode("utf-8") + b'"\r\n\r\n' + value + b"\r\n"
    out += (b"--" + b + b'\r\nContent-Disposition: form-data; name="' + _quote(file_field).encode("utf-8") + b'"; filename="'
            + fname.encode("utf-8") + b'"\r\nContent-Type: ' + ctype.encode("ascii", "replace") + b"\r\n\r\n" + content + b"\r\n")
    out += b"--" + b + b"--\r\n"
    return bytes(out), f"multipart/form-data; boundary={boundary}"

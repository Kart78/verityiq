"""Compressed multi-file uploads.

Hosted deployments cap a request body at about 4.5 MB, while a PBIP folder is hundreds of small text files (TMDL, JSON)
that compress about 10x. The browser packs the files into one gzip "bundle":

    gzip( <manifest JSON, one line> "\n" <bytes of file 1><bytes of file 2>... )

where the manifest is [{"p": "<relative path>", "n": <size in bytes>}, ...].
"""
from __future__ import annotations

import json
import zlib

MAX_TOTAL = 300 * 1024 * 1024        # decompressed bytes accepted in one bundle
MAX_FILES = 20000


def unpack(data: bytes, max_total: int = MAX_TOTAL) -> list[tuple[str, bytes]]:
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)          # gzip wrapper
    raw = d.decompress(data, max_total + 1)
    if len(raw) > max_total or d.unconsumed_tail:
        raise ValueError(f"Upload is larger than {max_total // (1024 * 1024)} MB once unpacked")
    raw += d.flush()
    nl = raw.find(b"\n")
    if nl < 0:
        raise ValueError("Upload bundle is damaged (no manifest)")
    try:
        manifest = json.loads(raw[:nl].decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise ValueError("Upload bundle is damaged (bad manifest)") from e
    if not isinstance(manifest, list) or len(manifest) > MAX_FILES:
        raise ValueError("Upload bundle is damaged or has too many files")
    out, pos = [], nl + 1
    for item in manifest:
        try:
            path, size = str(item["p"]), int(item["n"])
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError("Upload bundle is damaged (bad entry)") from e
        if size < 0 or pos + size > len(raw):
            raise ValueError("Upload bundle is damaged (truncated)")
        out.append((path, raw[pos:pos + size]))
        pos += size
    if pos != len(raw):
        raise ValueError("Upload bundle is damaged (extra data)")
    return out

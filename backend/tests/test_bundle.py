"""Compressed multi-file upload bundle (hosted request-size limit)."""
import gzip
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

from app.core.bundle import unpack

DEMO = Path(__file__).resolve().parents[2] / "demo" / "pbip"


def make_bundle(files):
    manifest = json.dumps([{"p": p, "n": len(b)} for p, b in files]).encode() + b"\n"
    return gzip.compress(manifest + b"".join(b for _, b in files))


def test_roundtrip_and_errors():
    files = [("a/b.tmdl", b"table X\n"), ("c.json", "{\"é\": 1}".encode()), ("empty.txt", b"")]
    assert unpack(make_bundle(files)) == files
    with pytest.raises(ValueError):
        unpack(gzip.compress(b"no manifest"))
    with pytest.raises(ValueError):                                   # declared sizes larger than the data
        unpack(gzip.compress(json.dumps([{"p": "x", "n": 99}]).encode() + b"\nabc"))
    with pytest.raises(ValueError):                                   # decompression cap (zip-bomb guard)
        unpack(gzip.compress(b"[]\n" + b"0" * 5000), max_total=1000)


def test_project_upload_as_bundle():
    from app.main import app
    files = [(p.relative_to(DEMO.parent).as_posix(), p.read_bytes()) for p in DEMO.rglob("*") if p.is_file()]
    blob = make_bundle(files)
    raw = sum(len(b) for _, b in files)
    assert len(blob) < raw / 3                                        # text compresses well
    r = TestClient(app).post("/api/projects", files={"bundle": ("upload.bundle.gz", blob, "application/octet-stream")},
                             data={"name": "Bundle upload"})
    assert r.status_code == 200, r.text
    assert r.json()["measures"] > 0

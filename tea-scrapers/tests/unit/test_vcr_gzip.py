"""Round-trip test for the custom :class:`GzipFilesystemPersister`."""

from __future__ import annotations

import sys
from pathlib import Path

from vcr.request import Request
from vcr.serializers import yamlserializer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _vcr_gzip import GzipFilesystemPersister  # noqa: E402


def _make_cassette_dict() -> dict:
    return {
        "requests": [Request(method="GET", uri="http://example.test/", body=None, headers={})],
        "responses": [
            {
                "status": {"code": 200, "message": "OK"},
                "headers": {"Content-Type": ["text/plain"]},
                "body": {"string": "hello"},
            }
        ],
    }


def test_gzip_persister_roundtrip(tmp_path: Path) -> None:
    import gzip

    cassette = _make_cassette_dict()

    gz_path = tmp_path / "fixture.yaml.gz"
    plain_path = tmp_path / "fixture.yaml"

    GzipFilesystemPersister.save_cassette(str(gz_path), cassette, yamlserializer)
    GzipFilesystemPersister.save_cassette(str(plain_path), cassette, yamlserializer)

    assert gz_path.is_file()
    assert plain_path.is_file()

    with gz_path.open("rb") as f:
        assert f.read(2) == b"\x1f\x8b"

    plain_text = plain_path.read_text(encoding="utf-8")
    with gzip.open(gz_path, "rt", encoding="utf-8") as f:
        gz_text = f.read()
    assert gz_text == plain_text

    gz_requests, gz_responses = GzipFilesystemPersister.load_cassette(str(gz_path), yamlserializer)
    plain_requests, plain_responses = GzipFilesystemPersister.load_cassette(
        str(plain_path), yamlserializer
    )

    assert gz_responses == plain_responses
    assert len(gz_requests) == len(plain_requests) == 1
    assert gz_requests[0].method == plain_requests[0].method == "GET"
    assert gz_requests[0].uri == plain_requests[0].uri == "http://example.test/"

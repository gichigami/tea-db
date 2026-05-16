"""Custom vcrpy persister that gzips cassettes when the path ends in ``.yaml.gz``."""

from __future__ import annotations

import gzip
from pathlib import Path

from vcr.persisters.filesystem import CassetteDecodeError, CassetteNotFoundError, FilesystemPersister
from vcr.serialize import deserialize, serialize


class GzipFilesystemPersister(FilesystemPersister):
    """Transparent gzip read/write for cassettes whose path ends in ``.yaml.gz``; uncompressed paths fall through to the parent."""

    @classmethod
    def load_cassette(cls, cassette_path, serializer):
        path = Path(cassette_path)
        if not str(path).endswith(".gz"):
            return super().load_cassette(cassette_path, serializer)
        if not path.is_file():
            raise CassetteNotFoundError()
        try:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                data = f.read()
        except UnicodeDecodeError as err:
            raise CassetteDecodeError("Can't read Cassette, Encoding is broken") from err
        return deserialize(data, serializer)

    @staticmethod
    def save_cassette(cassette_path, cassette_dict, serializer):
        path = Path(cassette_path)
        if not str(path).endswith(".gz"):
            return FilesystemPersister.save_cassette(cassette_path, cassette_dict, serializer)
        data = serialize(cassette_dict, serializer)
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt", encoding="utf-8") as f:
            f.write(data)

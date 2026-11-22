import asyncio

from sister import document_storage as ds


class FakeProvider:
    def __init__(self, objects):
        self.objects = objects
        self.downloads = 0

    async def exists(self, key):
        return key in self.objects

    async def download(self, key):
        self.downloads += 1
        return self.objects[key]


def _setup(monkeypatch, tmp_path, objects):
    monkeypatch.setenv("SISTER_DOCUMENTS_GCS_BUCKET", "bucket")
    monkeypatch.setenv("SISTER_DOCUMENTS_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("SISTER_DOCUMENTS_GCS_PREFIX", raising=False)
    provider = FakeProvider(objects)
    monkeypatch.setattr(ds, "_provider", provider)
    ds._exists_cache.clear()
    return provider


def test_object_key_rejects_paths():
    assert ds.object_key("a.p7m") == "documents/a.p7m"
    for bad in ("", None, "../x", "a/b", "..", "a\\b"):
        assert ds.object_key(bad) is None


def test_disabled_without_bucket(monkeypatch):
    monkeypatch.delenv("SISTER_DOCUMENTS_GCS_BUCKET", raising=False)
    monkeypatch.delenv("GCS_BUCKET", raising=False)
    assert not ds.enabled()
    assert asyncio.run(ds.ensure_local("a.p7m", "/nonexistent/a.p7m")) is None
    assert asyncio.run(ds.remote_available(["a.p7m"])) == set()


def test_downloads_once_and_prefers_disk(monkeypatch, tmp_path):
    provider = _setup(monkeypatch, tmp_path, {"documents/a.p7m": b"data"})
    path = asyncio.run(ds.ensure_local("a.p7m", str(tmp_path / "missing" / "a.p7m")))
    assert path and open(path, "rb").read() == b"data"
    assert asyncio.run(ds.ensure_local("a.p7m", str(tmp_path / "missing" / "a.p7m"))) == path
    assert provider.downloads == 1
    disk = tmp_path / "b.pdf"
    disk.write_bytes(b"x")
    assert asyncio.run(ds.ensure_local("b.pdf", str(disk))) == str(disk)


def test_missing_object_and_availability(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, {"documents/a.p7m": b"d"})
    assert asyncio.run(ds.ensure_local("zzz.p7m", "/nope")) is None
    assert asyncio.run(ds.remote_available(["a.p7m", "zzz.p7m"])) == {"a.p7m"}

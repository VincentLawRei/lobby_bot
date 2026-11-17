"""Карта матча на сайте: файлы в WEB_ROOT/map/data, список матчей (новые первыми, без повторов, не больше
MAP_INDEX_LIMIT), запись списка через временный файл; при сбое ссылка не возвращается."""

import io
import json
import posixpath
from contextlib import contextmanager

import pytest

from lobby_bot.features.match_stats import site
from lobby_bot.settings import settings


class FakeFile(io.StringIO):
    def __init__(self, sftp, path, mode):
        super().__init__(sftp.files[path].decode("utf-8") if "r" in mode else "")
        self.sftp, self.path, self.mode = sftp, path, mode

    def read(self, *args):
        return super().read(*args).encode("utf-8")

    def __exit__(self, *exc):
        if "w" in self.mode:
            self.sftp.files[self.path] = self.getvalue().encode("utf-8")
        return super().__exit__(*exc)


class FakeSftp:
    """SFTP в памяти: файлы и каталоги по полному пути."""

    def __init__(self):
        self.files, self.dirs, self.uploaded = {}, set(), {}

    def stat(self, path):
        if path not in self.dirs:
            raise OSError(path)

    def mkdir(self, path):
        self.dirs.add(path)

    def put(self, local, remote):
        with open(local, "rb") as f:
            self.files[remote] = f.read()
        self.uploaded[remote] = local

    def open(self, path, mode):
        if "r" in mode and path not in self.files:
            raise OSError(path)
        return FakeFile(self, path, mode)

    def remove(self, path):
        if path not in self.files:
            raise OSError(path)
        del self.files[path]

    def rename(self, old, new):
        self.files[new] = self.files.pop(old)


@pytest.fixture
def sftp(monkeypatch):
    fake = FakeSftp()

    @contextmanager
    def site_sftp():
        yield fake

    monkeypatch.setattr(site, "site_sftp", site_sftp)
    return fake


@pytest.fixture
def match_files(tmp_path):
    positions = tmp_path / "M1_pos.json"
    positions.write_text("[]", encoding="utf-8")
    damage = tmp_path / "M1.json"
    damage.write_text(
        json.dumps({"ended_at": "2026-10-01T12:00:00", "winner": "Traitors", "players": {"a": {}, "b": {}}}),
        encoding="utf-8",
    )
    return str(positions), str(damage)


def index(sftp):
    return json.loads(sftp.files[posixpath.join(site.map_data_dir(), "matches.json")])


def test_upload_with_damage(sftp, match_files):
    positions, damage = match_files

    url = site.upload_match_map("M1", damage, positions, "Outpost")

    data_dir = site.map_data_dir()
    assert url == f"{settings.site_url}/map/?m=M1"
    assert set(sftp.uploaded) == {f"{data_dir}/M1_pos.json", f"{data_dir}/M1.json"}
    assert "/" + data_dir.strip("/") in sftp.dirs  # каталоги создаются от корня
    assert index(sftp) == [
        {"match_id": "M1", "map": "Outpost", "ended_at": "2026-10-01T12:00:00", "winner": "Traitors", "players": 2}
    ]
    assert f"{data_dir}/matches.json.tmp" not in sftp.files  # временный файл переименован


def test_index_newest_first_without_duplicates_and_limited(sftp, match_files, monkeypatch):
    positions, _ = match_files
    monkeypatch.setattr(site, "MAP_INDEX_LIMIT", 3)
    for match_id in ("A", "B", "C", "A", "D"):
        site.upload_match_map(match_id, None, positions, None)

    assert [m["match_id"] for m in index(sftp)] == ["D", "A", "C"]


def test_broken_index_and_damage_are_replaced(sftp, match_files, tmp_path):
    positions, _ = match_files
    sftp.files[posixpath.join(site.map_data_dir(), "matches.json")] = b"{not json"
    broken_damage = tmp_path / "bad.json"
    broken_damage.write_text("[1, 2", encoding="utf-8")

    site.upload_match_map("M1", str(broken_damage), positions, None)

    assert index(sftp) == [{"match_id": "M1"}]


def test_failure_returns_no_link(monkeypatch, match_files):
    @contextmanager
    def unreachable():
        raise OSError("сайт недоступен")
        yield

    monkeypatch.setattr(site, "site_sftp", unreachable)
    positions, damage = match_files
    assert site.upload_match_map("M1", damage, positions, None) is None

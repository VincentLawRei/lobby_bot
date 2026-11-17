"""Фейковый SSH (вместо paramiko), замороженное время, снимок базы и общие фикстуры сценариев серверов."""

import asyncio
from datetime import datetime

import paramiko
import pytest

from tests import mongo as db

NOW = datetime(2026, 10, 20, 12, 0, 30, 123456)
COLLECTIONS = (
    'seasons_col',
    'player_ratings_col',
    'history_player_stats_col',
    'discord_hosts_notify_col',
    'discord_hosts_presets_col',
    'discord_panel_col',
    'game_matches_col',
    'console_access_col',
    'elo_overrides_col',
)


class FakeStream:
    def __init__(self, text: str):
        self._text = text

    def read(self):
        return self._text.encode('utf-8')


class FakeRemoteFile:
    def __init__(self, sftp, path, mode):
        self.sftp, self.path, self.mode = sftp, path, mode

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.sftp.ssh.world.files[self.path].encode('utf-8')

    def write(self, text):
        self.sftp.ssh.world.log.append(('write', self.path, text))


class FakeSFTP:
    def __init__(self, ssh):
        self.ssh = ssh

    def put(self, local, remote):
        with open(local, encoding='utf-8') as f:
            self.ssh.world.log.append(('put', remote, f.read()))

    def open(self, path, mode):
        if path not in self.ssh.world.files and mode == 'r':
            raise FileNotFoundError(path)
        return FakeRemoteFile(self, path, mode)

    def close(self):
        pass


class FakeSSH:
    """paramiko.SSHClient: записывает подключения и команды, отвечает по сценарию world.responses."""

    def __init__(self, world):
        self.world = world

    def set_missing_host_key_policy(self, policy):
        pass

    def connect(self, **kwargs):
        self.host = kwargs['hostname']
        self.world.log.append(('connect', kwargs['hostname'], tuple(sorted(kwargs.items()))))
        if self.world.fail_connect:
            raise OSError("connection refused")

    def exec_command(self, command):
        self.world.log.append(('exec', command))
        # Ответ: первая запись, чья подстрока есть в команде (третий элемент, если есть: только для этого хоста)
        out = next(
            (
                text
                for needle, text, *host in self.world.responses
                if needle in command and (not host or host[0] == self.host)
            ),
            '',
        )
        return None, FakeStream(out), FakeStream('')

    def open_sftp(self):
        return FakeSFTP(self)

    def close(self):
        pass


class World:
    def __init__(self):
        self.log = []
        self.responses = []  # [(подстрока команды, stdout[, hostname])]
        self.files = {}  # файлы на сервере для чтения через SFTP
        self.fail_connect = False


@pytest.fixture
def ssh(monkeypatch):
    world = World()
    monkeypatch.setattr(paramiko, "SSHClient", lambda: FakeSSH(world))
    return world


@pytest.fixture
def frozen(monkeypatch):
    """Одно «сейчас» для правил серверов (service._now)."""
    from lobby_bot.features.servers import service

    monkeypatch.setattr(service, "_now", lambda: NOW)
    return NOW


def clean_db():
    for name in COLLECTIONS:
        getattr(db, name).delete_many({})


def snapshot() -> dict:
    return {
        name: sorted(
            ({k: v for k, v in d.items() if k != '_id' or isinstance(v, str)} for d in getattr(db, name).find()),
            key=repr,
        )
        for name in COLLECTIONS
    }


def set_disabled(servers: set):
    from lobby_bot.features.servers import service

    service.DISABLED_SERVERS.clear()
    service.DISABLED_SERVERS.update(servers)


@pytest.fixture
def env(monkeypatch, ssh, frozen):
    """Мгновенный asyncio.sleep, все серверы включены."""
    from lobby_bot.features.servers import service

    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda *_a, **_k: real_sleep(0))
    saved = set(service.DISABLED_SERVERS)
    set_disabled(set())
    yield ssh
    set_disabled(saved)


SCENARIO = {}  # тест и счётчик для имён эталонов run_scenario (в одном тесте сценариев может быть несколько)


@pytest.fixture(autouse=True)
def scenario_snapshots(request):
    SCENARIO.update(request=request, count=0)

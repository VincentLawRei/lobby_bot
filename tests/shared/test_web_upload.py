"""Загрузка на сайт по SFTP: параметры подключения из настроек, путь от WEB_ROOT, соединение закрывается всегда."""

from typing import ClassVar

import pytest

from lobby_bot.settings import settings
from lobby_bot.shared import web_upload


class FakeSftp:
    def __init__(self):
        self.files = {}

    def putfo(self, fileobj, path):
        self.files[path] = fileobj.read()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeSsh:
    instances: ClassVar[list["FakeSsh"]] = []

    def __init__(self):
        self.sftp = FakeSftp()
        self.connected = None
        self.closed = False
        FakeSsh.instances.append(self)

    def set_missing_host_key_policy(self, policy):
        pass

    def connect(self, host, **kwargs):
        self.connected = (host, kwargs)

    def open_sftp(self):
        return self.sftp

    def close(self):
        self.closed = True


@pytest.fixture
def ssh(monkeypatch):
    FakeSsh.instances.clear()
    monkeypatch.setattr(web_upload.paramiko, "SSHClient", FakeSsh)
    monkeypatch.setattr(web_upload.paramiko.Ed25519Key, "from_private_key_file", lambda path: f"key:{path}")
    return FakeSsh.instances


def test_upload_writes_under_web_root(ssh):
    web_upload.upload_to_site("graphs/full.html", b"<html>")

    [client] = ssh
    assert client.sftp.files == {f"{settings.web_root}/graphs/full.html": b"<html>"}
    host, kwargs = client.connected
    assert (host, kwargs["username"], kwargs["pkey"]) == (
        settings.web_host,
        settings.web_user,
        f"key:{settings.web_ssh_key_path}",
    )
    assert client.closed


def test_connection_closed_on_error(ssh):
    with pytest.raises(RuntimeError), web_upload.site_sftp():
        raise RuntimeError("сбой во время загрузки")
    assert ssh[0].closed

"""Файлы на веб-сервере сайта по SFTP (WEB_HOST, WEB_USER, WEB_SSH_KEY_PATH, WEB_ROOT).

Функции блокирующие (paramiko): из async-кода вызывать через `asyncio.to_thread`.
"""

import io
import posixpath
from collections.abc import Iterator
from contextlib import contextmanager

import paramiko

from lobby_bot.settings import settings


@contextmanager
def site_sftp() -> Iterator[paramiko.SFTPClient]:
    """SFTP-сессия к веб-серверу; соединение закрывается при выходе из with."""
    key = paramiko.Ed25519Key.from_private_key_file(settings.web_ssh_key_path)
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(settings.web_host, username=settings.web_user, pkey=key, timeout=15)
    try:
        with ssh.open_sftp() as sftp:
            yield sftp
    finally:
        ssh.close()


def site_path(*parts: str) -> str:
    """Путь на веб-сервере относительно WEB_ROOT."""
    return posixpath.join(settings.web_root, *parts)


def upload_to_site(relative_path: str, data: bytes) -> None:
    """Записать data в WEB_ROOT/relative_path на веб-сервере."""
    with site_sftp() as sftp:
        sftp.putfo(io.BytesIO(data), site_path(relative_path))

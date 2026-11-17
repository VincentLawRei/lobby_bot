"""Игровые серверы по SSH/SFTP (paramiko). Синхронные функции: из async-кода вызывать в потоке.

Подключение каждый раз новое, таймауты 5 секунд, ключ и адреса из SSH_CONFIGS (из .env).
"""

import json
import logging
import os
import tempfile

import paramiko

from lobby_bot.settings import settings

logger = logging.getLogger(__name__)

SSH_TIMEOUTS = {'timeout': 5, 'banner_timeout': 5, 'auth_timeout': 5}


def _ssh_config(host: str, user: str) -> dict:
    return {
        'hostname': host,
        'username': user,
        'key_filename': settings.game_ssh_key_path,
        'look_for_keys': False,
        'allow_agent': False,
    }


# Номер сервера -> параметры paramiko.SSHClient.connect
SSH_CONFIGS = {
    0: _ssh_config(settings.game_server_1_host, settings.game_server_1_user),  # Сервер 1 - Франкфурт
    1: _ssh_config(settings.game_server_2_host, settings.game_server_2_user),  # Сервер 2 - Франкфурт 2
    2: _ssh_config(settings.game_server_3_host, settings.game_server_3_user),  # Сервер 3 - Франкфурт 3
}


def _ssh_configs() -> dict:
    return SSH_CONFIGS


def connect(server_num: int) -> paramiko.SSHClient:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(**_ssh_configs()[server_num], **SSH_TIMEOUTS)
    return ssh


def execute_ssh_command(command: str, server_num: int) -> tuple[str, str]:
    """Выполнить команду на сервере, вернуть (stdout, stderr)."""
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(**_ssh_configs()[server_num], **SSH_TIMEOUTS)
        _stdin, stdout, stderr = ssh.exec_command(command)
        return stdout.read().decode('utf-8'), stderr.read().decode('utf-8')
    finally:
        ssh.close()


def upload_json(server_num: int, data, remote_path: str) -> None:
    """Записать data в JSON (UTF-8, без экранирования кириллицы) и загрузить на сервер по SFTP."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False, encoding='utf-8') as tmp:
        json.dump(data, tmp, ensure_ascii=False)
        tmp_path = tmp.name
    ssh = connect(server_num)
    sftp = ssh.open_sftp()
    sftp.put(tmp_path, remote_path)
    sftp.close()
    ssh.close()
    os.unlink(tmp_path)


def patch_remote_template(server_num: int, template_path: str, output_path: str, placeholder: str, value: str) -> None:
    """Прочитать шаблон на сервере, заменить placeholder на value и записать в output_path."""
    ssh = connect(server_num)
    sftp = ssh.open_sftp()
    with sftp.open(template_path, "r") as f:
        template = f.read().decode("utf-8")
    with sftp.open(output_path, "w") as f:
        f.write(template.replace(placeholder, value))
    sftp.close()
    ssh.close()

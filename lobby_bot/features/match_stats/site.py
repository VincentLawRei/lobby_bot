"""Карта матча на сайте (SITE_URL/map): файлы позиций и урона в WEB_ROOT/map/data и список
последних матчей data/matches.json. Блокирующий код (SFTP): вызывать из потока."""

import contextlib
import json
import logging
import posixpath

from lobby_bot.settings import settings
from lobby_bot.shared.web_upload import site_path, site_sftp

logger = logging.getLogger(__name__)

MAP_INDEX_LIMIT = 300  # сколько матчей держать в списке на сайте


def map_data_dir() -> str:
    return posixpath.join(site_path("map"), "data")


def map_url(match_id: str) -> str:
    return f"{settings.site_url}/map/?m={match_id}"


def upload_match_map(match_id: str, damage_path: str | None, positions_path: str, map_name: str | None) -> str | None:
    """Выложить <id>_pos.json (и <id>.json, если есть) и добавить матч в начало списка.
    Возвращает ссылку на карту матча или None, если загрузить не удалось."""
    try:
        with site_sftp() as sftp:
            data_dir = map_data_dir()
            _mkdirs(sftp, data_dir)
            sftp.put(positions_path, posixpath.join(data_dir, f"{match_id}_pos.json"))

            meta = {"match_id": match_id}
            if map_name:
                meta["map"] = map_name
            if damage_path:
                sftp.put(damage_path, posixpath.join(data_dir, f"{match_id}.json"))
                meta.update(_damage_meta(damage_path))

            index_path = posixpath.join(data_dir, "matches.json")
            index = _read_json(sftp, index_path, [])
            if not isinstance(index, list):
                index = []
            index = [x for x in index if isinstance(x, dict) and x.get("match_id") != match_id]
            index.insert(0, meta)
            _write_json(sftp, index_path, index[:MAP_INDEX_LIMIT])
        return map_url(match_id)
    except Exception:
        logger.exception("Не удалось выложить карту матча %s", match_id)
        return None


def _damage_meta(path: str) -> dict:
    """Итог матча для списка на сайте; если JSON не читается, без него."""
    try:
        with open(path, encoding="utf-8") as f:
            match = json.load(f)
        return {
            "ended_at": match.get("ended_at"),
            "winner": match.get("winner"),
            "players": len(match.get("players", {})),
        }
    except (OSError, ValueError, AttributeError, TypeError):
        return {}


def _mkdirs(sftp, path: str) -> None:
    current = ""
    for part in path.strip("/").split("/"):
        current += "/" + part
        try:
            sftp.stat(current)
        except OSError:
            sftp.mkdir(current)


def _read_json(sftp, path: str, default):
    try:
        with sftp.open(path, "r") as f:
            return json.loads(f.read().decode("utf-8"))
    except Exception:
        return default


def _write_json(sftp, path: str, obj) -> None:
    """Через временный файл, чтобы сайт не прочитал недописанный JSON."""
    tmp = path + ".tmp"
    with sftp.open(tmp, "w") as f:
        f.write(json.dumps(obj, ensure_ascii=False))
    with contextlib.suppress(OSError):
        sftp.remove(path)
    sftp.rename(tmp, path)

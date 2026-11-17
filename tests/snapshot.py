"""Snapshot-тесты: результат сравнивается с эталоном в `snapshots/<модуль теста>.json` рядом с файлом теста
(один файл на модуль, ключ: имя теста).

Эталоны фиксируют поведение (первые записаны при переносе старого кода, когда новый с ним совпадал). Если вывод
меняется намеренно, перезаписать эталоны: `UPDATE_SNAPSHOTS=1 pytest <тесты>` и проверить diff эталонов.
Отсутствующий эталон тест не создаёт сам: иначе новый тест прошёл бы, ничего не проверив.
"""

import json
import os
from pathlib import Path


def _dump(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True, default=repr) + "\n"


def assert_snapshot(request, value, name: str | None = None) -> None:
    path = Path(request.path).parent / "snapshots" / f"{Path(request.path).stem}.json"
    key = name or request.node.name
    actual = json.loads(_dump(value))  # кортежи в списки, прочее через repr: как в файле эталонов
    stored = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if os.environ.get("UPDATE_SNAPSHOTS"):
        stored[key] = actual
        path.parent.mkdir(exist_ok=True)
        path.write_text(_dump(stored), encoding="utf-8")
        return
    assert key in stored, f"Нет эталона {key} в {path.name}: создать через UPDATE_SNAPSHOTS=1"
    assert actual == stored[key], f"Вывод отличается от эталона {key} в {path.name}"

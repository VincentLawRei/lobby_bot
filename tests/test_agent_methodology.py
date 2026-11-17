"""Агенты, команды и skills в .claude/ (см. .claude/README.md) согласованы сами с собой.

Claude Code молча пропускает агента или skill с битым frontmatter, а команда со ссылкой
на несуществующий файл ломается только при вызове. Тест ловит это до выкатки.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CLAUDE_DIR = ROOT / ".claude"

AGENT_FILES = sorted((CLAUDE_DIR / "agents").glob("*.md"))
COMMAND_FILES = sorted((CLAUDE_DIR / "commands").glob("*.md"))
SKILL_FILES = sorted((CLAUDE_DIR / "skills").glob("*/SKILL.md"))
ALL_FILES = AGENT_FILES + COMMAND_FILES + SKILL_FILES


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def frontmatter(path: Path) -> dict[str, str]:
    """Плоский frontmatter `ключ: значение`. Значение с ": " обязано быть в кавычках."""
    text = _text(path)
    assert text.startswith("---\n"), f"{path}: нет frontmatter"
    head, _, _ = text[4:].partition("\n---\n")
    fields = {}
    for line in head.splitlines():
        key, sep, value = line.partition(": ")
        assert sep, f"{path}: строка frontmatter без 'ключ: значение': {line!r}"
        quoted = len(value) >= 2 and value[0] == value[-1] == '"'
        assert quoted or ": " not in value, f"{path}: значение {key} с ': ' должно быть в кавычках"
        fields[key] = value[1:-1] if quoted else value
    return fields


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def test_methodology_is_not_empty():
    assert AGENT_FILES and COMMAND_FILES and SKILL_FILES


@pytest.mark.parametrize("path", AGENT_FILES, ids=rel)
def test_agent_frontmatter(path):
    fields = frontmatter(path)
    assert fields.get("name") == path.stem, "имя агента должно совпадать с именем файла"
    assert fields.get("description")
    assert fields.get("tools")


@pytest.mark.parametrize("path", SKILL_FILES, ids=rel)
def test_skill_frontmatter(path):
    fields = frontmatter(path)
    assert fields.get("name") == path.parent.name, "имя skill должно совпадать с именем папки"
    assert fields.get("description")


@pytest.mark.parametrize("path", COMMAND_FILES, ids=rel)
def test_command_frontmatter(path):
    assert frontmatter(path).get("description")


@pytest.mark.parametrize("path", [*ALL_FILES, CLAUDE_DIR / "README.md"], ids=rel)
def test_referenced_methodology_files_exist(path):
    """Пути внутри .claude, упомянутые в тексте, указывают на существующие файлы."""
    for ref in re.findall(r"`(\.claude/[^`\s]+)`", _text(path)):
        assert (ROOT / ref).exists(), f"{rel(path)} ссылается на несуществующий {ref}"

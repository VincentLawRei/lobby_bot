"""Репорты на игроков: причины, сохранение, поиск игрока (без discord).

Репорт подают командой /report (фича reports) или кнопкой под итогами матча (фича match_results, с match_id).
"""

from datetime import UTC, datetime

from .repo import ReportsRepo

REPORT_REASONS = ["Читы", "Консоль", "Негативное поведение", "Отсутствие тумана", "По рофлу", "Троллинг в игре"]


def now_utc() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def target_name(player: dict, steam_id: str) -> str:
    """Имя для репорта: «ник из игры (отображаемый ник)», если отображаемый отличается."""
    current_name = player.get("current_name", steam_id)
    changed = player.get("changed_nickname")
    return f"{current_name} ({changed})" if changed and changed != current_name else current_name


async def save_report(
    repo: ReportsRepo,
    reporter_id: str,
    reporter_name: str,
    anonymous: bool,
    target_steam_id: str,
    target_name: str,
    reasons: list[str],
    match_id: str | None = None,
) -> dict:
    """Сохранить репорт (status=open). match_id — если репорт из итогов матча."""
    doc = {
        "reporter_id": reporter_id,
        "reporter_name": reporter_name,
        "anonymous": anonymous,
        "target_steam_id": target_steam_id,
        "target_name": target_name,
        "reasons": reasons,
    }
    if match_id is not None:
        doc["match_id"] = match_id
    doc.update({"created_at": now_utc(), "status": "open"})
    await repo.save(doc)
    return doc

"""Простые фейки Discord, общие для тестов разных фич."""

from types import SimpleNamespace

from lobby_bot.settings import settings


def user(user_id: int = 10, role_ids: tuple[int, ...] = (), bot: bool = False) -> SimpleNamespace:
    """Участник сервера с ролями по ID."""
    return SimpleNamespace(id=user_id, bot=bot, roles=[SimpleNamespace(id=r) for r in role_ids])


def moderator() -> SimpleNamespace:
    return user(20, role_ids=(settings.moderator_role_id,))


class FakeContext:
    """Контекст !-команды: автор и ответы бота."""

    def __init__(self, author=None, guild=None) -> None:
        self.author = author or moderator()
        self.guild = guild
        self.command = SimpleNamespace(name="fake")
        self.replies: list[str] = []

    async def send(self, text: str) -> None:
        self.replies.append(text)

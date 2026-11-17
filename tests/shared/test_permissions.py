from types import SimpleNamespace

import pytest

from lobby_bot.settings import settings
from lobby_bot.shared.permissions import NotModerator, is_moderator, moderator_check
from tests.fakes import FakeContext, moderator, user


def test_moderator_role_allowed():
    assert is_moderator(moderator())


def test_admin_id_allowed_without_role():
    assert is_moderator(user(settings.admin_ids[0]))


def test_regular_member_denied():
    assert not is_moderator(user(10, role_ids=(12345,)))


def test_user_without_roles_in_dm_denied():
    assert not is_moderator(SimpleNamespace(id=10))  # в ЛС автор без ролей


def test_check_raises_for_regular_member():
    with pytest.raises(NotModerator):
        moderator_check(FakeContext(author=user(10)))
    assert moderator_check(FakeContext(author=moderator()))

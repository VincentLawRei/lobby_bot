"""Прогон матчей расчётом ELO в базе в памяти (mongomock): строки матчей и стартовые флаги игроков
(штрафы, твинки) на входе, снимок всего, что расчёт пишет в базу (без служебных полей _id и времени записи).
"""

from datetime import datetime

import mongomock

from lobby_bot.features.elo import service
from lobby_bot.features.elo.repo import EloRepo
from tests.async_mongo import wrap

# Поля, которые расчёт ставит текущим временем или базой: в эталон не попадают
VOLATILE = {'_id', 'created_at', 'updated_at', 'processed_at', 'linked_at', 'penalized_at'}
# Поля рейтинга, которые задают модераторы (переносятся из исходных данных)
FLAG_FIELDS = ('win_multiplier', 'loss_multiplier', 'is_twink', 'linked_to')
# Поля, которые пишет сам расчёт ELO в строки матча (при подготовке данных убираются)
MATCH_ELO_FIELDS = ('elo_before', 'elo_after', 'elo_change', 'twink_redirected_to', 'season')


def new_database():
    return mongomock.MongoClient().db


def seed(database, stats_rows: list[dict], flags: dict[str, dict], season: int) -> None:
    """Строки матчей и рейтинги с флагами модераторов (рейтинг создаётся как новый игрок)."""
    rows = [{k: v for k, v in r.items() if k != '_id' and k not in MATCH_ELO_FIELDS} for r in stats_rows]
    if rows:
        database.history_player_stats.insert_many(rows)
    names = {r['steam_id']: (r.get('game_uid'), r.get('name')) for r in rows}
    for steam_id, player_flags in flags.items():
        game_id, name = names.get(steam_id, (None, steam_id))
        doc = service.new_rating(steam_id, game_id, name, season, datetime(2000, 1, 1))
        doc.update(player_flags)
        database.player_ratings.insert_one(doc)


def make_repo(database) -> EloRepo:
    return EloRepo(
        *(
            wrap(c)
            for c in (
                database.player_ratings,
                database.history_player_stats,
                database.elo_history,
                database.matches,
                database.elo_processing,
                database.elo_overrides,
            )
        )
    )


async def run_new(database, match_uuids: list[str], season: int) -> list[bool]:
    repo = make_repo(database)
    return [await service.process_match_elo(repo, uuid, season) for uuid in match_uuids]


def _clean(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if k not in VOLATILE}


def snapshot(database) -> dict:
    return {
        'ratings': {d['steam_id']: _clean(d) for d in database.player_ratings.find()},
        'elo_history': [_clean(d) for d in database.elo_history.find()],
        'stats': {(d['match_uuid'], d['steam_id']): _clean(d) for d in database.history_player_stats.find()},
        'matches': {d['match_uuid']: _clean(d) for d in database.matches.find()},
    }


def plain(snap: dict) -> dict:
    """Снимок для эталона: ключи строк матча (match_uuid, steam_id) строкой."""
    return {**snap, 'stats': {f"{k[0]}|{k[1]}": v for k, v in snap['stats'].items()}}


def diff(old: dict, new: dict, limit: int = 20) -> list[str]:
    """Расхождения снимков: пустой список, если расчёты совпали."""
    problems = []
    for section in ('ratings', 'stats', 'matches'):
        for key in sorted(set(old[section]) | set(new[section]), key=str):
            a, b = old[section].get(key), new[section].get(key)
            if a != b:
                fields = sorted(k for k in set(a or {}) | set(b or {}) if (a or {}).get(k) != (b or {}).get(k))
                problems.append(
                    f"{section} {key}: "
                    + ", ".join(f"{f}: {(a or {}).get(f)!r} != {(b or {}).get(f)!r}" for f in fields)
                )
    if old['elo_history'] != new['elo_history']:
        for i, (a, b) in enumerate(zip(old['elo_history'], new['elo_history'], strict=False)):
            if a != b:
                problems.append(f"elo_history #{i}: {a} != {b}")
        if len(old['elo_history']) != len(new['elo_history']):
            problems.append(f"elo_history: {len(old['elo_history'])} записей != {len(new['elo_history'])}")
    return problems[:limit]

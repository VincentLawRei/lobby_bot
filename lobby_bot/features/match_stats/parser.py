"""Разбор файлов матча от игровых серверов. Чистые функции: без БД, сети и Discord.

- `<UUID>.csv` (пишет игра): строки 1-2 матч, строка 4 заголовок игроков, дальше игроки;
- `<match_id>.json` (Node.js-скрипт на сервере): игроки и урон;
- `<match_id>_pos.json`: позиции игроков для карты матча на сайте (здесь не разбирается).
"""

import csv
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

CSV_ENCODINGS = ['utf-8-sig', 'utf-16', 'utf-16-le', 'cp1251', 'latin-1']
MIN_VALID_DURATION = 300  # матч короче 5 минут не считается
MATCH_PLAYERS = 8  # и матч не на 8 игроков тоже
MAP_NAME_OVERRIDES = {'station': 'harbor'}  # внутриигровое имя карты -> имя на сайте


# ---------- CSV ----------
def read_lines(path: str) -> list[str] | None:
    """Строки CSV: игра пишет то UTF-16, то однобайтные кодировки."""
    for encoding in CSV_ENCODINGS:
        try:
            with open(path, encoding=encoding) as f:
                return f.readlines()
        except (UnicodeDecodeError, UnicodeError):
            continue
    return None


def parse_csv_file(path: str) -> tuple[dict | None, list[dict] | None]:
    """(поля матча из заголовка CSV, игроки) или (None, None), если файл неполный."""
    lines = read_lines(path)
    if not lines or len(lines) < 5:
        return None, None
    match_data = next(csv.DictReader([lines[0].strip(), lines[1].strip()]))
    players = []
    for line in lines[4:]:
        if not line.strip():
            continue
        parts = line.strip().split(',')
        if len(parts) >= 10:
            players.append(
                {
                    'platform': parts[0].strip('"'),
                    'steam_id': parts[1].strip(),
                    'game_uid': parts[2].strip(),
                    'team': parts[3].strip(),
                    'role': parts[4].strip().replace('EPlayerTeamRole::PTR_', ''),
                    'name': parts[5].strip('"'),
                    'rating': parts[6].strip(),
                    'deaths': int(parts[7]) if parts[7].isdigit() else 0,
                }
            )
    return match_data, players


def parse_duration_to_seconds(duration: str | None) -> int:
    """'+00:23:03.000' -> 1383; непонятный формат -> 0."""
    try:
        h, m, s = map(int, duration.replace('+', '').split('.')[0].split(':'))
        return h * 3600 + m * 60 + s
    except (AttributeError, ValueError):
        return 0


def parse_finish_time(match_data: dict) -> datetime | None:
    try:
        return datetime.strptime(match_data.get('FinishTime'), '%Y.%m.%d-%H.%M.%S')
    except (TypeError, ValueError):
        return None


def parse_escape_progress(match_data: dict) -> int:
    try:
        return int(float(match_data.get('Escape Progress', '0 %').replace('%', '').strip()))
    except (AttributeError, ValueError):
        return 0


def is_valid_match(match_data: dict, players_count: int) -> bool:
    """Валидный матч идёт в статистику и ELO: не короче 5 минут и на 8 игроков."""
    duration = parse_duration_to_seconds(match_data.get('Duration', '+00:00:00.000'))
    return duration >= MIN_VALID_DURATION and players_count >= MATCH_PLAYERS


def player_stat_docs(match_data: dict, players: list[dict], is_ranked: bool, season: int, now: datetime) -> list[dict]:
    """Документы history_player_stats: по одному на игрока матча."""
    outcome = match_data.get('Outcome', '')
    duration = match_data.get('Duration', '+00:00:00.000')
    common = {
        'map': match_data.get('Map'),
        'match_date': match_data.get('FinishTime'),
        'match_datetime': parse_finish_time(match_data),
        'match_duration': match_data.get('Duration'),
        'match_duration_seconds': parse_duration_to_seconds(duration),
        'match_outcome': outcome,
        'escape_progress': parse_escape_progress(match_data),
        'players_count': len(players),
        'is_ranked': is_ranked,
        'is_valid_match': is_valid_match(match_data, len(players)),
        'season': season,
        'imported_at': now,
    }
    return [
        {
            'match_uuid': match_data.get('UUID'),
            'steam_id': p['steam_id'],
            'game_uid': p['game_uid'],
            'name': p['name'],
            'team': p['team'],
            'role': p['role'],
            'won': p['team'] in outcome,  # Outcome вида "Traitors Won"
            'deaths': p['deaths'],
            'rating': p['rating'],
            **common,
        }
        for p in players
    ]


# ---------- JSON урона ----------
def _parse_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace('Z', '+00:00')) if value else None


def damage_doc(data: dict, now: datetime) -> dict:
    """Документ match_damage: урон каждого игрока по предателям, по экипажу и по оружию."""
    players = data.get('players', {})
    damage = data.get('damage', {})  # damage[атакующий][жертва][оружие] = урон
    traitors = {sid for sid, p in players.items() if p.get('isTraitor', False)}
    crew = {sid for sid, p in players.items() if not p.get('isTraitor', False)}

    totals: dict[str, dict] = {}
    for attacker, victims in damage.items():
        t = totals.setdefault(attacker, {'damage_to_traitors': 0, 'damage_to_crew': 0, 'damage_by_weapons': {}})
        for victim, weapons in victims.items():
            for weapon, amount in weapons.items():
                t['damage_by_weapons'][weapon] = t['damage_by_weapons'].get(weapon, 0) + amount
                if victim in traitors:
                    t['damage_to_traitors'] += amount
                elif victim in crew:
                    t['damage_to_crew'] += amount

    try:
        started_at, ended_at = _parse_iso(data.get('started_at')), _parse_iso(data.get('ended_at'))
    except (AttributeError, ValueError):
        started_at = ended_at = None

    enriched = {}
    for steam_id, p in players.items():
        t = totals.get(steam_id, {'damage_to_traitors': 0, 'damage_to_crew': 0, 'damage_by_weapons': {}})
        enriched[steam_id] = {
            'name': p.get('name', steam_id),
            'role': p.get('role', ''),
            'isTraitor': p.get('isTraitor', False),
            'damage_to_traitors': t['damage_to_traitors'],
            'damage_to_crew': t['damage_to_crew'],
            'damage_by_weapons': t['damage_by_weapons'],
        }
    return {
        'match_id': data['match_id'],
        'started_at': started_at,
        'ended_at': ended_at,
        'winner': data.get('winner', ''),
        'players': enriched,
        'raw_damage': damage,
        'imported_at': now,
    }


def normalize_map(raw: str | None) -> str | None:
    """Название карты в нижнем регистре, с заменой внутриигровых имён на имена сайта."""
    if not raw:
        return None
    key = raw.strip().lower()
    return MAP_NAME_OVERRIDES.get(key, key)

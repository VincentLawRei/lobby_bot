"""Итоги матчей: урон, детальная статистика игрока, имена и данные для уведомлений (без discord).

Тексты урона и статистики — чистые функции от документа match_damage (`players`, `raw_damage`)
и имён игроков. Данные матча читают async-функции, репозиторий передаёт вызывающий (MatchResultsCog).
"""

import logging
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import UTC, datetime

from lobby_bot.features.reports import service as reports_service
from lobby_bot.features.reports.repo import ReportsRepo

from .repo import MatchResultsRepo

logger = logging.getLogger(__name__)

# Не игроки: источники урона в match_damage
NON_PLAYER_IDS = {'wildlife', 'Unknown'}
NON_PLAYER_IDS_MENU = {'wildlife', 'Unknown', 'HOMELANDER'}  # так было в меню статистики


def now_utc() -> datetime:
    """Текущее время с часовым поясом UTC (для embed.timestamp: naive discord.py считает локальным временем)."""
    return datetime.now(UTC)


# Маппинг технических названий оружия в читаемые
WEAPON_NAMES = {
    'DT_SwordSlash_C': 'Сабля',
    'DT_WoodAxeSlash_C': 'Топор',
    'DT_PickaxePiercing_C': 'Кирка',
    'DT_CleaverSlashing_C': 'Тесак',
    'DT_DaggerSlash_C': 'Костяной кинжал',
    'DT_DaggerSlash_Self_C': 'Костяной кинжал (способность)',
    'DT_FistBlunt_C': 'Кулак',
    'DT_ShovelBlunt_C': 'Лопата',
    'DT_RockBlunt_C': 'Камень',
    'DT_StickBlunt_C': 'Палка',
    'DT_IngotBlunt_C': 'Слиток',
    'DT_Blunt_C': 'Бочка',
    'DT_PistolBullet_C': 'Пистолет',
    'DT_Arrow_C': 'Лук',
    'DT_Explosive_Charge_C': 'Заряд',
    'DT_Explosive_C': 'Саботаж базы (?)',
    'DT_Explosive_Offsite_C': 'Пороховая бочка',
    'DT_BearTrap_C': 'Медвежий капкан',
    'DT_Falling_C': 'Падение',
    'DT_WolfBite_C': 'Волк',
    'DT_BearAOE_C': 'Медведь',
    'DT_BearClaws_C': 'Медведь',
    'DT_BearBite_C': 'Медведь',
    'DT_MonsterClaw_C': 'Монстр',
    'DT_MonsterBite_C': 'Монстр',
    'DT_MonsterRock_C': 'Монстр',
}

# Природные источники урона
WILDLIFE_SOURCES = {
    'DT_WolfBite_C',
    'DT_BearAOE_C',
    'DT_BearClaws_C',
    'DT_BearBite_C',
    'DT_MonsterClaw_C',
    'DT_MonsterBite_C',
    'DT_MonsterRock_C',
}

WILDLIFE_GROUP_NAMES = {
    'DT_WolfBite_C': '🐺 Волк',
    'DT_BearAOE_C': '🐻 Медведь',
    'DT_BearClaws_C': '🐻 Медведь',
    'DT_BearBite_C': '🐻 Медведь',
    'DT_MonsterClaw_C': '👹 Монстр',
    'DT_MonsterBite_C': '👹 Монстр',
    'DT_MonsterRock_C': '👹 Монстр',
}


def format_weapons(weapons: dict, exclude_wildlife: bool = True) -> str:
    """Форматирует словарь оружий в строку"""
    result = {}
    for weapon, dmg in weapons.items():
        if exclude_wildlife and weapon in WILDLIFE_SOURCES:
            continue
        name = WEAPON_NAMES.get(weapon, weapon)
        result[name] = result.get(name, 0) + dmg
    parts = [f'{name}: {round(dmg)}' for name, dmg in sorted(result.items(), key=lambda x: x[1], reverse=True)]
    return ' · '.join(parts)


def calc_teamdamage(raw_damage: dict, attacker_ids: Collection[str], victim_ids: Collection[str]) -> dict:
    """Считает урон по тиммейтам: attacker_id -> суммарный урон по victim_ids (исключая себя)"""
    result = {}
    for attacker_id in attacker_ids:
        total = 0
        victims = raw_damage.get(attacker_id, {})
        for victim_id, weapons in victims.items():
            if victim_id in victim_ids and victim_id != attacker_id:
                total += sum(weapons.values())
        if total > 0:
            result[attacker_id] = total
    return result


def format_damage_stats(
    players_dmg: dict, raw_damage: dict, crew: list, traitors: list, player_names: dict
) -> str | None:
    """
    Возвращает строку со статистикой урона для embed, или None если данных нет.
    crew/traitors — список dict с ключом 'steam_id'
    """
    if not players_dmg:
        return None

    # dict, а не set: порядок игроков с равным уроном тот же, что во входном списке (у set он случайный)
    crewmate_ids = dict.fromkeys(r['steam_id'] for r in crew)
    traitor_ids = dict.fromkeys(r['steam_id'] for r in traitors)

    lines = []

    # --- Экипаж: урон по предателям ---
    lines.append('🗡️ **Урон по предателям (Crew)**')
    crew_dmg_rows = []
    for steam_id in crewmate_ids:
        name = player_names.get(steam_id, steam_id)
        p = players_dmg.get(steam_id)
        dmg = p['damage_to_traitors'] if p else 0
        role = p['role'] if p else ''
        crew_dmg_rows.append((name, dmg, role))
    crew_dmg_rows.sort(key=lambda x: x[1], reverse=True)
    for name, dmg, role in crew_dmg_rows:
        role_str = f' ({role})' if role else ''
        lines.append(f'• **{name}**{role_str}: {round(dmg)}')

    lines.append('')

    # --- Предатели: урон по экипажу ---
    lines.append('💀 **Урон по экипажу (Traitors)**')
    traitor_dmg_rows = []
    for steam_id in traitor_ids:
        name = player_names.get(steam_id, steam_id)
        p = players_dmg.get(steam_id)
        dmg = p['damage_to_crew'] if p else 0
        role = p['role'] if p else ''
        traitor_dmg_rows.append((name, dmg, role))
    traitor_dmg_rows.sort(key=lambda x: x[1], reverse=True)
    for name, dmg, role in traitor_dmg_rows:
        role_str = f' ({role})' if role else ''
        lines.append(f'• **{name}**{role_str}: {round(dmg)}')

    # --- Урон по тиммейтам ---
    crew_tk = calc_teamdamage(raw_damage, crewmate_ids, crewmate_ids)
    traitor_tk = calc_teamdamage(raw_damage, traitor_ids, traitor_ids)
    all_tk = {**crew_tk, **traitor_tk}

    if all_tk:
        lines.append('')
        lines.append('🤝 **Урон по тиммейтам**')
        for steam_id, dmg in sorted(all_tk.items(), key=lambda x: x[1], reverse=True):
            name = player_names.get(steam_id, steam_id)
            p = players_dmg.get(steam_id)
            role = p.get('role', '') if p else ''
            role_str = f' ({role})' if role else ''
            lines.append(f'• **{name}**{role_str}: {round(dmg)}')

    return '\n'.join(lines)


def _dmg_line(total: int, name: str, weapons_str: str) -> str:
    """Строка урона в детальной статистике: `123` Имя — оружие."""
    return f'`{total}` {name} — {weapons_str}' if weapons_str else f'`{total}` {name}'


def build_player_detail_text(steam_id: str, players_dmg: dict, raw_damage: dict, player_names: dict) -> str:
    """Детальная статистика игрока в виде plain text."""
    p = players_dmg.get(steam_id, {})
    name = player_names.get(steam_id, steam_id)
    role = p.get('role', '')
    is_traitor = p.get('isTraitor', False)

    team_icon = '👹' if is_traitor else '👥'
    team_label = 'Traitor' if is_traitor else 'Crewmate'
    role_str = f' · {role}' if role else ''

    lines = [f'{team_icon} **{name}** — {team_label}{role_str}', '']

    # === НАНЕСЕНО ВРАГАМ ===
    dealt = raw_damage.get(steam_id, {})
    enemy_ids = {sid for sid, pd in players_dmg.items() if pd.get('isTraitor') != is_traitor}
    team_ids = {sid for sid, pd in players_dmg.items() if pd.get('isTraitor') == is_traitor and sid != steam_id}

    enemy_entries = []
    tk_entries = []
    self_dmg = 0
    self_weapons_str = ''

    for victim_id, weapons in sorted(dealt.items(), key=lambda x: sum(x[1].values()), reverse=True):
        total = round(sum(weapons.values()))
        weapons_str = format_weapons(weapons)
        if victim_id == steam_id:
            self_dmg = total
            self_weapons_str = weapons_str
        elif victim_id in enemy_ids:
            vname = _player_label(victim_id, player_names, players_dmg)
            enemy_entries.append((vname, total, weapons_str))
        elif victim_id in team_ids:
            vname = _player_label(victim_id, player_names, players_dmg)
            tk_entries.append((vname, total, weapons_str))

    enemy_total = sum(e[1] for e in enemy_entries)
    enemy_label = 'Нанесено предателям' if not is_traitor else 'Нанесено экипажу'
    lines.append(f'**⚔️ {enemy_label}: {enemy_total}**')
    if enemy_entries:
        lines.extend(_dmg_line(total, vname, weapons_str) for vname, total, weapons_str in enemy_entries)
    else:
        lines.append('└ Нет данных')

    # === УРОН ПО ТИММЕЙТАМ ===
    if tk_entries:
        tk_total = sum(e[1] for e in tk_entries)
        lines.append(f'\n**🤝 По тиммейтам: {tk_total}**')
        lines.extend(_dmg_line(total, vname, weapons_str) for vname, total, weapons_str in tk_entries)

    # === УРОН СЕБЕ ===
    if self_dmg:
        line = f'\n**🪃 Себе: {self_dmg}**'
        if self_weapons_str:
            line += f' — {self_weapons_str}'
        lines.append(line)

    # === ПОЛУЧЕНО ===
    received = {aid: victims[steam_id] for aid, victims in raw_damage.items() if steam_id in victims}
    total_received = sum(sum(w.values()) for w in received.values())
    lines.append(f'\n**📥 Получено: {round(total_received)}**')
    wildlife_total = {}
    recv_entries = []
    for attacker_id, weapons in received.items():
        if attacker_id == 'wildlife':
            for weapon, dmg in weapons.items():
                group = WILDLIFE_GROUP_NAMES.get(weapon) or WEAPON_NAMES.get(weapon, weapon)
                wildlife_total[group] = wildlife_total.get(group, 0) + dmg
        else:
            aname = (
                '❓ Неизвестно' if attacker_id == 'Unknown' else _player_label(attacker_id, player_names, players_dmg)
            )
            recv_entries.append((aname, round(sum(weapons.values())), format_weapons(weapons)))
    lines.extend(
        _dmg_line(total, aname, weapons_str)
        for aname, total, weapons_str in sorted(recv_entries, key=lambda x: x[1], reverse=True)
    )
    if wildlife_total:
        wildlife_sum = round(sum(wildlife_total.values()))
        wildlife_parts = ' · '.join(
            f'{n}: {round(d)}' for n, d in sorted(wildlife_total.items(), key=lambda x: x[1], reverse=True)
        )
        lines.append(f'`{wildlife_sum}` 🌿 Природа — {wildlife_parts}')
    if not recv_entries and not wildlife_total:
        lines.append('└ Нет данных')

    return '\n'.join(lines)


def _player_label(steam_id: str, player_names: dict, players_dmg: dict) -> str:
    """Возвращает имя игрока с иконкой команды"""
    if steam_id == 'Unknown':
        return '❓ Неизвестно'
    name = player_names.get(steam_id, steam_id)
    p = players_dmg.get(steam_id, {})
    icon = '👹' if p.get('isTraitor') else '👥'
    return f'{icon} {name}'


# ============ Данные матча ============
def short_uuid(match_uuid: str) -> str:
    return match_uuid[:8] + '...' + match_uuid[-2:] if len(match_uuid) > 10 else match_uuid


def duration_text(seconds: int | None) -> str:
    if not seconds:
        return ''
    m, s = divmod(seconds, 60)
    return f'⏱️ Длительность: {m:02d}:{s:02d}'


def _names_from_rows(rows: list[dict]) -> dict[str, str]:
    return {row['steam_id']: row.get('name', row['steam_id']) for row in rows}


def _first(rows: list[dict], key: str):
    """Первое непустое значение поля среди строк матча."""
    for row in rows:
        if row.get(key) is not None:
            return row[key]
    return None


async def damage_data(repo: MatchResultsRepo, match_id: str) -> tuple[dict | None, dict, dict]:
    """(документ урона или None, players, raw_damage)."""
    doc = await repo.damage(match_id)
    return doc, (doc.get('players', {}) if doc else {}), (doc.get('raw_damage', {}) if doc else {})


async def menu_player_names(repo: MatchResultsRepo, match_id: str, players_dmg: dict) -> dict[str, str]:
    """Имена для меню статистики и репорта: из статистики, затем из урона, затем отображаемые ники."""
    names = _names_from_rows(await repo.match_rows(match_id))
    for sid, p in players_dmg.items():
        names.setdefault(sid, p.get('name', sid))
    names.update(await repo.nicknames(list(names)))
    return names


async def report_player_options(repo: MatchResultsRepo, match_id: str) -> list[tuple[str, str]]:
    """Игроки матча для репорта: [(steam_id, имя)]."""
    _, players_dmg, _ = await damage_data(repo, match_id)
    names = await menu_player_names(repo, match_id, players_dmg)
    return [(sid, name) for sid, name in names.items() if sid not in NON_PLAYER_IDS]


def stats_menu_options(players_dmg: dict, player_names: dict) -> list[tuple[str, str]]:
    """Пункты меню детальной статистики: [(steam_id, «👹 Имя (роль)»)]."""
    player_ids = [sid for sid in players_dmg if sid not in NON_PLAYER_IDS_MENU]
    player_ids += [sid for sid in player_names if sid not in player_ids]
    options = []
    for steam_id in player_ids:
        p = players_dmg.get(steam_id, {})
        name = player_names.get(steam_id, steam_id)
        role = p.get('role', '')
        icon = '👹' if p.get('isTraitor', False) else '👥'
        label = f'{icon} {name} ({role})' if role else f'{icon} {name}'
        options.append((steam_id, label[:100]))
    return options


async def player_detail(repo: MatchResultsRepo, match_id: str, steam_id: str) -> str | None:
    """Текст детальной статистики игрока (до 2000 символов) или None, если нет данных урона."""
    doc, players_dmg, raw_damage = await damage_data(repo, match_id)
    if not doc:
        return None
    names = await menu_player_names(repo, match_id, players_dmg)
    detail = build_player_detail_text(steam_id, players_dmg, raw_damage, names)
    return detail[:1997] + '...' if len(detail) > 2000 else detail


@dataclass
class Penalty:
    win_multiplier: float
    loss_multiplier: float
    reason: str


@dataclass
class RankedMatch:
    """Всё для уведомления о рейтинговом матче."""

    match_uuid: str
    records: list[dict]  # записи elo_history матча
    crew: list[dict]
    traitors: list[dict]
    crew_won: bool
    player_names: dict[str, str]
    duration_seconds: int | None
    match_datetime: datetime | None
    penalties: dict[str, Penalty]
    twink_rows: list[dict]
    twink_main_changes: dict[str, dict]
    players_dmg: dict = field(default_factory=dict)
    raw_damage: dict = field(default_factory=dict)


async def ranked_match(repo: MatchResultsRepo, match_uuid: str, season: int) -> RankedMatch | None:
    """Данные рейтингового матча из истории ELO; None, если записей нет."""
    records = await repo.elo_records(match_uuid, season)
    if not records:
        return None
    crew = [r for r in records if r['team'] == 'Crew']
    traitors = [r for r in records if r['team'] == 'Traitors']

    rows = await repo.match_rows(match_uuid)
    names = _names_from_rows(rows)
    names.update(await repo.nicknames(list(names), season))

    penalties = {
        r['steam_id']: Penalty(r.get('win_multiplier', 1.0), r.get('loss_multiplier', 1.0), r.get('penalty_reason', ''))
        for r in await repo.penalized([r['steam_id'] for r in records], season)
    }

    twinks = await repo.twink_rows(match_uuid)
    main_changes = (
        await repo.twink_main_changes(match_uuid, season, [t['twink_redirected_to'] for t in twinks]) if twinks else {}
    )

    _, players_dmg, raw_damage = await damage_data(repo, match_uuid)
    return RankedMatch(
        match_uuid=match_uuid,
        records=records,
        crew=crew,
        traitors=traitors,
        crew_won=crew[0]['won'] if crew else False,
        player_names=names,
        duration_seconds=_first(rows, 'match_duration_seconds'),
        match_datetime=records[0].get('match_datetime'),
        penalties=penalties,
        twink_rows=twinks,
        twink_main_changes=main_changes,
        players_dmg=players_dmg,
        raw_damage=raw_damage,
    )


@dataclass
class CasualMatch:
    match_uuid: str
    crew: list[dict]  # строки history_player_stats
    traitors: list[dict]
    crew_won: bool
    player_names: dict[str, str]
    duration_seconds: int | None
    match_datetime: datetime | None
    players_dmg: dict
    raw_damage: dict


async def casual_match(repo: MatchResultsRepo, match_uuid: str) -> CasualMatch | None:
    rows = await repo.match_rows(match_uuid)
    if not rows:
        return None
    names = _names_from_rows(rows)
    names.update(await repo.nicknames(list(names)))
    crew = [s for s in rows if s['team'] == 'Crew']
    traitors = [s for s in rows if s['team'] == 'Traitors']
    _, players_dmg, raw_damage = await damage_data(repo, match_uuid)
    return CasualMatch(
        match_uuid,
        crew,
        traitors,
        crew[0]['won'] if crew else False,
        names,
        _first(rows, 'match_duration_seconds'),
        _first(rows, 'match_datetime'),
        players_dmg,
        raw_damage,
    )


def penalty_effect(penalty: Penalty, won: bool) -> int:
    """Процент штрафа для этого исхода: при победе снижение, при поражении увеличение."""
    if won:
        return round((1 - penalty.win_multiplier) * 100)
    return round((penalty.loss_multiplier - 1) * 100)


async def save_report(
    reports_repo: ReportsRepo,
    reporter_id: str,
    reporter_name: str,
    anonymous: bool,
    target_steam_id: str,
    target_name: str,
    reasons: list[str],
    match_id: str,
) -> dict:
    """Репорт на игрока из матча (сохраняет фича reports)."""
    return await reports_service.save_report(
        reports_repo, reporter_id, reporter_name, anonymous, target_steam_id, target_name, reasons, match_id=match_id
    )

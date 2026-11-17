"""Полный граф игроков (players_graph.html): кто с кем играл по снапшотам лобби.

Только вычисления и HTML: без Discord, БД и сети, поэтому всё проверяется тестами.
Интерфейс поверх графа (панели, поиск, топы, ELO-таблица) в overlay.html.
"""

import json
import math
import random
from collections import Counter
from datetime import UTC, datetime, timedelta
from itertools import combinations
from pathlib import Path

import community as community_louvain
import networkx as nx
from pyvis.network import Network

from .repo import FullGraphData

OVERLAY = Path(__file__).with_name("overlay.html")

MIN_GAMES_TOGETHER = 5  # связь показывается, если игроки сыграли вместе не меньше
MIN_LINKS = 2  # игрок показывается, если у него не меньше связей

COMMUNITY_COLORS = [
    "#FF6B6B",
    "#4ECDC4",
    "#45B7D1",
    "#FFA07A",
    "#98D8C8",
    "#F7DC6F",
    "#BB8FCE",
    "#85C1E2",
    "#F8B88B",
    "#52BE80",
    "#FF85A2",
    "#5DADE2",
    "#F39C12",
    "#A569BD",
    "#48C9B0",
]

# Особые связи между игроками (ник, ник, подпись)
MANUAL_LINKS = [
    ("Antony", "John", "Идейные противники"),
]

PHYSICS_OPTIONS = """
        {
          "physics": {
            "forceAtlas2Based": {
              "gravitationalConstant": -100,
              "centralGravity": 0.003,
              "springLength": 250,
              "springConstant": 0.02,
              "damping": 0.8,
              "avoidOverlap": 0
            },
            "maxVelocity": 30,
            "minVelocity": 0.5,
            "solver": "forceAtlas2Based",
            "stabilization": {
              "enabled": true,
              "iterations": 800,
              "updateInterval": 40,
              "fit": true
            },
            "timestep": 0.35
          },
      "interaction": {
        "hideEdgesOnDrag": true,
        "hideEdgesOnZoom": true,
        "tooltipDelay": 100,
        "hover": false
      }
    }
    """


# ---------- Данные игроков ----------
def player_stats(stats_rows: list[dict]) -> dict[str, dict]:
    """Винрейты, пики сторон и любимая роль по steam_id."""
    stats = {}
    for r in stats_rows:
        total = r['total_games']
        role_counts = Counter(r['roles'])
        stats[r['_id']] = {
            'overall_wr': round(r['total_wins'] / total * 100, 1) if total > 0 else 0,
            'traitors_wr': round(r['traitors_wins'] / r['traitors_games'] * 100, 1)
            if r['traitors_games'] > 0
            else None,
            'crew_wr': round(r['crew_wins'] / r['crew_games'] * 100, 1) if r['crew_games'] > 0 else None,
            'traitors_pick': int(round(r['traitors_games'] / total * 100, 0) if total > 0 else 0),
            'crew_pick': int(round(r['crew_games'] / total * 100, 0) if total > 0 else 0),
            'most_common_role': role_counts.most_common(1)[0][0] if role_counts else None,
            'total_games': total,
        }
    return stats


def latest_nicknames(players: list[dict]) -> dict[str, str]:
    """steam_id -> ник из самой свежей записи (players отсортированы от новых к старым).
    Ник, заданный модератором (changed_nickname), важнее игрового."""
    names = {}
    for p in players:
        steam_id = p.get("other_id")
        if steam_id and steam_id not in names:
            names[steam_id] = p.get("changed_nickname") or p.get("nickname", "Unknown")
    return names


def game_to_steam_map(players: list[dict]) -> dict[str, str]:
    """game_uid -> steam_id (по самой свежей записи)."""
    mapping = {}
    for p in players:
        game_id, steam_id = p.get("game_uid"), p.get("other_id")
        if game_id and steam_id and game_id not in mapping:
            mapping[game_id] = steam_id
    return mapping


def lobby_steam_ids(lobby: dict, game_to_steam: dict[str, str]) -> list[str]:
    """Steam ID игроков лобби. В старых снапшотах игроки записаны как dict, в новых как game_uid."""
    steam_ids = []
    for entry in lobby.get("players", []):
        steam_id = entry.get("other_id") if isinstance(entry, dict) else game_to_steam.get(entry)
        if steam_id:
            steam_ids.append(steam_id)
    return steam_ids


def _host_steam_id(lobby: dict, game_to_steam: dict[str, str]) -> str | None:
    host = lobby.get("host")
    if isinstance(host, str):
        return game_to_steam.get(host)
    return host.get("other_id") if isinstance(host, dict) else None


# ---------- Граф ----------
def build_lobby_graph(lobbies: list[dict], game_to_steam: dict[str, str]):
    """Граф «играли в одном лобби» (вес = число общих лобби), сколько раз хостил, дата последней игры."""
    graph = nx.Graph()
    host_counts: dict[str, int] = {}
    last_game_date: dict[str, datetime] = {}
    for lobby in lobbies:
        steam_ids = lobby_steam_ids(lobby, game_to_steam)
        host = _host_steam_id(lobby, game_to_steam)
        if host:
            host_counts[host] = host_counts.get(host, 0) + 1
        lobby_date = lobby.get("timestamp")
        if lobby_date:
            for steam_id in steam_ids:
                if steam_id not in last_game_date or lobby_date > last_game_date[steam_id]:
                    last_game_date[steam_id] = lobby_date
        for a, b in combinations(steam_ids, 2):
            if graph.has_edge(a, b):
                graph[a][b]["weight"] += 1
            else:
                graph.add_edge(a, b, weight=1)
    return graph, host_counts, last_game_date


def remove_weak_links(graph: nx.Graph) -> None:
    """Убрать редкие связи и игроков почти без связей (иначе граф нечитаем и тормозит)."""
    graph.remove_edges_from([(a, b) for a, b, d in graph.edges(data=True) if d.get("weight", 1) < MIN_GAMES_TOGETHER])
    graph.remove_nodes_from([node for node in graph.nodes() if graph.degree(node) < MIN_LINKS])


def detect_communities(graph: nx.Graph):
    """Сообщества (алгоритм Лувена). Возвращает (steam_id -> id, [(id, участники)] от больших к малым)."""
    communities = community_louvain.best_partition(graph, random_state=42)
    members: dict[int, list[str]] = {}
    for node, comm_id in communities.items():
        members.setdefault(comm_id, []).append(node)
    return communities, sorted(members.items(), key=lambda x: len(x[1]), reverse=True)


def community_color(comm_id: int, sorted_communities: list) -> str:
    index = next((i for i, (cid, _) in enumerate(sorted_communities) if cid == comm_id), None)
    if index is not None and index < len(COMMUNITY_COLORS):
        return COMMUNITY_COLORS[index]
    rnd = random.Random(comm_id)  # стабильный светлый цвет для мелких сообществ
    return f"#{rnd.randint(150, 255):02x}{rnd.randint(150, 255):02x}{rnd.randint(150, 255):02x}"


# ---------- «Сколько времени назад» ----------
def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def ago_long(when: datetime | None, now: datetime) -> str:
    """Для подсказки узла: «5 мин. назад», «вчера», «2 нед. назад»."""
    if not when:
        return "неизвестно"
    delta = now - _as_utc(when)
    days, hours = delta.days, delta.total_seconds() / 3600
    if days == 0:
        return f"{int(delta.total_seconds() / 60)} мин. назад" if hours < 1 else f"{int(hours)} ч. назад"
    if days == 1:
        return "вчера"
    if days < 7:
        return f"{days} дн. назад"
    if days < 30:
        return f"{days // 7} нед. назад"
    return f"{days // 30} мес. назад"


def ago_short(when: datetime | None, now: datetime) -> str:
    """Для таблиц топов: «сейчас», «15м», «3ч», «вчера», «4д»."""
    if not when:
        return "?"
    delta = now - _as_utc(when)
    days, hours, minutes = delta.days, delta.total_seconds() / 3600, delta.total_seconds() / 60
    if days == 0:
        if minutes < 40:
            return "сейчас"
        return f"{int(minutes)}м" if hours < 1 else f"{int(hours)}ч"
    return "вчера" if days == 1 else f"{days}д"


# ---------- Топы и таблицы ----------
def top_active(
    lobbies: list[dict],
    game_to_steam: dict[str, str],
    names: dict[str, str],
    last_game_date: dict[str, datetime],
    since: datetime,
    now: datetime,
) -> list[dict]:
    """10 игроков с наибольшим числом лобби начиная с since."""
    activity: dict[str, int] = {}
    for lobby in lobbies:
        lobby_date = lobby.get("timestamp")
        if not lobby_date or _as_utc(lobby_date) < since:
            continue
        for steam_id in lobby_steam_ids(lobby, game_to_steam):
            activity[steam_id] = activity.get(steam_id, 0) + 1
    top = sorted(activity.items(), key=lambda x: x[1], reverse=True)[:10]
    return [
        {
            'nickname': names.get(steam_id, steam_id),
            'games': games,
            'last_game': ago_short(last_game_date.get(steam_id), now),
            'last_teammate': None,
        }
        for steam_id, games in top
    ]


def elo_table(elo_rows: list[dict]) -> list[dict]:
    table = []
    for i, p in enumerate(elo_rows):
        games, wins = p.get('ranked_games', 0), p.get('ranked_wins', 0)
        table.append(
            {
                'rank': i + 1,
                'name': p.get('changed_nickname') or p.get('current_name', 'Unknown'),
                'elo': round(p.get('elo', 1000)),
                'games': games,
                'wins': wins,
                'losses': games - wins,
                'wr': round(wins / games * 100, 1) if games > 0 else 0,
            }
        )
    return table


# ---------- Сборка HTML ----------
def build_full_graph_html(data: FullGraphData, now: datetime) -> str:
    stats = player_stats(data.stats_rows)
    names = latest_nicknames(data.players)
    game_to_steam = game_to_steam_map(data.players)

    graph, host_counts, last_game_date = build_lobby_graph(data.lobbies, game_to_steam)
    players_before_filter = graph.number_of_nodes()
    remove_weak_links(graph)
    communities, sorted_communities = detect_communities(graph)

    # На картинке узел = ник; игроки с одинаковым ником сливаются в один узел
    id_to_nick = {steam_id: names.get(steam_id, steam_id) for steam_id in graph.nodes}
    nick_to_id = {nick: steam_id for steam_id, nick in id_to_nick.items()}
    display = nx.Graph()
    for a, b, d in graph.edges(data=True):
        nick_a, nick_b, weight = id_to_nick[a], id_to_nick[b], d.get("weight", 1)
        if display.has_edge(nick_a, nick_b):
            display[nick_a][nick_b]["weight"] += weight
        else:
            display.add_edge(nick_a, nick_b, weight=weight)
    players_after_filter = display.number_of_nodes()

    net = Network(
        height="100vh", width="100%", bgcolor="#1e1e1e", font_color="white", notebook=False, select_menu=False
    )
    net.set_options(PHYSICS_OPTIONS)
    _add_player_nodes(
        net, display, nick_to_id, host_counts, communities, sorted_communities, last_game_date, stats, now
    )
    _add_link_edges(net, display)
    _add_manual_links(net, display)

    overlay = OVERLAY.read_text(encoding="utf-8")
    values = {
        "COMMUNITIES_JSON": json.dumps(_communities_table(sorted_communities, host_counts, names), ensure_ascii=False),
        "TOP_DAILY_JSON": json.dumps(
            top_active(data.lobbies, game_to_steam, names, last_game_date, now - timedelta(hours=24), now),
            ensure_ascii=False,
        ),
        "TOP_WEEKLY_JSON": json.dumps(
            top_active(data.lobbies, game_to_steam, names, last_game_date, now - timedelta(days=7), now),
            ensure_ascii=False,
        ),
        "TOP_MONTHLY_JSON": json.dumps(
            top_active(data.lobbies, game_to_steam, names, last_game_date, now - timedelta(days=30), now),
            ensure_ascii=False,
        ),
        "ELO_LEADERBOARD_JSON": json.dumps(elo_table(data.elo_rows), ensure_ascii=False),
        "TOTAL_PLAYERS_BEFORE_FILTER": str(players_before_filter),
        "TOTAL_PLAYERS_AFTER_FILTER": str(players_after_filter),
    }
    for key, value in values.items():
        overlay = overlay.replace(f"__{key}__", value)
    return net.generate_html().replace("</body>", overlay + "\n</body>")


def _add_player_nodes(
    net, display, nick_to_id, host_counts, communities, sorted_communities, last_game_date, stats, now
) -> None:
    max_hosts = max(host_counts.values()) if host_counts else 1
    for nickname in display.nodes():
        steam_id = nick_to_id[nickname]
        degree = display.degree(nickname)
        host_freq = host_counts.get(steam_id, 0)
        comm_id = communities.get(steam_id, -1)
        comm_index = next((i for i, (cid, _) in enumerate(sorted_communities) if cid == comm_id), -1)
        comm_display = f"#{comm_index + 1}" if comm_index != -1 else "N/A"
        last_game = ago_long(last_game_date.get(steam_id), now)
        s = stats.get(steam_id, {})
        net.add_node(
            nickname,
            label=nickname,
            title=f"Игрок: {nickname}\nСообщество: {comm_display}\nХостил: {host_freq} раз\nСвязей: {degree}\n"
            f"Последняя игра: {last_game}",
            color=community_color(comm_id, sorted_communities),
            size=10 + math.log1p(degree) * 8 + (host_freq / max_hosts) * 15,
            font={'size': 18, 'color': 'white', 'strokeWidth': 2, 'strokeColor': '#000'},
            last_game=last_game,
            last_party="",
            community=comm_display,
            overall_wr=s.get('overall_wr', 0),
            traitors_wr=s.get('traitors_wr') if s.get('traitors_wr') is not None else 0,
            crew_wr=s.get('crew_wr') if s.get('crew_wr') is not None else 0,
            traitors_pick=s.get('traitors_pick', 0),
            crew_pick=s.get('crew_pick', 0),
            most_common_role=s.get('most_common_role', 'N/A'),
            total_games=s.get('total_games', 0),
        )


def _add_link_edges(net, display) -> None:
    """Связи скрыты по умолчанию (прозрачные), overlay.html подсвечивает их при выборе игрока."""
    max_weight = max((d.get("weight", 1) for _, _, d in display.edges(data=True)), default=1)
    for nick_a, nick_b, d in display.edges(data=True):
        weight = d.get("weight", 1)
        net.add_edge(
            nick_a,
            nick_b,
            value=1 + math.log1p(weight) * 0.4,
            color="rgba(180, 180, 180, 0)",
            title=f"{nick_a} ⚔️ {nick_b}\n{weight} игр вместе\nNORM:{weight / max_weight:.3f}",
            smooth=False,
            games=weight,
        )


def _add_manual_links(net, display) -> None:
    for nick_a, nick_b, desc in MANUAL_LINKS:
        if nick_a not in display.nodes or nick_b not in display.nodes:
            continue
        games_together = 0
        if display.has_edge(nick_a, nick_b):
            games_together = display[nick_a][nick_b].get("weight", 0)
            display.remove_edge(nick_a, nick_b)
            net.edges = [e for e in net.edges if {e['from'], e['to']} != {nick_a, nick_b}]
        if games_together > 0:
            desc += f"\n\n⚔️ Играли вместе: {games_together} раз"
        net.add_edge(
            nick_a,
            nick_b,
            color="rgba(255, 60, 60, 0)",
            width=4,
            title=desc,
            smooth={"type": "curvedCW", "roundness": 0.2},
            length=300,
            manual=True,
            dashes=[10, 5],
        )


def _communities_table(sorted_communities, host_counts, names) -> list[dict]:
    table = []
    for i, (comm_id, members) in enumerate(sorted_communities[:15], 1):
        top_hosts = sorted([(m, host_counts.get(m, 0)) for m in members], key=lambda x: x[1], reverse=True)[:3]
        table.append(
            {
                'id': i,
                'size': len(members),
                'color': community_color(comm_id, sorted_communities),
                'top_hosts': [names.get(steam_id, steam_id) for steam_id, count in top_hosts if count > 0],
            }
        )
    return table

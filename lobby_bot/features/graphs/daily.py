"""Граф игроков за последние 24 часа (players_graph_daily.html).

В отличие от полного графа узлы здесь это game_uid, без фильтрации слабых связей,
а в подсказке узла перечислены лобби, где игрок хостил и играл.
"""

import math
from datetime import UTC, datetime
from itertools import combinations

import networkx as nx
from pyvis.network import Network


def _record_date(record: dict) -> datetime:
    """Дата записи игрока (date или timestamp; строка ISO или datetime) как наивное время UTC,
    чтобы даты разных форматов можно было сравнивать."""
    value = record.get("date", record.get("timestamp"))
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not value:
        return datetime.min
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value


def latest_names_by_game_id(players: list[dict]) -> dict[str, str]:
    """game_uid -> ник: самый свежий changed_nickname, иначе самый свежий игровой ник."""
    by_id: dict[str, list[dict]] = {}
    for p in players:
        if p.get("game_uid"):
            by_id.setdefault(p["game_uid"], []).append(p)
    names = {}
    for game_id, records in by_id.items():
        changed = [r for r in records if r.get("changed_nickname")]
        if changed:
            names[game_id] = max(changed, key=_record_date).get("changed_nickname")
        else:
            names[game_id] = max(records, key=_record_date).get("nickname", "Unknown")
    return names


def _lobbies_text(lobbies: list[tuple[str, list[str]]], names: dict[str, str], with_host: bool) -> str:
    lines = []
    for i, (host_id, players) in enumerate(lobbies, start=1):
        players_str = ", ".join(names.get(pid, pid) for pid in players)
        if with_host:
            lines.append(f"Лобби {i} (хост {names.get(host_id, 'Unknown Host')}): {players_str}")
        else:
            lines.append(f"Лобби {i}: {players_str}")
    return "\n".join(lines)


def _node_title(
    node: str, label: str, host_freq: int, host_lobbies: dict[str, list[list[str]]], names: dict[str, str]
) -> str:
    if host_freq > 0 and node in host_lobbies:
        own = [(node, players) for players in host_lobbies[node]]
        parts = [f"{label} — хостил {host_freq} раз(а):\n{_lobbies_text(own, names, with_host=False)}"]
        played = [
            (host_id, players)
            for host_id, lst in host_lobbies.items()
            if host_id != node
            for players in lst
            if node in players
        ]
        if played:
            parts.append(f"\nТакже играл у других ({len(played)} лобби):\n{_lobbies_text(played, names, True)}")
        return "\n".join(parts)

    played = [(host_id, players) for host_id, lst in host_lobbies.items() for players in lst if node in players]
    if played:
        return f"{label}\nИграл в {len(played)} лобби:\n{_lobbies_text(played, names, True)}"
    return f"{label}\nНет данных за последние 24 часа"


def build_daily_graph_html(lobbies: list[dict], players: list[dict]) -> str:
    names = latest_names_by_game_id(players)

    graph = nx.Graph()
    host_counts: dict[str, int] = {}
    host_lobbies: dict[str, list[list[str]]] = {}
    for lobby in lobbies:
        lobby_players, host = lobby.get("players", []), lobby.get("host")
        host_counts[host] = host_counts.get(host, 0) + 1
        host_lobbies.setdefault(host, []).append(lobby_players)
        for a, b in combinations(lobby_players, 2):
            if graph.has_edge(a, b):
                graph[a][b]["weight"] += 1
                graph[a][b]["hosts"].add(host)
            else:
                graph.add_edge(a, b, weight=1, hosts={host})
    nx.set_node_attributes(graph, {pid: names.get(pid, pid) for pid in graph.nodes}, "nickname")

    net = Network(height="100vh", width="100%", bgcolor="#1e1e1e", font_color="white", notebook=False, select_menu=True)
    net.force_atlas_2based(
        gravity=-90, central_gravity=0.003, spring_length=255, spring_strength=0.05, damping=0.8, overlap=0
    )

    max_hosts = max(host_counts.values()) if host_counts else 1
    for node, data in graph.nodes(data=True):
        label = data.get("nickname", node)
        degree = graph.degree(node)
        host_freq = host_counts.get(node, 0)
        if host_freq > max_hosts * 0.1:
            color = "#ffb347"  # частый хост
        elif host_freq > 0:
            color = "#ffd580"  # хостил
        else:
            color = "#4dabf7"
        net.add_node(
            node,
            label=label,
            title=_node_title(node, label, host_freq, host_lobbies, names),
            color=color,
            size=8 + math.log1p(degree) * 5 + (host_freq / max_hosts) * 10,
            font={'size': 15, 'color': 'white'},
        )

    max_weight = max((d.get("weight", 1) for _, _, d in graph.edges(data=True)), default=1)
    for a, b, d in graph.edges(data=True):
        weight = d.get("weight", 1)
        norm = weight / max_weight
        net.add_edge(
            a,
            b,
            value=min(0.005 + math.log1p(weight) * 0.005, 0.8),
            color=f"rgba({int(200 * (1 - norm))},{int(200 + 30 * norm)},{int(200 * (1 - norm))},"
            f"{min(0.2 + weight * 0.03, 0.7)})",
            title=f"{names.get(a, 'Unknown')} и {names.get(b, 'Unknown')} ИГРАЛИ друг с другом {weight} раз",
            smooth=False,
        )
    return net.generate_html()

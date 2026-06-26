from __future__ import annotations

import sqlite3
from collections import deque
from pathlib import Path
from typing import Any

from config import DB_PATH, OUTPUT_DIR

UNKNOWN = "UNKNOWN"

EVENT_CHAINS_SCHEMA = """
CREATE TABLE IF NOT EXISTS event_chains (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT,
    depth INTEGER,
    route_order INTEGER,
    from_task TEXT,
    to_task TEXT,
    message_id TEXT,
    data_name TEXT,
    caller_function TEXT,
    file_path TEXT,
    line INTEGER,
    confidence REAL,
    visited_key TEXT
);
"""


def _safe_filename_part(value: str) -> str:
    import re
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value.strip())
    cleaned = cleaned.strip(" .")
    return cleaned or "UNKNOWN"


def _normalize(value: str | None) -> str:
    if not value or not value.strip():
        return UNKNOWN
    return value.strip()


def build_event_chain(
    event_id: str,
    db_path: Path = DB_PATH,
    output_dir: Path | None = None,
    max_depth: int = 50,
) -> dict[str, Any]:
    import pandas as pd

    if output_dir is None:
        output_dir = db_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row

        # Ensure event_chains table exists
        conn.execute("DROP TABLE IF EXISTS event_chains")
        conn.executescript(EVENT_CHAINS_SCHEMA)
        conn.commit()

        # Fetch task_edges for the given event_id
        try:
            rows = conn.execute(
                """
                SELECT task_edges.event_id, task_edges.from_task,
                       task_edges.to_task, task_edges.message_id,
                       task_edges.data_name, task_edges.caller_function,
                       task_edges.file_id, task_edges.line,
                       task_edges.confidence, task_edges.edge_reason
                FROM task_edges
                WHERE task_edges.event_id = ?
                ORDER BY task_edges.line, task_edges.file_id
                """,
                (event_id,),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            print(f"[WARN] task_edges table not found. Run task-graph first: {exc}")
            rows = []

        if not rows:
            print(f"0 records found for event_id: {event_id}")
            # Still create empty outputs
            _write_empty_outputs(event_id, output_dir)
            # Insert empty record to create table
            return {
                "count": 0,
                "excel_path": output_dir / f"event_chain_{_safe_filename_part(event_id)}.xlsx",
                "dot_path": output_dir / f"event_chain_{_safe_filename_part(event_id)}.dot",
            }

        # Build graph: adjacency list from task_edges
        # key: from_task, value: list of edge dicts
        graph: dict[str, list[dict[str, Any]]] = {}
        all_tasks: set[str] = set()

        for row in rows:
            from_task = _normalize(row["from_task"])
            to_task = _normalize(row["to_task"])

            edge = {
                "from_task": from_task,
                "to_task": to_task,
                "event_id": _normalize(row["event_id"]),
                "message_id": _normalize(row["message_id"]),
                "data_name": _normalize(row["data_name"]),
                "caller_function": _normalize(row["caller_function"]),
                "file_id": row["file_id"],
                "line": row["line"],
                "confidence": row["confidence"],
                "edge_reason": row["edge_reason"] or "",
            }

            if from_task not in graph:
                graph[from_task] = []
            graph[from_task].append(edge)
            all_tasks.add(from_task)
            all_tasks.add(to_task)

        # Calculate in-degree for each node to find start candidates
        in_degree: dict[str, int] = {task: 0 for task in all_tasks}
        for from_task, edges in graph.items():
            for edge in edges:
                to_task = edge["to_task"]
                if to_task in in_degree:
                    in_degree[to_task] += 1

        # Start candidates: nodes with in_degree == 0
        start_candidates = [task for task in all_tasks if in_degree.get(task, 0) == 0]
        if not start_candidates:
            # If all nodes have incoming edges, use nodes with minimum in_degree
            min_degree = min(in_degree.values()) if in_degree else 0
            start_candidates = [task for task, degree in in_degree.items() if degree == min_degree]

        # BFS to build chains
        chain_rows: list[dict[str, Any]] = []
        visited: set[str] = set()
        route_order = 0

        for start in start_candidates:
            queue: deque[tuple[str, int, list[dict[str, Any]]]] = deque()
            queue.append((start, 0, []))

            while queue:
                current_task, depth, path_edges = queue.popleft()

                if depth > max_depth:
                    continue

                # Get outgoing edges from current_task
                outgoing_edges = graph.get(current_task, [])
                if not outgoing_edges:
                    # Terminal node: record the path
                    for pe in path_edges:
                        visited_key = (
                            f"{pe['event_id']}|{pe['from_task']}|{pe['to_task']}"
                            f"|{pe['message_id']}|{pe['data_name']}"
                        )
                        if visited_key not in visited:
                            visited.add(visited_key)
                            route_order += 1
                            chain_rows.append({
                                "event_id": pe["event_id"],
                                "depth": pe["_depth"],
                                "route_order": route_order,
                                "from_task": pe["from_task"],
                                "to_task": pe["to_task"],
                                "message_id": pe["message_id"],
                                "data_name": pe["data_name"],
                                "caller_function": pe["caller_function"],
                                "file_path": str(output_dir),  # We'll get actual path
                                "line": pe["line"],
                                "confidence": pe["confidence"],
                                "visited_key": visited_key,
                            })
                    continue

                for edge in outgoing_edges:
                    visited_key = (
                        f"{edge['event_id']}|{edge['from_task']}|{edge['to_task']}"
                        f"|{edge['message_id']}|{edge['data_name']}"
                    )

                    if visited_key in visited:
                        continue  # Prevent loops

                    visited.add(visited_key)
                    edge_copy = {**edge, "_depth": depth}
                    new_path = path_edges + [edge_copy]
                    route_order += 1

                    chain_rows.append({
                        "event_id": edge_copy["event_id"],
                        "depth": depth,
                        "route_order": route_order,
                        "from_task": edge_copy["from_task"],
                        "to_task": edge_copy["to_task"],
                        "message_id": edge_copy["message_id"],
                        "data_name": edge_copy["data_name"],
                        "caller_function": edge_copy["caller_function"],
                        "file_path": str(output_dir),  # Will be updated with file path
                        "line": edge_copy["line"],
                        "confidence": edge_copy["confidence"],
                        "visited_key": visited_key,
                    })

                    queue.append((edge["to_task"], depth + 1, new_path))

        # Update file_path from files table
        try:
            file_path_map = {}
            for row in conn.execute(
                "SELECT id, path FROM files"
            ).fetchall():
                file_path_map[row["id"]] = row["path"]

            for chain in chain_rows:
                # Find file_id from the edge
                for row in rows:
                    if (row["from_task"] == chain["from_task"] or
                            row["to_task"] == chain["to_task"]):
                        chain["file_path"] = file_path_map.get(row["file_id"], str(output_dir))
                        break
        except sqlite3.OperationalError:
            pass

        # Insert into event_chains table
        insert_sql = """
            INSERT INTO event_chains(event_id, depth, route_order, from_task, to_task,
                                     message_id, data_name, caller_function, file_path,
                                     line, confidence, visited_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        for chain in chain_rows:
            conn.execute(
                insert_sql,
                (
                    chain["event_id"],
                    chain["depth"],
                    chain["route_order"],
                    chain["from_task"],
                    chain["to_task"],
                    chain["message_id"],
                    chain["data_name"],
                    chain["caller_function"],
                    chain["file_path"],
                    chain["line"],
                    chain["confidence"],
                    chain["visited_key"],
                ),
            )
        conn.commit()

    print(f"Event chain built: {len(chain_rows)} records for event_id={event_id}")

    # Export Excel
    df = pd.DataFrame(chain_rows)
    safe_event = _safe_filename_part(event_id)
    excel_path = output_dir / f"event_chain_{safe_event}.xlsx"
    dot_path = output_dir / f"event_chain_{safe_event}.dot"

    df.to_excel(excel_path, index=False)
    print(f"Excel written: {excel_path}")

    # Export DOT
    _write_event_chain_dot(chain_rows, dot_path, event_id)

    return {
        "count": len(chain_rows),
        "excel_path": excel_path,
        "dot_path": dot_path,
    }


def _write_empty_outputs(event_id: str, output_dir: Path) -> None:
    import pandas as pd

    safe_event = _safe_filename_part(event_id)
    excel_path = output_dir / f"event_chain_{safe_event}.xlsx"
    dot_path = output_dir / f"event_chain_{safe_event}.dot"

    # Empty Excel
    df = pd.DataFrame(columns=[
        "event_id", "depth", "route_order", "from_task", "to_task",
        "message_id", "data_name", "caller_function", "file_path",
        "line", "confidence", "visited_key",
    ])
    df.to_excel(excel_path, index=False)
    print(f"Excel written (empty): {excel_path}")

    # Empty DOT
    lines = [
        "digraph event_chain {",
        f'    label="Event Chain: {safe_event} (empty)";',
        '    rankdir=LR;',
        '    graph [fontname="Arial", fontsize=12];',
        "}",
    ]
    dot_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"DOT written (empty): {dot_path}")


def _write_event_chain_dot(
    chains: list[dict[str, Any]],
    dot_path: Path,
    event_id: str,
) -> None:
    """Write DOT file for event chain visualization."""
    nodes: set[str] = set()
    edge_agg: dict[tuple[str, str], dict[str, Any]] = {}

    for chain in chains:
        from_task = chain["from_task"]
        to_task = chain["to_task"]
        key = (from_task, to_task)

        if key not in edge_agg:
            edge_agg[key] = {
                "from_task": from_task,
                "to_task": to_task,
                "depths": set(),
                "count": 0,
                "confidence": 0.0,
            }
        edge_agg[key]["depths"].add(chain["depth"])
        edge_agg[key]["count"] += 1
        edge_agg[key]["confidence"] = max(
            edge_agg[key]["confidence"], chain["confidence"] or 0.0
        )
        nodes.add(from_task)
        nodes.add(to_task)

    lines = ["digraph event_chain {"]
    lines.append(f'    label="Event Chain: {event_id}";')
    lines.append("    rankdir=LR;")
    lines.append('    graph [fontname="Arial", fontsize=12];')
    lines.append('    node [fontname="Arial", fontsize=10, shape=box];')
    lines.append('    edge [fontname="Arial", fontsize=9];')
    lines.append("")

    # Nodes
    for node in sorted(nodes):
        label = node.replace('"', '\\"')
        lines.append(f'    "{label}" [label="{label}"];')

    lines.append("")

    # Edges
    for key, agg in edge_agg.items():
        from_label = agg["from_task"].replace('"', '\\"')
        to_label = agg["to_task"].replace('"', '\\"')
        depths_str = ",".join(str(d) for d in sorted(agg["depths"]))
        label_parts = [
            f"depth=[{depths_str}]",
            f"count={agg['count']}",
            f"conf={agg['confidence']:.2f}",
        ]
        edge_label = "\\n".join(label_parts)
        lines.append(f'    "{from_label}" -> "{to_label}" [label="{edge_label}"];')

    lines.append("}")
    dot_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"DOT written: {dot_path}")
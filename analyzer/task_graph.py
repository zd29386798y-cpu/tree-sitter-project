from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from config import DB_PATH, OUTPUT_DIR

UNKNOWN = "UNKNOWN"

TASK_EDGES_SCHEMA = """
CREATE TABLE IF NOT EXISTS task_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT,
    from_task TEXT,
    to_task TEXT,
    message_id TEXT,
    data_name TEXT,
    caller_function TEXT,
    file_id INTEGER,
    line INTEGER,
    confidence REAL,
    edge_reason TEXT
);
"""


def _normalize_task_name(name: str | None) -> str:
    if not name or not name.strip():
        return UNKNOWN
    return name.strip()


def build_task_edges(
    db_path: Path = DB_PATH,
    event_id: str | None = None,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    import pandas as pd

    if output_dir is None:
        output_dir = db_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row

        # Ensure task_edges table exists
        conn.execute("DROP TABLE IF EXISTS task_edges")
        conn.executescript(TASK_EDGES_SCHEMA)
        conn.commit()

        # Fetch message_edges
        try:
            if event_id:
                rows = conn.execute(
                    """
                    SELECT message_edges.event_id, message_edges.from_task,
                           message_edges.to_task, message_edges.message_id,
                           message_edges.data_name, message_edges.caller_name,
                           message_edges.file_id, message_edges.line,
                           message_edges.confidence, message_edges.raw_call_text
                    FROM message_edges
                    WHERE message_edges.event_id = ?
                    ORDER BY message_edges.line, message_edges.file_id
                    """,
                    (event_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT message_edges.event_id, message_edges.from_task,
                           message_edges.to_task, message_edges.message_id,
                           message_edges.data_name, message_edges.caller_name,
                           message_edges.file_id, message_edges.line,
                           message_edges.confidence, message_edges.raw_call_text
                    FROM message_edges
                    ORDER BY message_edges.line, message_edges.file_id
                    """,
                ).fetchall()
        except sqlite3.OperationalError as exc:
            print(f"[WARN] message_edges table could not be read. Run middleware first: {exc}")
            rows = []

        # Build edges
        edge_rows: list[dict[str, Any]] = []
        for msg in rows:
            from_task = _normalize_task_name(msg["from_task"])
            to_task = _normalize_task_name(msg["to_task"])

            edge_rows.append({
                "event_id": msg["event_id"] or UNKNOWN,
                "from_task": from_task,
                "to_task": to_task,
                "message_id": msg["message_id"] or UNKNOWN,
                "data_name": msg["data_name"] or UNKNOWN,
                "caller_function": msg["caller_name"] or UNKNOWN,
                "file_id": msg["file_id"],
                "line": msg["line"],
                "confidence": msg["confidence"],
                "edge_reason": msg["raw_call_text"] or "",
            })

        # Insert into task_edges
        insert_sql = """
            INSERT INTO task_edges(event_id, from_task, to_task, message_id,
                                   data_name, caller_function, file_id, line,
                                   confidence, edge_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        for edge in edge_rows:
            conn.execute(
                insert_sql,
                (
                    edge["event_id"],
                    edge["from_task"],
                    edge["to_task"],
                    edge["message_id"],
                    edge["data_name"],
                    edge["caller_function"],
                    edge["file_id"],
                    edge["line"],
                    edge["confidence"],
                    edge["edge_reason"],
                ),
            )
        conn.commit()

    print(f"Task edges built: {len(edge_rows)} edges from message_edges.")

    # Export Excel
    df = pd.DataFrame(edge_rows)
    if not edge_rows:
        # Create empty dataframe with columns
        df = pd.DataFrame(columns=[
            "event_id", "from_task", "to_task", "message_id",
            "data_name", "caller_function", "file_id", "line",
            "confidence", "edge_reason",
        ])

    if event_id:
        safe_event = _safe_filename_part(event_id)
        excel_path = output_dir / f"task_graph_{safe_event}.xlsx"
        dot_path = output_dir / f"task_graph_{safe_event}.dot"
    else:
        excel_path = output_dir / "task_graph.xlsx"
        dot_path = output_dir / "task_graph.dot"

    df.to_excel(excel_path, index=False)
    print(f"Excel written: {excel_path}")

    # Export DOT
    _write_task_graph_dot(edge_rows, dot_path, event_id)

    return {
        "count": len(edge_rows),
        "excel_path": excel_path,
        "dot_path": dot_path,
    }


def _safe_filename_part(value: str) -> str:
    import re
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value.strip())
    cleaned = cleaned.strip(" .")
    return cleaned or "UNKNOWN"


def _write_task_graph_dot(
    edges: list[dict[str, Any]],
    dot_path: Path,
    event_id: str | None = None,
) -> None:
    """Write DOT file for task graph visualization."""
    # Aggregate edges: count occurrences per (from_task, to_task, event_id, message_id, data_name)
    edge_agg: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    nodes: set[str] = set()

    for edge in edges:
        key = (edge["from_task"], edge["to_task"], edge["event_id"],
               edge["message_id"], edge["data_name"])
        if key not in edge_agg:
            edge_agg[key] = {
                "from_task": edge["from_task"],
                "to_task": edge["to_task"],
                "event_id": edge["event_id"],
                "message_id": edge["message_id"],
                "data_name": edge["data_name"],
                "confidence": edge["confidence"],
                "count": 0,
            }
        edge_agg[key]["count"] += 1
        # Keep highest confidence
        edge_agg[key]["confidence"] = max(
            edge_agg[key]["confidence"], edge["confidence"]
        )
        nodes.add(edge["from_task"])
        nodes.add(edge["to_task"])

    lines = ["digraph task_graph {"]
    lines.append("    rankdir=LR;")
    lines.append('    graph [fontname="Arial", fontsize=12];')
    lines.append('    node [fontname="Arial", fontsize=10, shape=box];')
    lines.append('    edge [fontname="Arial", fontsize=9];')
    lines.append("")

    # Define nodes
    for node in sorted(nodes):
        label = node.replace('"', '\\"')
        lines.append(f'    "{label}" [label="{label}"];')

    lines.append("")

    # Define edges
    for key, agg in edge_agg.items():
        from_label = agg["from_task"].replace('"', '\\"')
        to_label = agg["to_task"].replace('"', '\\"')
        label_parts = []
        if agg["event_id"] != UNKNOWN:
            label_parts.append(f"event={agg['event_id']}")
        if agg["message_id"] != UNKNOWN:
            label_parts.append(f"msg={agg['message_id']}")
        if agg["data_name"] != UNKNOWN:
            label_parts.append(f"data={agg['data_name']}")
        label_parts.append(f"count={agg['count']}")
        label_parts.append(f"conf={agg['confidence']:.2f}")
        edge_label = "\\n".join(label_parts)
        lines.append(f'    "{from_label}" -> "{to_label}" [label="{edge_label}"];')

    lines.append("}")
    dot_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"DOT written: {dot_path}")
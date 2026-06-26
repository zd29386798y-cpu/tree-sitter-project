from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from config import DB_PATH, OUTPUT_DIR

UNKNOWN = "UNKNOWN"

DATA_FLOW_EDGES_SCHEMA = """
CREATE TABLE IF NOT EXISTS data_flow_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT,
    from_task TEXT,
    to_task TEXT,
    from_function TEXT,
    to_function TEXT,
    data_name TEXT,
    data_kind TEXT,
    direction TEXT,
    access_type TEXT,
    source_type TEXT,
    file_id INTEGER,
    line INTEGER,
    confidence REAL,
    reason TEXT
);
"""

DATA_KINDS = ("message", "file", "db", "shared_memory")
ACCESS_TYPES = ("SEND", "RECEIVE", "READ", "WRITE")
DIRECTIONS = ("OUT", "IN", "BOTH")


def _safe_filename_part(value: str) -> str:
    import re
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value.strip())
    cleaned = cleaned.strip(" .")
    return cleaned or "UNKNOWN"


def _normalize(value: str | None) -> str:
    if not value or not value.strip():
        return UNKNOWN
    return value.strip()


def build_data_flow(
    db_path: Path = DB_PATH,
    event_id: str | None = None,
    data_name: str | None = None,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    import pandas as pd

    if output_dir is None:
        output_dir = db_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row

        # Ensure data_flow_edges table exists
        conn.execute("DROP TABLE IF EXISTS data_flow_edges")
        conn.executescript(DATA_FLOW_EDGES_SCHEMA)
        conn.commit()

        flow_rows: list[dict[str, Any]] = []

        # === 1. MESSAGE 由来 ===
        try:
            msg_query = """
                SELECT message_edges.event_id, message_edges.from_task,
                       message_edges.to_task, message_edges.caller_name,
                       message_edges.data_name, message_edges.file_id,
                       message_edges.line, message_edges.confidence,
                       message_edges.raw_call_text
                FROM message_edges
                WHERE 1=1
            """
            params: list[Any] = []
            if event_id:
                msg_query += " AND message_edges.event_id = ?"
                params.append(event_id)
            if data_name:
                msg_query += " AND message_edges.data_name = ?"
                params.append(data_name)
            msg_query += " ORDER BY message_edges.line, message_edges.file_id"

            message_rows = conn.execute(msg_query, params).fetchall()
        except sqlite3.OperationalError as exc:
            print(f"[WARN] message_edges table could not be read: {exc}")
            message_rows = []

        for msg in message_rows:
            flow_rows.append({
                "event_id": _normalize(msg["event_id"]),
                "from_task": _normalize(msg["from_task"]),
                "to_task": _normalize(msg["to_task"]),
                "from_function": _normalize(msg["caller_name"]),
                "to_function": _normalize(msg["caller_name"]),
                "data_name": _normalize(msg["data_name"]),
                "data_kind": "message",
                "direction": "OUT",
                "access_type": "SEND",
                "source_type": "message",
                "file_id": msg["file_id"],
                "line": msg["line"],
                "confidence": msg["confidence"],
                "reason": msg["raw_call_text"] or "",
            })
            flow_rows.append({
                "event_id": _normalize(msg["event_id"]),
                "from_task": _normalize(msg["from_task"]),
                "to_task": _normalize(msg["to_task"]),
                "from_function": _normalize(msg["caller_name"]),
                "to_function": _normalize(msg["caller_name"]),
                "data_name": _normalize(msg["data_name"]),
                "data_kind": "message",
                "direction": "IN",
                "access_type": "RECEIVE",
                "source_type": "message",
                "file_id": msg["file_id"],
                "line": msg["line"],
                "confidence": msg["confidence"],
                "reason": msg["raw_call_text"] or "",
            })

        # === 2. FILE / DB / SHARED 由来 ===
        try:
            access_query = """
                SELECT data_accesses.task_name, data_accesses.function_name,
                       data_accesses.access_type, data_accesses.data_kind,
                       data_accesses.data_name, data_accesses.direction,
                       data_accesses.file_id, data_accesses.line,
                       data_accesses.confidence, data_accesses.raw_call_text
                FROM data_accesses
                WHERE data_accesses.data_kind IN ('file_read', 'file_write',
                                                  'db_read', 'db_write',
                                                  'shared_read', 'shared_write')
            """
            access_params: list[Any] = []
            if event_id:
                access_query += " AND data_accesses.task_name IN (SELECT DISTINCT from_task FROM message_edges WHERE event_id = ?)"
                access_params.append(event_id)
                access_query += " AND data_accesses.task_name IN (SELECT DISTINCT to_task FROM message_edges WHERE event_id = ?)"
                access_params.append(event_id)
            if data_name:
                access_query += " AND data_accesses.data_name = ?"
                access_params.append(data_name)
            access_query += " ORDER BY data_accesses.line, data_accesses.file_id"

            access_rows = conn.execute(access_query, access_params).fetchall()
        except sqlite3.OperationalError as exc:
            print(f"[WARN] data_accesses table could not be read: {exc}")
            access_rows = []

        # Group by data_name and data_kind to find WRITE -> READ pairs
        write_accesses: dict[str, list[dict[str, Any]]] = {}
        read_accesses: dict[str, list[dict[str, Any]]] = {}

        for access in access_rows:
            data_name_val = _normalize(access["data_name"])
            data_kind = access["data_kind"] or UNKNOWN
            key = f"{data_name_val}|{data_kind}"

            access_dict = {
                "task_name": _normalize(access["task_name"]),
                "function_name": _normalize(access["function_name"]),
                "access_type": access["access_type"] or UNKNOWN,
                "data_kind": data_kind,
                "data_name": data_name_val,
                "direction": access["direction"] or UNKNOWN,
                "file_id": access["file_id"],
                "line": access["line"],
                "confidence": access["confidence"],
                "raw_call_text": access["raw_call_text"] or "",
            }

            if data_kind in ("file_write", "db_write", "shared_write"):
                if key not in write_accesses:
                    write_accesses[key] = []
                write_accesses[key].append(access_dict)
            elif data_kind in ("file_read", "db_read", "shared_read"):
                if key not in read_accesses:
                    read_accesses[key] = []
                read_accesses[key].append(access_dict)

        # Pair WRITE -> READ
        for key, writes in write_accesses.items():
            reads = read_accesses.get(key, [])
            data_name_val = key.split("|")[0]
            data_kind_raw = key.split("|")[1] if "|" in key else UNKNOWN

            # Map data_kind to source_type
            if "file" in data_kind_raw.lower():
                source_type = "file"
                data_kind = "file"
            elif "db" in data_kind_raw.lower():
                source_type = "db"
                data_kind = "db"
            elif "shared" in data_kind_raw.lower():
                source_type = "shared_memory"
                data_kind = "shared_memory"
            else:
                source_type = UNKNOWN
                data_kind = UNKNOWN

            for write in writes:
                for read in reads:
                    flow_rows.append({
                        "event_id": event_id or UNKNOWN,
                        "from_task": write["task_name"],
                        "to_task": read["task_name"],
                        "from_function": write["function_name"],
                        "to_function": read["function_name"],
                        "data_name": data_name_val,
                        "data_kind": data_kind,
                        "direction": "OUT",
                        "access_type": "WRITE",
                        "source_type": source_type,
                        "file_id": write["file_id"],
                        "line": write["line"],
                        "confidence": min(write["confidence"], read["confidence"]) if write["confidence"] and read["confidence"] else 0.5,
                        "reason": write["raw_call_text"],
                    })
                    flow_rows.append({
                        "event_id": event_id or UNKNOWN,
                        "from_task": write["task_name"],
                        "to_task": read["task_name"],
                        "from_function": write["function_name"],
                        "to_function": read["function_name"],
                        "data_name": data_name_val,
                        "data_kind": data_kind,
                        "direction": "IN",
                        "access_type": "READ",
                        "source_type": source_type,
                        "file_id": read["file_id"],
                        "line": read["line"],
                        "confidence": min(write["confidence"], read["confidence"]) if write["confidence"] and read["confidence"] else 0.5,
                        "reason": read["raw_call_text"],
                    })

        # Insert into data_flow_edges
        insert_sql = """
            INSERT INTO data_flow_edges(event_id, from_task, to_task,
                from_function, to_function, data_name, data_kind, direction,
                access_type, source_type, file_id, line, confidence, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        for flow in flow_rows:
            conn.execute(
                insert_sql,
                (
                    flow["event_id"],
                    flow["from_task"],
                    flow["to_task"],
                    flow["from_function"],
                    flow["to_function"],
                    flow["data_name"],
                    flow["data_kind"],
                    flow["direction"],
                    flow["access_type"],
                    flow["source_type"],
                    flow["file_id"],
                    flow["line"],
                    flow["confidence"],
                    flow["reason"],
                ),
            )
        conn.commit()

    print(f"Data flow edges built: {len(flow_rows)} edges.")

    # Export Excel
    df = pd.DataFrame(flow_rows)
    if not flow_rows:
        df = pd.DataFrame(columns=[
            "event_id", "from_task", "to_task", "from_function", "to_function",
            "data_name", "data_kind", "direction", "access_type", "source_type",
            "file_id", "line", "confidence", "reason",
        ])

    if event_id and data_name:
        safe_event = _safe_filename_part(event_id)
        safe_data = _safe_filename_part(data_name)
        excel_path = output_dir / f"data_flow_{safe_event}_{safe_data}.xlsx"
        dot_path = output_dir / f"data_flow_{safe_event}.dot"
        suffix_excel = output_dir / f"data_flow_{safe_event}.xlsx"
    elif event_id:
        safe_event = _safe_filename_part(event_id)
        excel_path = output_dir / f"data_flow_{safe_event}.xlsx"
        dot_path = output_dir / f"data_flow_{safe_event}.dot"
        suffix_excel = None
    elif data_name:
        safe_data = _safe_filename_part(data_name)
        excel_path = output_dir / f"data_flow_{safe_data}.xlsx"
        dot_path = output_dir / "data_flow.dot"
        suffix_excel = None
    else:
        excel_path = output_dir / "data_flow.xlsx"
        dot_path = output_dir / "data_flow.dot"
        suffix_excel = None

    df.to_excel(excel_path, index=False)
    print(f"Excel written: {excel_path}")

    # Also write event-specific file if event_id is given
    if event_id and suffix_excel and suffix_excel != excel_path:
        df.to_excel(suffix_excel, index=False)
        print(f"Excel written: {suffix_excel}")

    # Export DOT
    _write_data_flow_dot(flow_rows, dot_path, event_id, data_name)

    return {
        "count": len(flow_rows),
        "excel_path": excel_path,
        "dot_path": dot_path,
    }


def _write_data_flow_dot(
    flows: list[dict[str, Any]],
    dot_path: Path,
    event_id: str | None = None,
    data_name: str | None = None,
) -> None:
    """Write DOT file for data flow visualization."""
    nodes: set[str] = set()
    edge_agg: dict[tuple[str, str, str, str], dict[str, Any]] = {}

    for flow in flows:
        from_task = flow["from_task"]
        to_task = flow["to_task"]
        data_name_val = flow["data_name"]
        access_type = flow["access_type"]
        source_type = flow["source_type"]
        key = (from_task, to_task, data_name_val, access_type)

        if key not in edge_agg:
            edge_agg[key] = {
                "from_task": from_task,
                "to_task": to_task,
                "data_name": data_name_val,
                "access_type": access_type,
                "source_type": source_type,
                "count": 0,
                "confidence": 0.0,
                "directions": set(),
            }
        edge_agg[key]["count"] += 1
        edge_agg[key]["confidence"] = max(
            edge_agg[key]["confidence"], flow["confidence"] or 0.0
        )
        edge_agg[key]["directions"].add(flow["direction"])
        nodes.add(from_task)
        nodes.add(to_task)

    lines = ["digraph data_flow {"]
    title = "Data Flow"
    if event_id:
        title += f" (event={event_id})"
    if data_name:
        title += f" (data={data_name})"
    lines.append(f'    label="{title}";')
    lines.append("    rankdir=LR;")
    lines.append('    graph [fontname="Arial", fontsize=12];')
    lines.append('    node [fontname="Arial", fontsize=10, shape=box];')
    lines.append('    edge [fontname="Arial", fontsize=9];')
    lines.append("")

    for node in sorted(nodes):
        label = node.replace('"', '\\"')
        lines.append(f'    "{label}" [label="{label}"];')

    lines.append("")

    for key, agg in edge_agg.items():
        from_label = agg["from_task"].replace('"', '\\"')
        to_label = agg["to_task"].replace('"', '\\"')
        color = "black"
        style = "solid"
        if agg["source_type"] == "file":
            color = "blue"
        elif agg["source_type"] == "db":
            color = "green"
        elif agg["source_type"] == "shared_memory":
            color = "purple"
        elif agg["source_type"] == "message":
            color = "orange"

        label_parts = [
            f"data={agg['data_name']}",
            f"type={agg['access_type']}",
            f"src={agg['source_type']}",
            f"count={agg['count']}",
            f"conf={agg['confidence']:.2f}",
        ]
        edge_label = "\\n".join(label_parts)

        dir_attr = ""
        if agg["access_type"] in ("READ", "RECEIVE"):
            dir_attr = " [dir=back]"
        elif agg["access_type"] in ("WRITE", "SEND"):
            dir_attr = ""
        elif "IN" in agg["directions"] and "OUT" in agg["directions"]:
            dir_attr = " [dir=both]"

        lines.append(
            f'    "{from_label}" -> "{to_label}" '
            f'[label="{edge_label}", color="{color}", style="{style}"];'
        )

    lines.append("}")
    dot_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"DOT written: {dot_path}")
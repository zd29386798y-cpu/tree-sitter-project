from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from config import DB_PATH, OUTPUT_DIR

UNKNOWN = "UNKNOWN"

BUSINESS_ROUTES_SCHEMA = """
CREATE TABLE IF NOT EXISTS business_routes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT,
    route_order INTEGER,
    depth INTEGER,
    node_type TEXT,
    task_name TEXT,
    function_name TEXT,
    data_name TEXT,
    data_kind TEXT,
    action TEXT,
    next_task TEXT,
    file_path TEXT,
    line INTEGER,
    confidence REAL,
    reason TEXT
);
"""

NODE_TYPES = (
    "EVENT",
    "TASK",
    "FUNCTION",
    "MESSAGE",
    "DATA",
    "FILE",
    "DB",
    "SHARED_MEMORY",
    "UNKNOWN",
)

ACTIONS = (
    "START",
    "SEND",
    "RECEIVE",
    "READ",
    "WRITE",
    "CALL",
    "PROCESS",
    "END",
)


def _safe_filename_part(value: str) -> str:
    import re
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value.strip())
    cleaned = cleaned.strip(" .")
    return cleaned or "UNKNOWN"


def _normalize(value: str | None) -> str:
    if not value or not value.strip():
        return UNKNOWN
    return value.strip()


def build_business_route(
    event_id: str,
    db_path: Path = DB_PATH,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    import pandas as pd

    if output_dir is None:
        output_dir = db_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row

        # Ensure business_routes table exists
        conn.execute("DROP TABLE IF EXISTS business_routes")
        conn.executescript(BUSINESS_ROUTES_SCHEMA)
        conn.commit()

        route_rows: list[dict[str, Any]] = []
        route_order = 0

        # === Step 1: Get event_chains ===
        try:
            chain_rows = conn.execute(
                """
                SELECT event_chains.event_id, event_chains.depth,
                       event_chains.route_order, event_chains.from_task,
                       event_chains.to_task, event_chains.message_id,
                       event_chains.data_name, event_chains.caller_function,
                       event_chains.file_path, event_chains.line,
                       event_chains.confidence
                FROM event_chains
                WHERE event_chains.event_id = ?
                ORDER BY event_chains.route_order, event_chains.depth
                """,
                (event_id,),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            print(f"[WARN] event_chains table not found. Run event-chain first: {exc}")
            chain_rows = []

        if not chain_rows:
            print(f"0 records found for event_id: {event_id}")
            # Create empty output
            _write_empty_outputs(event_id, output_dir)
            return {
                "count": 0,
                "excel_path": output_dir / f"business_route_{_safe_filename_part(event_id)}.xlsx",
                "dot_path": output_dir / f"business_route_{_safe_filename_part(event_id)}.dot",
                "html_path": output_dir / f"business_route_{_safe_filename_part(event_id)}.html",
            }

        # === Step 2: Get data_flow_edges for this event ===
        try:
            flow_rows = conn.execute(
                """
                SELECT data_flow_edges.from_task, data_flow_edges.to_task,
                       data_flow_edges.from_function, data_flow_edges.to_function,
                       data_flow_edges.data_name, data_flow_edges.data_kind,
                       data_flow_edges.direction, data_flow_edges.access_type,
                       data_flow_edges.source_type, data_flow_edges.file_id,
                       data_flow_edges.line, data_flow_edges.confidence,
                       data_flow_edges.reason
                FROM data_flow_edges
                WHERE data_flow_edges.event_id = ?
                   OR data_flow_edges.from_task IN (
                       SELECT DISTINCT from_task FROM event_chains WHERE event_id = ?
                   )
                   OR data_flow_edges.to_task IN (
                       SELECT DISTINCT to_task FROM event_chains WHERE event_id = ?
                   )
                ORDER BY data_flow_edges.line
                """,
                (event_id, event_id, event_id),
            ).fetchall()
        except sqlite3.OperationalError:
            flow_rows = []

        # Build a lookup for data flows by (from_task, to_task)
        flow_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for flow in flow_rows:
            key = (flow["from_task"], flow["to_task"])
            if key not in flow_by_key:
                flow_by_key[key] = []
            flow_by_key[key].append({
                "from_task": _normalize(flow["from_task"]),
                "to_task": _normalize(flow["to_task"]),
                "from_function": _normalize(flow["from_function"]),
                "to_function": _normalize(flow["to_function"]),
                "data_name": _normalize(flow["data_name"]),
                "data_kind": flow["data_kind"] or UNKNOWN,
                "direction": flow["direction"] or UNKNOWN,
                "access_type": flow["access_type"] or UNKNOWN,
                "source_type": flow["source_type"] or UNKNOWN,
                "file_id": flow["file_id"],
                "line": flow["line"],
                "confidence": flow["confidence"],
                "reason": flow["reason"] or "",
            })

        # Build file path map
        file_path_map: dict[int, str] = {}
        try:
            for row in conn.execute("SELECT id, path FROM files").fetchall():
                file_path_map[row["id"]] = row["path"]
        except sqlite3.OperationalError:
            pass

        # === Step 3: Build route ===
        seen_keys: set[str] = set()
        # Track task order by appearance in event_chains
        task_order: list[str] = []
        for chain in chain_rows:
            if chain["from_task"] not in task_order:
                task_order.append(chain["from_task"])
            if chain["to_task"] not in task_order:
                task_order.append(chain["to_task"])

        # Add START event node
        route_order += 1
        route_rows.append({
            "event_id": event_id,
            "route_order": route_order,
            "depth": 0,
            "node_type": "EVENT",
            "task_name": "",
            "function_name": "",
            "data_name": "",
            "data_kind": "",
            "action": "START",
            "next_task": task_order[0] if task_order else "",
            "file_path": "",
            "line": None,
            "confidence": 1.0,
            "reason": f"Event started: {event_id}",
        })

        # Process chains
        for chain in chain_rows:
            from_task = _normalize(chain["from_task"])
            to_task = _normalize(chain["to_task"])
            message_id = _normalize(chain["message_id"])
            data_name_val = _normalize(chain["data_name"])
            caller_func = _normalize(chain["caller_function"])
            depth = chain["depth"] or 0

            # Create key to avoid duplicates
            chain_key = f"{event_id}|{from_task}|{to_task}|{message_id}|{data_name_val}|{depth}"
            if chain_key in seen_keys:
                continue
            seen_keys.add(chain_key)

            # Find file_path for this chain
            chain_file_path = chain["file_path"] or ""

            # FROM task node
            route_order += 1
            route_rows.append({
                "event_id": event_id,
                "route_order": route_order,
                "depth": depth,
                "node_type": "TASK",
                "task_name": from_task,
                "function_name": caller_func,
                "data_name": "",
                "data_kind": "",
                "action": "PROCESS",
                "next_task": to_task,
                "file_path": chain_file_path,
                "line": chain["line"],
                "confidence": chain["confidence"],
                "reason": f"Task {from_task} processes message",
            })

            # SEND message
            route_order += 1
            route_rows.append({
                "event_id": event_id,
                "route_order": route_order,
                "depth": depth,
                "node_type": "MESSAGE",
                "task_name": from_task,
                "function_name": caller_func,
                "data_name": message_id,
                "data_kind": "message",
                "action": "SEND",
                "next_task": to_task,
                "file_path": chain_file_path,
                "line": chain["line"],
                "confidence": chain["confidence"],
                "reason": f"Send message {message_id}",
            })

            # Data flows for this edge
            flow_key = (from_task, to_task)
            if flow_key in flow_by_key:
                for flow in flow_by_key[flow_key]:
                    source_type = flow["source_type"]
                    data_name_val_flow = flow["data_name"]
                    access_type = flow["access_type"]
                    action_flow = access_type  # SEND / RECEIVE / READ / WRITE
                    flow_file_path = file_path_map.get(flow["file_id"], "")

                    # Determine node_type from source_type
                    if source_type == "file":
                        node_type = "FILE"
                    elif source_type == "db":
                        node_type = "DB"
                    elif source_type == "shared_memory":
                        node_type = "SHARED_MEMORY"
                    elif source_type == "message":
                        node_type = "MESSAGE"
                    else:
                        node_type = "DATA"

                    flow_key_unique = (
                        f"{from_task}|{to_task}|{data_name_val_flow}|{access_type}"
                    )
                    if flow_key_unique not in seen_keys:
                        seen_keys.add(flow_key_unique)
                        route_order += 1
                        route_rows.append({
                            "event_id": event_id,
                            "route_order": route_order,
                            "depth": depth + 1,
                            "node_type": node_type,
                            "task_name": from_task,
                            "function_name": flow["from_function"],
                            "data_name": data_name_val_flow,
                            "data_kind": source_type,
                            "action": action_flow,
                            "next_task": to_task,
                            "file_path": flow_file_path,
                            "line": flow["line"],
                            "confidence": flow["confidence"],
                            "reason": flow["reason"],
                        })

            # TO task node
            route_order += 1
            route_rows.append({
                "event_id": event_id,
                "route_order": route_order,
                "depth": depth,
                "node_type": "TASK",
                "task_name": to_task,
                "function_name": caller_func,
                "data_name": "",
                "data_kind": "",
                "action": "RECEIVE",
                "next_task": "",
                "file_path": chain_file_path,
                "line": chain["line"],
                "confidence": chain["confidence"],
                "reason": f"Task {to_task} receives message",
            })

        # Add END node
        route_order += 1
        route_rows.append({
            "event_id": event_id,
            "route_order": route_order,
            "depth": max((r.get("depth", 0) or 0) for r in route_rows) if route_rows else 0,
            "node_type": "EVENT",
            "task_name": "",
            "function_name": "",
            "data_name": "",
            "data_kind": "",
            "action": "END",
            "next_task": "",
            "file_path": "",
            "line": None,
            "confidence": 1.0,
            "reason": f"Event completed: {event_id}",
        })

        # Insert into business_routes
        insert_sql = """
            INSERT INTO business_routes(event_id, route_order, depth, node_type,
                task_name, function_name, data_name, data_kind, action,
                next_task, file_path, line, confidence, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        for route in route_rows:
            conn.execute(
                insert_sql,
                (
                    route["event_id"],
                    route["route_order"],
                    route["depth"],
                    route["node_type"],
                    route["task_name"],
                    route["function_name"],
                    route["data_name"],
                    route["data_kind"],
                    route["action"],
                    route["next_task"],
                    route["file_path"],
                    route["line"],
                    route["confidence"],
                    route["reason"],
                ),
            )
        conn.commit()

    print(f"Business route built: {len(route_rows)} records for event_id={event_id}")

    # Export Excel
    df = pd.DataFrame(route_rows)
    safe_event = _safe_filename_part(event_id)
    excel_path = output_dir / f"business_route_{safe_event}.xlsx"
    dot_path = output_dir / f"business_route_{safe_event}.dot"
    html_path = output_dir / f"business_route_{safe_event}.html"

    df.to_excel(excel_path, index=False)
    print(f"Excel written: {excel_path}")

    # Export DOT
    _write_business_route_dot(route_rows, dot_path, event_id)

    # Export HTML
    _write_business_route_html(route_rows, html_path, event_id)

    return {
        "count": len(route_rows),
        "excel_path": excel_path,
        "dot_path": dot_path,
        "html_path": html_path,
    }


def _write_empty_outputs(event_id: str, output_dir: Path) -> None:
    import pandas as pd

    safe_event = _safe_filename_part(event_id)
    excel_path = output_dir / f"business_route_{safe_event}.xlsx"
    dot_path = output_dir / f"business_route_{safe_event}.dot"
    html_path = output_dir / f"business_route_{safe_event}.html"

    # Empty Excel
    df = pd.DataFrame(columns=[
        "event_id", "route_order", "depth", "node_type", "task_name",
        "function_name", "data_name", "data_kind", "action", "next_task",
        "file_path", "line", "confidence", "reason",
    ])
    df.to_excel(excel_path, index=False)

    # Empty DOT
    lines = [
        "digraph business_route {",
        f'    label="Business Route: {safe_event} (empty)";',
        "    rankdir=LR;",
        '    graph [fontname="Arial", fontsize=12];',
        "}",
    ]
    dot_path.write_text("\n".join(lines), encoding="utf-8")

    # Empty HTML
    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>Business Route: {safe_event} (empty)</title>
</head>
<body>
<h1>Business Route: {safe_event}</h1>
<p>No data found for this event.</p>
</body>
</html>"""
    html_path.write_text(html, encoding="utf-8")


def _write_business_route_dot(
    routes: list[dict[str, Any]],
    dot_path: Path,
    event_id: str,
) -> None:
    """Write DOT file for business route visualization."""
    lines = ["digraph business_route {"]
    lines.append(f'    label="Business Route: {event_id}";')
    lines.append("    rankdir=TB;")
    lines.append('    graph [fontname="Arial", fontsize=12];')
    lines.append('    node [fontname="Arial", fontsize=10];')
    lines.append('    edge [fontname="Arial", fontsize=9];')
    lines.append("")

    for route in routes:
        node_id = f"node{route['route_order']}"
        task_name = (route["task_name"] or "").replace('"', '\\"')
        action = route["action"] or ""
        node_type = route["node_type"] or ""
        data_name = (route["data_name"] or "").replace('"', '\\"')
        confidence = route["confidence"] or 0.0

        if node_type == "EVENT":
            if action == "START":
                lines.append(
                    f'    {node_id} [label="START\\n{event_id}", shape=ellipse, '
                    f'style=filled, fillcolor=lightgreen];'
                )
            elif action == "END":
                lines.append(
                    f'    {node_id} [label="END\\n{event_id}", shape=ellipse, '
                    f'style=filled, fillcolor=lightcoral];'
                )
        elif node_type == "TASK":
            lines.append(
                f'    {node_id} [label="TASK\\n{task_name}\\n{action}", '
                f'shape=box, style=filled, fillcolor=lightblue];'
            )
        elif node_type == "MESSAGE":
            lines.append(
                f'    {node_id} [label="MSG\\n{data_name}\\n{action}", '
                f'shape=note, style=filled, fillcolor=lightyellow];'
            )
        elif node_type == "FILE":
            lines.append(
                f'    {node_id} [label="FILE\\n{data_name}\\n{action}", '
                f'shape=folder, style=filled, fillcolor=lightsalmon];'
            )
        elif node_type == "DB":
            lines.append(
                f'    {node_id} [label="DB\\n{data_name}\\n{action}", '
                f'shape=cylinder, style=filled, fillcolor=lightskyblue];'
            )
        elif node_type == "SHARED_MEMORY":
            lines.append(
                f'    {node_id} [label="SHM\\n{data_name}\\n{action}", '
                f'shape=box3d, style=filled, fillcolor=plum];'
            )
        else:
            lines.append(
                f'    {node_id} [label="{node_type}\\n{data_name}\\n{action}", '
                f'shape=box, style=filled, fillcolor=grey90];'
            )

    # Connect nodes in sequence
    lines.append("")
    for i in range(len(routes) - 1):
        current = f"node{routes[i]['route_order']}"
        next_node = f"node{routes[i + 1]['route_order']}"
        conf = routes[i].get("confidence", 0) or 0.0
        lines.append(
            f'    {current} -> {next_node} '
            f'[label="conf={conf:.2f}", fontsize=8];'
        )

    lines.append("}")
    dot_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"DOT written: {dot_path}")


def _write_business_route_html(
    routes: list[dict[str, Any]],
    html_path: Path,
    event_id: str,
) -> None:
    """Write HTML report for business route visualization."""
    lines = ["<!DOCTYPE html>"]
    lines.append('<html lang="ja">')
    lines.append("<head>")
    lines.append('<meta charset="UTF-8">')
    lines.append("<title>Business Route: {}</title>".format(event_id))
    lines.append("<style>")
    lines.append("""
body { font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }
h1 { color: #333; }
table { border-collapse: collapse; width: 100%; background: white; box-shadow: 0 1px 3px rgba(0,0,0,0.2); }
th, td { border: 1px solid #ddd; padding: 8px; text-align: left; font-size: 13px; }
th { background: #4CAF50; color: white; font-weight: bold; }
tr:nth-child(even) { background: #f9f9f9; }
tr:hover { background: #f1f1f1; }
.node-type-EVENT { background: #c8e6c9 !important; }
.node-type-TASK { background: #bbdefb !important; }
.node-type-MESSAGE { background: #fff9c4 !important; }
.node-type-FILE { background: #ffccbc !important; }
.node-type-DB { background: #b3e5fc !important; }
.node-type-SHARED_MEMORY { background: #e1bee7 !important; }
.node-type-UNKNOWN { background: #eeeeee !important; }
.summary { background: white; padding: 15px; margin-bottom: 20px; border-radius: 5px; box-shadow: 0 1px 3px rgba(0,0,0,0.2); }
.unknown-section { background: white; padding: 15px; margin-top: 20px; border-radius: 5px; box-shadow: 0 1px 3px rgba(0,0,0,0.2); }
.badge { display: inline-block; padding: 2px 6px; border-radius: 3px; font-size: 11px; color: white; }
.badge-high { background: #4CAF50; }
.badge-medium { background: #FF9800; }
.badge-low { background: #f44336; }
""")
    lines.append("</style>")
    lines.append("</head>")
    lines.append("<body>")

    # Title
    lines.append(f"<h1>Business Route: {event_id}</h1>")

    # Summary
    lines.append('<div class="summary">')
    lines.append(f"<p><strong>Event ID:</strong> {event_id}</p>")
    lines.append(f"<p><strong>Total steps:</strong> {len(routes)}</p>")
    tasks = set(r["task_name"] for r in routes if r["task_name"])
    lines.append(f"<p><strong>Tasks involved:</strong> {', '.join(sorted(tasks)) if tasks else 'N/A'}</p>")
    unknowns = [r for r in routes if r["data_name"] == UNKNOWN or r["task_name"] == UNKNOWN]
    lines.append(f"<p><strong>UNKNOWN entries:</strong> {len(unknowns)}</p>")
    low_conf = [r for r in routes if (r["confidence"] or 0) < 0.7]
    lines.append(f"<p><strong>Low confidence entries (<0.7):</strong> {len(low_conf)}</p>")
    lines.append("</div>")

    # Route Table
    lines.append('<table>')
    lines.append('<thead><tr>')
    headers = ["#", "Depth", "Type", "Task", "Function", "Data", "Action", "Next", "File", "Line", "Confidence", "Reason"]
    for h in headers:
        lines.append(f"<th>{h}</th>")
    lines.append('</tr></thead>')
    lines.append('<tbody>')

    for route in routes:
        node_type = route["node_type"] or ""
        task_name = route["task_name"] or ""
        function_name = route["function_name"] or ""
        data_name = route["data_name"] or ""
        action = route["action"] or ""
        next_task = route["next_task"] or ""
        file_path = route["file_path"] or ""
        line = route["line"] if route["line"] is not None else ""
        confidence = route["confidence"] or 0.0
        reason = (route["reason"] or "")[:120]

        conf_class = "badge-high" if confidence >= 0.8 else ("badge-medium" if confidence >= 0.5 else "badge-low")
        tr_class = f"node-type-{node_type}" if node_type in ("EVENT", "TASK", "MESSAGE", "FILE", "DB", "SHARED_MEMORY", "UNKNOWN") else ""

        lines.append(f'<tr class="{tr_class}">')
        lines.append(f"<td>{route['route_order']}</td>")
        lines.append(f"<td>{route['depth']}</td>")
        lines.append(f"<td>{node_type}</td>")
        lines.append(f"<td>{task_name}</td>")
        lines.append(f"<td>{function_name}</td>")
        lines.append(f"<td>{data_name}</td>")
        lines.append(f"<td>{action}</td>")
        lines.append(f"<td>{next_task}</td>")
        lines.append(f"<td style='font-size:11px'>{file_path}</td>")
        lines.append(f"<td>{line}</td>")
        lines.append(f'<td><span class="badge {conf_class}">{confidence:.2f}</span></td>')
        lines.append(f"<td style='font-size:11px'>{reason}</td>")
        lines.append("</tr>")

    lines.append("</tbody></table>")

    # Unknown section
    if unknowns:
        lines.append('<div class="unknown-section">')
        lines.append("<h2>UNKNOWN Entries (Requires Investigation)</h2>")
        lines.append("<ul>")
        for u in unknowns[:20]:  # Limit to 20
            lines.append(f"<li>Route #{u['route_order']}: {u['node_type']} - {u['action']} (conf={u['confidence']:.2f})</li>")
        if len(unknowns) > 20:
            lines.append(f"<li>... and {len(unknowns) - 20} more</li>")
        lines.append("</ul>")
        lines.append("</div>")

    # Low confidence section
    if low_conf:
        lines.append('<div class="unknown-section">')
        lines.append("<h2>Low Confidence Items (<0.7)</h2>")
        lines.append("<ul>")
        for lc in low_conf[:20]:
            lines.append(f"<li>Route #{lc['route_order']}: {lc['task_name']} / {lc['data_name']} (conf={lc['confidence']:.2f})</li>")
        if len(low_conf) > 20:
            lines.append(f"<li>... and {len(low_conf) - 20} more</li>")
        lines.append("</ul>")
        lines.append("</div>")

    lines.append("</body>")
    lines.append("</html>")

    html_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"HTML written: {html_path}")
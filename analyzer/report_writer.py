from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from config import DB_PATH, OUTPUT_DIR

UNKNOWN = "UNKNOWN"


def _safe_filename_part(value: str) -> str:
    import re
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value.strip())
    cleaned = cleaned.strip(" .")
    return cleaned or "UNKNOWN"


def _normalize(value: str | None) -> str:
    if not value or not value.strip():
        return UNKNOWN
    return value.strip()


def generate_report(
    event_id: str,
    db_path: Path = DB_PATH,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    if output_dir is None:
        output_dir = db_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row

        safe_event = _safe_filename_part(event_id)
        html_path = output_dir / f"report_{safe_event}.html"

        # Gather data
        event_info = _get_event_info(conn, event_id)
        task_graph_data = _get_task_graph(conn, event_id)
        event_chain_data = _get_event_chains(conn, event_id)
        data_flow_data = _get_data_flows(conn, event_id)
        business_route_data = _get_business_routes(conn, event_id)
        middleware_data = _get_middleware_calls(conn, event_id)
        unknown_summary = _get_unknown_summary(conn, event_id)

        html = _build_html(
            event_id=event_id,
            event_info=event_info,
            task_graph=task_graph_data,
            event_chain=event_chain_data,
            data_flow=data_flow_data,
            business_route=business_route_data,
            middleware=middleware_data,
            unknown=unknown_summary,
        )

        html_path.write_text(html, encoding="utf-8")
        print(f"Report written: {html_path}")

    return {
        "html_path": html_path,
        "count": len(business_route_data),
    }


def _get_event_info(conn: sqlite3.Connection, event_id: str) -> dict[str, Any]:
    info: dict[str, Any] = {
        "event_id": event_id,
        "task_count": 0,
        "message_count": 0,
        "data_access_count": 0,
        "chain_count": 0,
        "flow_count": 0,
        "route_count": 0,
    }

    try:
        info["task_count"] = conn.execute(
            "SELECT COUNT(DISTINCT from_task) + COUNT(DISTINCT to_task) FROM message_edges WHERE event_id = ?",
            (event_id,),
        ).fetchone()[0] or 0
    except sqlite3.OperationalError:
        pass

    try:
        info["message_count"] = conn.execute(
            "SELECT COUNT(*) FROM message_edges WHERE event_id = ?", (event_id,)
        ).fetchone()[0] or 0
    except sqlite3.OperationalError:
        pass

    try:
        info["chain_count"] = conn.execute(
            "SELECT COUNT(*) FROM event_chains WHERE event_id = ?", (event_id,)
        ).fetchone()[0] or 0
    except sqlite3.OperationalError:
        pass

    try:
        info["flow_count"] = conn.execute(
            "SELECT COUNT(*) FROM data_flow_edges WHERE event_id = ?", (event_id,)
        ).fetchone()[0] or 0
    except sqlite3.OperationalError:
        pass

    try:
        info["route_count"] = conn.execute(
            "SELECT COUNT(*) FROM business_routes WHERE event_id = ?", (event_id,)
        ).fetchone()[0] or 0
    except sqlite3.OperationalError:
        pass

    return info


def _get_task_graph(conn: sqlite3.Connection, event_id: str) -> list[dict[str, Any]]:
    try:
        rows = conn.execute(
            """
            SELECT event_id, from_task, to_task, message_id, data_name,
                   caller_function, line, confidence, edge_reason
            FROM task_edges
            WHERE event_id = ?
            ORDER BY line
            """,
            (event_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.OperationalError:
        return []


def _get_event_chains(conn: sqlite3.Connection, event_id: str) -> list[dict[str, Any]]:
    try:
        rows = conn.execute(
            """
            SELECT event_id, depth, route_order, from_task, to_task,
                   message_id, data_name, caller_function, file_path,
                   line, confidence
            FROM event_chains
            WHERE event_id = ?
            ORDER BY route_order, depth
            """,
            (event_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.OperationalError:
        return []


def _get_data_flows(conn: sqlite3.Connection, event_id: str) -> list[dict[str, Any]]:
    try:
        rows = conn.execute(
            """
            SELECT event_id, from_task, to_task, from_function, to_function,
                   data_name, data_kind, direction, access_type, source_type,
                   line, confidence, reason
            FROM data_flow_edges
            WHERE event_id = ?
            ORDER BY line
            """,
            (event_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.OperationalError:
        return []


def _get_business_routes(conn: sqlite3.Connection, event_id: str) -> list[dict[str, Any]]:
    try:
        rows = conn.execute(
            """
            SELECT event_id, route_order, depth, node_type, task_name,
                   function_name, data_name, data_kind, action, next_task,
                   file_path, line, confidence, reason
            FROM business_routes
            WHERE event_id = ?
            ORDER BY route_order
            """,
            (event_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.OperationalError:
        return []


def _get_middleware_calls(conn: sqlite3.Connection, event_id: str) -> list[dict[str, Any]]:
    try:
        rows = conn.execute(
            """
            SELECT files.path, middleware_calls.caller_name,
                   middleware_calls.callee_name, middleware_calls.middleware_type,
                   middleware_calls.line, middleware_calls.confidence,
                   middleware_calls.call_text
            FROM middleware_calls
            JOIN files ON middleware_calls.file_id = files.id
            WHERE middleware_calls.middleware_type IN ('message_send', 'message_recv')
               AND middleware_calls.file_id IN (
                   SELECT DISTINCT file_id FROM message_edges WHERE event_id = ?
               )
            ORDER BY middleware_calls.line
            """,
            (event_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.OperationalError:
        return []


def _get_unknown_summary(conn: sqlite3.Connection, event_id: str) -> dict[str, Any]:
    unknowns: dict[str, Any] = {
        "task_graph": 0,
        "event_chain": 0,
        "data_flow": 0,
        "business_route": 0,
    }

    try:
        unknowns["task_graph"] = conn.execute(
            "SELECT COUNT(*) FROM task_edges WHERE event_id = ? AND (from_task = ? OR to_task = ?)",
            (event_id, UNKNOWN, UNKNOWN),
        ).fetchone()[0] or 0
    except sqlite3.OperationalError:
        pass

    try:
        unknowns["event_chain"] = conn.execute(
            "SELECT COUNT(*) FROM event_chains WHERE event_id = ? AND (from_task = ? OR to_task = ?)",
            (event_id, UNKNOWN, UNKNOWN),
        ).fetchone()[0] or 0
    except sqlite3.OperationalError:
        pass

    try:
        unknowns["data_flow"] = conn.execute(
            "SELECT COUNT(*) FROM data_flow_edges WHERE event_id = ? AND (data_name = ? OR from_task = ? OR to_task = ?)",
            (event_id, UNKNOWN, UNKNOWN, UNKNOWN),
        ).fetchone()[0] or 0
    except sqlite3.OperationalError:
        pass

    try:
        unknowns["business_route"] = conn.execute(
            "SELECT COUNT(*) FROM business_routes WHERE event_id = ? AND (data_name = ? OR task_name = ?)",
            (event_id, UNKNOWN, UNKNOWN),
        ).fetchone()[0] or 0
    except sqlite3.OperationalError:
        pass

    return unknowns


def _build_html(
    event_id: str,
    event_info: dict[str, Any],
    task_graph: list[dict[str, Any]],
    event_chain: list[dict[str, Any]],
    data_flow: list[dict[str, Any]],
    business_route: list[dict[str, Any]],
    middleware: list[dict[str, Any]],
    unknown: dict[str, Any],
) -> str:
    lines = ["<!DOCTYPE html>"]
    lines.append('<html lang="ja">')
    lines.append("<head>")
    lines.append('<meta charset="UTF-8">')
    lines.append(f"<title>Analysis Report: {event_id}</title>")
    lines.append("""
<style>
* { box-sizing: border-box; }
body { font-family: 'Arial', sans-serif; margin: 0; padding: 20px; background: #f0f2f5; color: #333; }
h1 { color: #1a1a2e; border-bottom: 3px solid #4CAF50; padding-bottom: 10px; }
h2 { color: #16213e; margin-top: 30px; border-left: 4px solid #4CAF50; padding-left: 10px; }
h3 { color: #0f3460; margin-top: 20px; }
table { border-collapse: collapse; width: 100%; background: white; box-shadow: 0 2px 4px rgba(0,0,0,0.1); margin-bottom: 20px; }
th, td { border: 1px solid #e0e0e0; padding: 10px; text-align: left; font-size: 13px; }
th { background: #4CAF50; color: white; font-weight: bold; }
tr:nth-child(even) { background: #f8f9fa; }
tr:hover { background: #e8f5e9; }
.summary-cards { display: flex; flex-wrap: wrap; gap: 15px; margin: 20px 0; }
.card { background: white; border-radius: 8px; padding: 20px; flex: 1; min-width: 150px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); text-align: center; }
.card h3 { margin: 0 0 10px 0; font-size: 14px; color: #666; }
.card .value { font-size: 28px; font-weight: bold; color: #4CAF50; }
.card .value.warning { color: #FF9800; }
.card .value.danger { color: #f44336; }
.badge { display: inline-block; padding: 2px 6px; border-radius: 3px; font-size: 11px; color: white; }
.badge-high { background: #4CAF50; }
.badge-medium { background: #FF9800; }
.badge-low { background: #f44336; }
.node-type-EVENT { background: #c8e6c9; }
.node-type-TASK { background: #bbdefb; }
.node-type-MESSAGE { background: #fff9c4; }
.node-type-FILE { background: #ffccbc; }
.node-type-DB { background: #b3e5fc; }
.node-type-SHARED_MEMORY { background: #e1bee7; }
.collapsible { background: white; border-radius: 8px; margin-bottom: 15px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
.collapsible-header { padding: 15px 20px; cursor: pointer; font-weight: bold; font-size: 16px; display: flex; justify-content: space-between; align-items: center; }
.collapsible-header:hover { background: #f5f5f5; }
.collapsible-content { padding: 0 20px 20px; display: none; }
.collapsible-content.active { display: block; }
.unknown-box { background: #fff3e0; border: 1px solid #ff9800; border-radius: 8px; padding: 15px; margin: 15px 0; }
.unknown-box h3 { color: #e65100; margin-top: 0; }
.source-code { font-family: 'Courier New', monospace; font-size: 11px; background: #f5f5f5; padding: 8px; border-radius: 4px; overflow-x: auto; }
</style>
<script>
document.addEventListener('DOMContentLoaded', function() {
    document.querySelectorAll('.collapsible-header').forEach(function(header) {
        header.addEventListener('click', function() {
            var content = this.nextElementSibling;
            content.classList.toggle('active');
        });
    });
});
</script>
""")
    lines.append("</head>")
    lines.append("<body>")

    # Title
    lines.append(f"<h1>Analysis Report: {event_id}</h1>")

    # Summary cards
    lines.append('<div class="summary-cards">')
    metrics = [
        ("Tasks", str(event_info["task_count"]), ""),
        ("Messages", str(event_info["message_count"]), ""),
        ("Task Graph Edges", str(event_info["task_count"]), ""),
        ("Event Chain Records", str(event_info["chain_count"]), ""),
        ("Data Flow Edges", str(event_info["flow_count"]), ""),
        ("Business Route Steps", str(event_info["route_count"]), ""),
    ]
    for label, value, extra_class in metrics:
        lines.append(f'<div class="card"><h3>{label}</h3><div class="value {extra_class}">{value}</div></div>')
    lines.append("</div>")

    # Unknown summary
    total_unknown = sum(unknown.values())
    lines.append('<div class="unknown-box">')
    lines.append("<h3>UNKNOWN Summary (Requires Investigation)</h3>")
    lines.append(f"<p>Total UNKNOWN entries: <strong>{total_unknown}</strong></p>")
    lines.append("<ul>")
    for key, val in unknown.items():
        if val > 0:
            lines.append(f"<li>{key}: {val} entries</li>")
    if total_unknown == 0:
        lines.append("<li>No UNKNOWN entries found</li>")
    lines.append("</ul>")
    lines.append("</div>")

    # ====== 1. Task Graph ======
    lines.append('<div class="collapsible">')
    lines.append('<div class="collapsible-header" onclick="this.nextElementSibling.classList.toggle(\'active\')">📊 Task Graph <span>▼</span></div>')
    lines.append('<div class="collapsible-content">')
    _write_html_table(lines, task_graph, [
        ("event_id", "Event"), ("from_task", "From"), ("to_task", "To"),
        ("message_id", "Message"), ("data_name", "Data"),
        ("caller_function", "Caller"), ("line", "Line"),
        ("confidence", "Confidence"),
    ])
    lines.append("</div></div>")

    # ====== 2. Event Chain ======
    lines.append('<div class="collapsible">')
    lines.append('<div class="collapsible-header" onclick="this.nextElementSibling.classList.toggle(\'active\')">🔗 Event Chain <span>▼</span></div>')
    lines.append('<div class="collapsible-content">')
    _write_html_table(lines, event_chain, [
        ("route_order", "#"), ("depth", "Depth"), ("from_task", "From"),
        ("to_task", "To"), ("message_id", "Message"), ("data_name", "Data"),
        ("caller_function", "Caller"), ("file_path", "File"), ("line", "Line"),
        ("confidence", "Confidence"),
    ])
    lines.append("</div></div>")

    # ====== 3. Data Flow ======
    lines.append('<div class="collapsible">')
    lines.append('<div class="collapsible-header" onclick="this.nextElementSibling.classList.toggle(\'active\')">💾 Data Flow <span>▼</span></div>')
    lines.append('<div class="collapsible-content">')
    _write_html_table(lines, data_flow, [
        ("from_task", "From"), ("to_task", "To"), ("data_name", "Data"),
        ("data_kind", "Kind"), ("access_type", "Access"), ("source_type", "Source"),
        ("direction", "Dir"), ("from_function", "From Func"),
        ("to_function", "To Func"), ("line", "Line"),
        ("confidence", "Confidence"),
    ])
    lines.append("</div></div>")

    # ====== 4. Business Route ======
    lines.append('<div class="collapsible">')
    lines.append('<div class="collapsible-header" onclick="this.nextElementSibling.classList.toggle(\'active\')">🛣️ Business Route <span>▼</span></div>')
    lines.append('<div class="collapsible-content">')
    _write_html_table(lines, business_route, [
        ("route_order", "#"), ("depth", "Depth"), ("node_type", "Type"),
        ("task_name", "Task"), ("function_name", "Function"),
        ("data_name", "Data"), ("action", "Action"), ("next_task", "Next"),
        ("file_path", "File"), ("line", "Line"),
        ("confidence", "Confidence"), ("reason", "Reason"),
    ])
    lines.append("</div></div>")

    # ====== 5. Middleware Calls ======
    lines.append('<div class="collapsible">')
    lines.append('<div class="collapsible-header" onclick="this.nextElementSibling.classList.toggle(\'active\')">⚙️ Middleware Calls <span>▼</span></div>')
    lines.append('<div class="collapsible-content">')
    _write_html_table(lines, middleware, [
        ("path", "File"), ("caller_name", "Caller"), ("callee_name", "Callee"),
        ("middleware_type", "Type"), ("line", "Line"),
        ("confidence", "Confidence"), ("call_text", "Call Text"),
    ])
    lines.append("</div></div>")

    # ====== 6. Investigation Required ======
    lines.append('<div class="collapsible">')
    lines.append('<div class="collapsible-header" onclick="this.nextElementSibling.classList.toggle(\'active\')" style="color:#e65100">🔍 Investigation Required <span>▼</span></div>')
    lines.append('<div class="collapsible-content">')

    if total_unknown > 0:
        lines.append("<h3>UNKNOWN Entries</h3>")
        lines.append(f"<p>{total_unknown} UNKNOWN entries found across all analysis phases.</p>")
        lines.append("<ul>")
        for key, val in unknown.items():
            if val > 0:
                lines.append(f"<li><strong>{key}</strong>: {val} entries</li>")
        lines.append("</ul>")
    else:
        lines.append("<p>No UNKNOWN entries found.</p>")

    lines.append("<h3>Low Confidence Items</h3>")
    low_conf_count = sum(
        1 for r in business_route if (r.get("confidence") or 0) < 0.7
    )
    if low_conf_count > 0:
        lines.append(f"<p>{low_conf_count} items have confidence below 0.7.</p>")
    else:
        lines.append("<p>No low confidence items found.</p>")

    lines.append("<h3>Recommended Actions</h3>")
    lines.append("<ul>")
    if total_unknown > 0:
        lines.append("<li>Investigate UNKNOWN entries by reviewing source code and middleware_rules.json</li>")
    if low_conf_count > 0:
        lines.append("<li>Review low confidence entries and add more specific middleware rules</li>")
    if not total_unknown and not low_conf_count:
        lines.append("<li>All entries have high confidence. No immediate actions required.</li>")
    lines.append("<li>Validate the business route with domain experts</li>")
    lines.append("<li>Add missing middleware rules for UNKNOWN items</li>")
    lines.append("</ul>")
    lines.append("</div></div>")

    lines.append("</body>")
    lines.append("</html>")

    return "\n".join(lines)


def _write_html_table(
    lines: list[str],
    data: list[dict[str, Any]],
    columns: list[tuple[str, str]],
) -> None:
    if not data:
        lines.append("<p>No data available.</p>")
        return

    lines.append("<table><thead><tr>")
    for _, header in columns:
        lines.append(f"<th>{header}</th>")
    lines.append("</tr></thead><tbody>")

    for row in data[:200]:  # Limit to 200 rows
        lines.append("<tr>")
        for key, _ in columns:
            value = row.get(key)
            if value is None:
                display = ""
            elif isinstance(value, float):
                conf_class = "badge-high" if value >= 0.8 else ("badge-medium" if value >= 0.5 else "badge-low")
                display = f'<span class="badge {conf_class}">{value:.2f}</span>'
            else:
                display = str(value)
            lines.append(f"<td>{display}</td>")
        lines.append("</tr>")

    if len(data) > 200:
        lines.append(f'<tr><td colspan="{len(columns)}" style="text-align:center;font-style:italic;">... and {len(data) - 200} more rows</td></tr>')

    lines.append("</tbody></table>")
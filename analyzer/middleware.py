from __future__ import annotations

import fnmatch
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from config import DB_PATH, MIDDLEWARE_RULES_PATH, MIDDLEWARE_RULES_TEMPLATE_PATH
from analyzer.db_writer import init_middleware_tables


MIDDLEWARE_TYPES = (
    "message_send",
    "message_recv",
    "file_read",
    "file_write",
    "db_read",
    "db_write",
    "shared_read",
    "shared_write",
)

UNKNOWN = "UNKNOWN"

SUGGEST_KEYWORDS = {
    "message_send": ("SEND", "SND", "POST", "PUT", "REQ", "REQUEST", "NOTIFY", "EVENT"),
    "message_recv": ("RECV", "RECEIVE", "RCV", "GET", "WAIT", "DISPATCH"),
    "file_read": ("READ", "LOAD", "OPEN", "INPUT"),
    "file_write": ("WRITE", "SAVE", "OUTPUT", "CLOSE"),
    "db_read": ("SELECT", "FETCH", "FIND", "SEARCH", "GET"),
    "db_write": ("INSERT", "UPDATE", "DELETE", "PUT", "SET"),
    "shared_read": ("SHM", "SHARE", "COMMON", "MEM", "CACHE"),
    "shared_write": ("SHM", "SHARE", "COMMON", "MEM", "CACHE"),
}


def load_rules(path: Path = MIDDLEWARE_RULES_PATH) -> dict[str, list[dict[str, Any]]]:
    if not path.exists():
        print(f"[WARN] middleware rules file not found: {path}")
        return {middleware_type: [] for middleware_type in MIDDLEWARE_TYPES}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[WARN] middleware rules file could not be loaded: {path}: {exc}")
        return {middleware_type: [] for middleware_type in MIDDLEWARE_TYPES}

    rules: dict[str, list[dict[str, Any]]] = {}
    for middleware_type in MIDDLEWARE_TYPES:
        values = data.get(middleware_type, [])
        if not isinstance(values, list):
            print(f"[WARN] middleware rule category is not a list: {middleware_type}")
            values = []
        rules[middleware_type] = [
            rule for rule in values if isinstance(rule, dict) and bool(rule.get("enabled", True))
        ]
    return rules


def match_rule(callee_name: str, rules: dict[str, list[dict[str, Any]]]) -> tuple[str, dict[str, Any], float] | None:
    for middleware_type, rule_list in rules.items():
        for rule in rule_list:
            name = str(rule.get("name", ""))
            match_type = str(rule.get("match_type", "exact")).lower()
            matched = False
            confidence = 0.8

            if match_type == "exact":
                matched = callee_name == name
                confidence = 0.95
            elif match_type == "wildcard":
                matched = fnmatch.fnmatchcase(callee_name, name)
                confidence = 0.75
            elif match_type == "regex":
                try:
                    matched = re.fullmatch(name, callee_name) is not None
                except re.error as exc:
                    print(f"[WARN] invalid regex rule skipped: {name}: {exc}")
                    matched = False
                confidence = 0.8
            else:
                print(f"[WARN] unsupported match_type skipped: {match_type}")

            if matched:
                return middleware_type, rule, confidence
    return None


def list_active_rules(
    rules_path: Path = MIDDLEWARE_RULES_PATH,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    import pandas as pd

    if output_dir is None:
        output_dir = DB_PATH.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    rules = load_rules(rules_path)
    rows: list[dict[str, Any]] = []
    for middleware_type in MIDDLEWARE_TYPES:
        for rule in rules.get(middleware_type, []):
            rows.append(
                {
                    "middleware_type": middleware_type,
                    "match_type": rule.get("match_type", "exact"),
                    "name": rule.get("name", ""),
                    "event_arg_index": rule.get("event_arg_index", ""),
                    "message_arg_index": rule.get("message_arg_index", ""),
                    "from_task_arg_index": rule.get("from_task_arg_index", ""),
                    "to_task_arg_index": rule.get("to_task_arg_index", ""),
                    "data_arg_index": rule.get("data_arg_index", ""),
                    "confidence": rule.get("confidence", ""),
                    "note": rule.get("note", ""),
                }
            )

    columns = [
        "middleware_type",
        "match_type",
        "name",
        "event_arg_index",
        "message_arg_index",
        "from_task_arg_index",
        "to_task_arg_index",
        "data_arg_index",
        "confidence",
        "note",
    ]
    df = pd.DataFrame(rows, columns=columns)
    output_path = output_dir / "active_middleware_rules.xlsx"
    df.to_excel(output_path, index=False)

    counts = {middleware_type: len(rules.get(middleware_type, [])) for middleware_type in MIDDLEWARE_TYPES}
    for middleware_type, count in counts.items():
        print(f"{middleware_type}: {count}")
    if rows:
        print(df.to_string(index=False))
    else:
        print("No active middleware rules found.")

    return {"count": len(rows), "counts": counts, "output_path": output_path}


def split_call_args(call_text: str) -> list[str]:
    start = call_text.find("(")
    if start < 0:
        return []

    depth = 0
    quote: str | None = None
    escape = False
    end = -1
    for index in range(start, len(call_text)):
        char = call_text[index]
        if quote:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                end = index
                break

    if end < 0:
        return []

    args_text = call_text[start + 1 : end].strip()
    if not args_text:
        return []

    args: list[str] = []
    current: list[str] = []
    depth = 0
    quote = None
    escape = False
    for char in args_text:
        if quote:
            current.append(char)
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
            current.append(char)
        elif char in "([{":
            depth += 1
            current.append(char)
        elif char in ")]}":
            depth = max(0, depth - 1)
            current.append(char)
        elif char == "," and depth == 0:
            args.append("".join(current).strip() or UNKNOWN)
            current = []
        else:
            current.append(char)

    args.append("".join(current).strip() or UNKNOWN)
    return args


def arg_value(args: list[str], rule: dict[str, Any], key: str) -> str:
    index = rule.get(key)
    if index is None:
        return UNKNOWN
    try:
        index_int = int(index)
    except (TypeError, ValueError):
        return UNKNOWN
    if 0 <= index_int < len(args):
        return args[index_int] or UNKNOWN
    return UNKNOWN


def first_arg_value(args: list[str], rule: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = arg_value(args, rule, key)
        if value != UNKNOWN:
            return value
    return UNKNOWN


def adjusted_confidence(base: float, args: list[str], required_values: list[str]) -> float:
    if not args:
        return min(base, 0.55)
    missing = sum(1 for value in required_values if value == UNKNOWN)
    if missing:
        return max(0.4, base - (missing * 0.08))
    return base


def data_kind_and_direction(middleware_type: str) -> tuple[str, str]:
    kind, direction = middleware_type.split("_", 1)
    return kind, direction


def insert_middleware_call(
    conn: sqlite3.Connection,
    call: sqlite3.Row,
    middleware_type: str,
    args: list[str],
    confidence: float,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO middleware_calls(
            file_id, caller_name, callee_name, middleware_type, line,
            call_text, extracted_args, confidence
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            call["file_id"],
            call["caller_name"] or UNKNOWN,
            call["callee_name"] or UNKNOWN,
            middleware_type,
            call["line"],
            call["call_text"] or "",
            json.dumps(args, ensure_ascii=False),
            confidence,
        ),
    )
    return int(cur.lastrowid)


def insert_message_edge(
    conn: sqlite3.Connection,
    middleware_call_id: int,
    call: sqlite3.Row,
    middleware_type: str,
    rule: dict[str, Any],
    args: list[str],
    confidence: float,
) -> None:
    caller = call["caller_name"] or UNKNOWN
    event_id = first_arg_value(args, rule, ("event_arg_index", "event_id_arg_index"))
    message_id = first_arg_value(args, rule, ("message_arg_index", "message_id_arg_index", "event_arg_index"))
    data_name = first_arg_value(args, rule, ("data_arg_index", "data_name_arg_index"))

    if middleware_type == "message_send":
        from_task = first_arg_value(args, rule, ("from_task_arg_index",))
        if from_task == UNKNOWN:
            from_task = caller
        to_task = first_arg_value(args, rule, ("to_task_arg_index", "task_arg_index"))
    else:
        from_task = first_arg_value(args, rule, ("from_task_arg_index", "task_arg_index"))
        to_task = first_arg_value(args, rule, ("to_task_arg_index",))
        if to_task == UNKNOWN:
            to_task = caller

    confidence = adjusted_confidence(confidence, args, [event_id, message_id, from_task, to_task, data_name])
    conn.execute(
        """
        INSERT INTO message_edges(
            middleware_call_id, file_id, caller_name, callee_name, event_id,
            message_id, from_task, to_task, data_name, line, raw_call_text, confidence
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            middleware_call_id,
            call["file_id"],
            caller,
            call["callee_name"] or UNKNOWN,
            event_id,
            message_id,
            from_task,
            to_task,
            data_name,
            call["line"],
            call["call_text"] or "",
            confidence,
        ),
    )


def insert_data_access(
    conn: sqlite3.Connection,
    middleware_call_id: int,
    call: sqlite3.Row,
    middleware_type: str,
    rule: dict[str, Any],
    args: list[str],
    confidence: float,
) -> None:
    data_kind, direction = data_kind_and_direction(middleware_type)
    data_name = first_arg_value(
        args,
        rule,
        ("data_arg_index", "data_name_arg_index", "path_arg_index", "table_arg_index", "name_arg_index"),
    )
    caller = call["caller_name"] or UNKNOWN
    confidence = adjusted_confidence(confidence, args, [data_name])

    conn.execute(
        """
        INSERT INTO data_accesses(
            middleware_call_id, task_name, function_name, access_type, data_kind,
            data_name, direction, file_id, line, raw_call_text, confidence
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            middleware_call_id,
            caller,
            caller,
            middleware_type,
            data_kind,
            data_name,
            direction,
            call["file_id"],
            call["line"],
            call["call_text"] or "",
            confidence,
        ),
    )


def analyze_middleware(
    db_path: Path = DB_PATH,
    rules_path: Path = MIDDLEWARE_RULES_PATH,
    reset: bool = True,
) -> dict[str, int]:
    rules = load_rules(rules_path)
    if not any(rules.values()):
        print("[WARN] middleware_rules.json has no active rules. Output tables will be empty.")

    counts = {"middleware_calls": 0, "message_edges": 0, "data_accesses": 0}
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        init_middleware_tables(conn, reset=reset)
        try:
            calls = conn.execute(
                """
                SELECT file_id, caller_name, callee_name, line, call_text
                FROM calls
                ORDER BY file_id, line
                """
            ).fetchall()
        except sqlite3.OperationalError as exc:
            print(f"[WARN] calls table is not available. Run analyze first: {exc}")
            conn.commit()
            return counts

        for call in calls:
            callee_name = call["callee_name"] or ""
            matched = match_rule(callee_name, rules)
            if matched is None:
                continue

            middleware_type, rule, confidence = matched
            args = split_call_args(call["call_text"] or "")
            middleware_call_id = insert_middleware_call(conn, call, middleware_type, args, confidence)
            counts["middleware_calls"] += 1

            if middleware_type in {"message_send", "message_recv"}:
                insert_message_edge(conn, middleware_call_id, call, middleware_type, rule, args, confidence)
                counts["message_edges"] += 1
            else:
                insert_data_access(conn, middleware_call_id, call, middleware_type, rule, args, confidence)
                counts["data_accesses"] += 1

        conn.commit()

    return counts


def wildcard_name(keyword: str) -> str:
    return f"*{keyword.title()}*"


def suggest_rules(db_path: Path = DB_PATH, output_dir: Path | None = None) -> dict[str, int]:
    import pandas as pd

    if output_dir is None:
        output_dir = db_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(db_path) as conn:
        try:
            summary_df = pd.read_sql_query(
                """
                SELECT callee_name, COUNT(*) AS count
                FROM calls
                WHERE callee_name IS NOT NULL AND callee_name != ''
                GROUP BY callee_name
                ORDER BY count DESC, callee_name
                """,
                conn,
            )
        except Exception as exc:
            print(f"[WARN] calls table could not be summarized. Run analyze first: {exc}")
            empty_summary = pd.DataFrame(columns=["callee_name", "count"])
            empty_candidates = pd.DataFrame(
                columns=[
                    "suggested_type",
                    "callee_name",
                    "count",
                    "reason_keyword",
                    "recommended_match_type",
                    "recommended_rule_name",
                ]
            )
            empty_summary.to_excel(output_dir / "callee_summary.xlsx", index=False)
            empty_candidates.to_excel(output_dir / "middleware_rule_candidates.xlsx", index=False)
            return {"callee_summary": 0, "middleware_rule_candidates": 0}

    summary_df.to_excel(output_dir / "callee_summary.xlsx", index=False)

    candidates: list[dict[str, Any]] = []
    wildcard_groups: dict[tuple[str, str], dict[str, Any]] = {}

    for _, row in summary_df.iterrows():
        callee_name = str(row["callee_name"])
        count = int(row["count"])
        upper_name = callee_name.upper()

        for suggested_type, keywords in SUGGEST_KEYWORDS.items():
            for keyword in keywords:
                if keyword not in upper_name:
                    continue

                candidates.append(
                    {
                        "suggested_type": suggested_type,
                        "callee_name": callee_name,
                        "count": count,
                        "reason_keyword": keyword,
                        "recommended_match_type": "exact",
                        "recommended_rule_name": callee_name,
                    }
                )

                group_key = (suggested_type, keyword)
                group = wildcard_groups.setdefault(
                    group_key,
                    {
                        "suggested_type": suggested_type,
                        "callee_names": set(),
                        "count": 0,
                        "reason_keyword": keyword,
                        "recommended_match_type": "wildcard",
                        "recommended_rule_name": wildcard_name(keyword),
                    },
                )
                group["callee_names"].add(callee_name)
                group["count"] += count
                break

    for group in wildcard_groups.values():
        if len(group["callee_names"]) < 2:
            continue
        candidates.append(
            {
                "suggested_type": group["suggested_type"],
                "callee_name": ", ".join(sorted(group["callee_names"])[:5]),
                "count": group["count"],
                "reason_keyword": group["reason_keyword"],
                "recommended_match_type": group["recommended_match_type"],
                "recommended_rule_name": group["recommended_rule_name"],
            }
        )

    candidate_df = pd.DataFrame(
        candidates,
        columns=[
            "suggested_type",
            "callee_name",
            "count",
            "reason_keyword",
            "recommended_match_type",
            "recommended_rule_name",
        ],
    )
    if not candidate_df.empty:
        candidate_df = candidate_df.sort_values(
            ["suggested_type", "count", "callee_name"],
            ascending=[True, False, True],
        )
    candidate_df.to_excel(output_dir / "middleware_rule_candidates.xlsx", index=False)

    print(summary_df.to_string(index=False))
    return {"callee_summary": len(summary_df), "middleware_rule_candidates": len(candidate_df)}


def default_arg_indexes(suggested_type: str) -> dict[str, int]:
    if suggested_type == "message_send":
        return {
            "event_arg_index": 0,
            "message_arg_index": 1,
            "to_task_arg_index": 2,
            "data_arg_index": 3,
        }
    if suggested_type == "message_recv":
        return {
            "event_arg_index": 0,
            "message_arg_index": 1,
            "from_task_arg_index": 2,
            "data_arg_index": 3,
        }
    if suggested_type in {"file_read", "file_write"}:
        return {"path_arg_index": 0}
    if suggested_type in {"db_read", "db_write"}:
        return {"table_arg_index": 0}
    if suggested_type in {"shared_read", "shared_write"}:
        return {"data_arg_index": 0}
    return {}


def generate_rules_template(
    candidates_path: Path | None = None,
    output_path: Path = MIDDLEWARE_RULES_TEMPLATE_PATH,
) -> dict[str, int]:
    import pandas as pd

    if candidates_path is None:
        candidates_path = DB_PATH.parent / "middleware_rule_candidates.xlsx"

    template: dict[str, list[dict[str, Any]]] = {middleware_type: [] for middleware_type in MIDDLEWARE_TYPES}
    if not candidates_path.exists():
        print(f"[WARN] rule candidates Excel not found: {candidates_path}")
        output_path.write_text(json.dumps(template, ensure_ascii=False, indent=2), encoding="utf-8")
        return {middleware_type: 0 for middleware_type in MIDDLEWARE_TYPES}

    try:
        candidate_df = pd.read_excel(candidates_path)
    except Exception as exc:
        print(f"[WARN] rule candidates Excel could not be loaded: {candidates_path}: {exc}")
        output_path.write_text(json.dumps(template, ensure_ascii=False, indent=2), encoding="utf-8")
        return {middleware_type: 0 for middleware_type in MIDDLEWARE_TYPES}

    required_columns = {"suggested_type", "recommended_match_type", "recommended_rule_name"}
    missing_columns = required_columns - set(candidate_df.columns)
    if missing_columns:
        print(f"[WARN] rule candidates Excel is missing columns: {', '.join(sorted(missing_columns))}")
        output_path.write_text(json.dumps(template, ensure_ascii=False, indent=2), encoding="utf-8")
        return {middleware_type: 0 for middleware_type in MIDDLEWARE_TYPES}

    seen: set[tuple[str, str, str]] = set()
    for _, row in candidate_df.iterrows():
        suggested_type = str(row.get("suggested_type", "")).strip()
        if suggested_type not in template:
            continue

        match_type = str(row.get("recommended_match_type", "exact")).strip() or "exact"
        name = str(row.get("recommended_rule_name", "")).strip()
        if not name or name.lower() == "nan":
            continue

        key = (suggested_type, match_type, name)
        if key in seen:
            continue
        seen.add(key)

        rule: dict[str, Any] = {
            "enabled": False,
            "match_type": match_type,
            "name": name,
        }
        rule.update(default_arg_indexes(suggested_type))
        rule["note"] = "candidate from suggest-rules"
        template[suggested_type].append(rule)

    output_path.write_text(json.dumps(template, ensure_ascii=False, indent=2), encoding="utf-8")
    return {middleware_type: len(rules) for middleware_type, rules in template.items()}


def safe_filename_part(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value.strip())
    cleaned = cleaned.strip(" .")
    return cleaned or "UNKNOWN"


def inspect_calls(
    callee_name: str,
    db_path: Path = DB_PATH,
    output_dir: Path | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    import pandas as pd

    if output_dir is None:
        output_dir = db_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        try:
            calls = conn.execute(
                """
                SELECT calls.callee_name, calls.caller_name, files.path,
                       calls.line, calls.call_text
                FROM calls JOIN files ON calls.file_id = files.id
                WHERE calls.callee_name = ?
                ORDER BY files.path, calls.line
                LIMIT ?
                """,
                (callee_name, limit),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            print(f"[WARN] calls table could not be inspected. Run analyze first: {exc}")
            calls = []

    for call in calls:
        args = split_call_args(call["call_text"] or "")
        row = {
            "callee_name": call["callee_name"] or UNKNOWN,
            "caller_name": call["caller_name"] or UNKNOWN,
            "path": call["path"] or UNKNOWN,
            "line": call["line"],
            "call_text": call["call_text"] or "",
            "extracted_args": json.dumps(args, ensure_ascii=False),
        }
        for index in range(6):
            row[f"arg{index}"] = args[index] if index < len(args) else UNKNOWN
        rows.append(row)

    columns = [
        "callee_name",
        "caller_name",
        "path",
        "line",
        "call_text",
        "extracted_args",
        "arg0",
        "arg1",
        "arg2",
        "arg3",
        "arg4",
        "arg5",
    ]
    df = pd.DataFrame(rows, columns=columns)
    output_path = output_dir / f"inspect_calls_{safe_filename_part(callee_name)}.xlsx"
    df.to_excel(output_path, index=False)

    if rows:
        print(df.to_string(index=False))
    else:
        print(f"No calls found for callee_name: {callee_name}")

    return {"count": len(rows), "output_path": output_path}


def event_route(
    event_id: str,
    db_path: Path = DB_PATH,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    import pandas as pd

    if output_dir is None:
        output_dir = db_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    route_rows: list[dict[str, Any]] = []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        try:
            message_rows = conn.execute(
                """
                SELECT message_edges.event_id, message_edges.from_task,
                       message_edges.to_task, message_edges.caller_name,
                       message_edges.data_name, files.path AS file_path,
                       message_edges.line, message_edges.confidence,
                       message_edges.raw_call_text
                FROM message_edges JOIN files ON message_edges.file_id = files.id
                WHERE message_edges.event_id = ?
                ORDER BY message_edges.line, files.path
                """,
                (event_id,),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            print(f"[WARN] message_edges table could not be inspected. Run middleware first: {exc}")
            message_rows = []

        for message in message_rows:
            route_rows.append(
                {
                    "event_id": message["event_id"] or UNKNOWN,
                    "from_task": message["from_task"] or UNKNOWN,
                    "to_task": message["to_task"] or UNKNOWN,
                    "caller_function": message["caller_name"] or UNKNOWN,
                    "data_name": message["data_name"] or UNKNOWN,
                    "file_path": message["file_path"] or UNKNOWN,
                    "line": message["line"],
                    "confidence": message["confidence"],
                    "raw_call_text": message["raw_call_text"] or "",
                }
            )

            try:
                data_rows = conn.execute(
                    """
                    SELECT data_accesses.task_name, data_accesses.function_name,
                           data_accesses.data_name, files.path AS file_path,
                           data_accesses.line, data_accesses.confidence,
                           data_accesses.raw_call_text
                    FROM data_accesses JOIN files ON data_accesses.file_id = files.id
                    WHERE data_accesses.function_name IN (?, ?)
                       OR data_accesses.task_name IN (?, ?)
                       OR (
                            data_accesses.data_name = ?
                            AND data_accesses.data_name IS NOT NULL
                            AND data_accesses.data_name != ''
                            AND data_accesses.data_name != ?
                       )
                    ORDER BY data_accesses.line, files.path
                    """,
                    (
                        message["caller_name"] or UNKNOWN,
                        message["to_task"] or UNKNOWN,
                        message["caller_name"] or UNKNOWN,
                        message["to_task"] or UNKNOWN,
                        message["data_name"] or UNKNOWN,
                        UNKNOWN,
                    ),
                ).fetchall()
            except sqlite3.OperationalError:
                data_rows = []

            for data in data_rows:
                route_rows.append(
                    {
                        "event_id": event_id,
                        "from_task": message["from_task"] or UNKNOWN,
                        "to_task": data["task_name"] or message["to_task"] or UNKNOWN,
                        "caller_function": data["function_name"] or UNKNOWN,
                        "data_name": data["data_name"] or UNKNOWN,
                        "file_path": data["file_path"] or UNKNOWN,
                        "line": data["line"],
                        "confidence": data["confidence"],
                        "raw_call_text": data["raw_call_text"] or "",
                    }
                )

    route_rows = sorted(route_rows, key=lambda row: (row["line"] if row["line"] is not None else 0, row["file_path"]))
    for index, row in enumerate(route_rows, start=1):
        row["route_order"] = index

    columns = [
        "route_order",
        "event_id",
        "from_task",
        "to_task",
        "caller_function",
        "data_name",
        "file_path",
        "line",
        "confidence",
        "raw_call_text",
    ]
    df = pd.DataFrame(route_rows, columns=columns)
    output_path = output_dir / f"event_route_{safe_filename_part(event_id)}.xlsx"
    df.to_excel(output_path, index=False)

    if route_rows:
        print(df.to_string(index=False))
    else:
        print(f"0 records found for event_id: {event_id}")

    return {"count": len(route_rows), "output_path": output_path}

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from config import DB_PATH, OUTPUT_DIR


SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    filename TEXT NOT NULL,
    ext TEXT NOT NULL,
    line_count INTEGER NOT NULL,
    size_bytes INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS functions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL,
    name TEXT,
    start_line INTEGER,
    end_line INTEGER,
    signature_text TEXT,
    FOREIGN KEY(file_id) REFERENCES files(id)
);

CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL,
    caller_name TEXT,
    callee_name TEXT,
    line INTEGER,
    call_text TEXT,
    FOREIGN KEY(file_id) REFERENCES files(id)
);

CREATE TABLE IF NOT EXISTS includes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL,
    include_text TEXT,
    include_name TEXT,
    line INTEGER,
    FOREIGN KEY(file_id) REFERENCES files(id)
);

CREATE TABLE IF NOT EXISTS structs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL,
    name TEXT,
    kind TEXT,
    start_line INTEGER,
    end_line INTEGER,
    FOREIGN KEY(file_id) REFERENCES files(id)
);

CREATE TABLE IF NOT EXISTS typedefs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL,
    name TEXT,
    target_text TEXT,
    line INTEGER,
    FOREIGN KEY(file_id) REFERENCES files(id)
);

CREATE TABLE IF NOT EXISTS macros (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL,
    name TEXT,
    macro_text TEXT,
    line INTEGER,
    FOREIGN KEY(file_id) REFERENCES files(id)
);
"""


TABLES = ("files", "functions", "calls", "includes", "structs", "typedefs", "macros")
MIDDLEWARE_TABLES = ("middleware_calls", "message_edges", "data_accesses")


MIDDLEWARE_SCHEMA = """
CREATE TABLE IF NOT EXISTS middleware_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL,
    caller_name TEXT,
    callee_name TEXT,
    middleware_type TEXT NOT NULL,
    line INTEGER,
    call_text TEXT,
    extracted_args TEXT,
    confidence REAL,
    FOREIGN KEY(file_id) REFERENCES files(id)
);

CREATE TABLE IF NOT EXISTS message_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    middleware_call_id INTEGER NOT NULL,
    file_id INTEGER NOT NULL,
    caller_name TEXT,
    callee_name TEXT,
    event_id TEXT,
    message_id TEXT,
    from_task TEXT,
    to_task TEXT,
    data_name TEXT,
    line INTEGER,
    raw_call_text TEXT,
    confidence REAL,
    FOREIGN KEY(middleware_call_id) REFERENCES middleware_calls(id),
    FOREIGN KEY(file_id) REFERENCES files(id)
);

CREATE TABLE IF NOT EXISTS data_accesses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    middleware_call_id INTEGER NOT NULL,
    task_name TEXT,
    function_name TEXT,
    access_type TEXT,
    data_kind TEXT,
    data_name TEXT,
    direction TEXT,
    file_id INTEGER NOT NULL,
    line INTEGER,
    raw_call_text TEXT,
    confidence REAL,
    FOREIGN KEY(middleware_call_id) REFERENCES middleware_calls(id),
    FOREIGN KEY(file_id) REFERENCES files(id)
);
"""


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection, reset: bool = True) -> None:
    if reset:
        for table in reversed((*TABLES, *MIDDLEWARE_TABLES)):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.executescript(SCHEMA)
    conn.commit()


def init_middleware_tables(conn: sqlite3.Connection, reset: bool = True) -> None:
    if reset:
        for table in reversed(MIDDLEWARE_TABLES):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.executescript(MIDDLEWARE_SCHEMA)
    conn.commit()


def insert_file(conn: sqlite3.Connection, path: Path, source: bytes) -> int:
    text = source.decode("utf-8", errors="ignore")
    cur = conn.execute(
        """
        INSERT INTO files(path, filename, ext, line_count, size_bytes)
        VALUES (?, ?, ?, ?, ?)
        """,
        (str(path), path.name, path.suffix.lower(), text.count("\n") + 1 if text else 0, len(source)),
    )
    return int(cur.lastrowid)


def insert_extracted(conn: sqlite3.Connection, file_id: int, extracted: dict[str, list[dict[str, Any]]]) -> None:
    for item in extracted["functions"]:
        conn.execute(
            """
            INSERT INTO functions(file_id, name, start_line, end_line, signature_text)
            VALUES (?, ?, ?, ?, ?)
            """,
            (file_id, item["name"], item["start_line"], item["end_line"], item["signature_text"]),
        )

    for item in extracted["calls"]:
        conn.execute(
            """
            INSERT INTO calls(file_id, caller_name, callee_name, line, call_text)
            VALUES (?, ?, ?, ?, ?)
            """,
            (file_id, item["caller_name"], item["callee_name"], item["line"], item["call_text"]),
        )

    for item in extracted["includes"]:
        conn.execute(
            """
            INSERT INTO includes(file_id, include_text, include_name, line)
            VALUES (?, ?, ?, ?)
            """,
            (file_id, item["include_text"], item["include_name"], item["line"]),
        )

    for item in extracted["structs"]:
        conn.execute(
            """
            INSERT INTO structs(file_id, name, kind, start_line, end_line)
            VALUES (?, ?, ?, ?, ?)
            """,
            (file_id, item["name"], item["kind"], item["start_line"], item["end_line"]),
        )

    for item in extracted["typedefs"]:
        conn.execute(
            """
            INSERT INTO typedefs(file_id, name, target_text, line)
            VALUES (?, ?, ?, ?)
            """,
            (file_id, item["name"], item["target_text"], item["line"]),
        )

    for item in extracted["macros"]:
        conn.execute(
            """
            INSERT INTO macros(file_id, name, macro_text, line)
            VALUES (?, ?, ?, ?)
            """,
            (file_id, item["name"], item["macro_text"], item["line"]),
        )


def export_excel(db_path: Path = DB_PATH, output_dir: Path = OUTPUT_DIR) -> None:
    import pandas as pd

    output_dir.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        init_middleware_tables(conn, reset=False)
        queries = {
            "functions.xlsx": """
                SELECT files.path, functions.*
                FROM functions JOIN files ON functions.file_id = files.id
                ORDER BY files.path, functions.start_line
            """,
            "calls.xlsx": """
                SELECT files.path, calls.*
                FROM calls JOIN files ON calls.file_id = files.id
                ORDER BY files.path, calls.line
            """,
            "includes.xlsx": """
                SELECT files.path, includes.*
                FROM includes JOIN files ON includes.file_id = files.id
                ORDER BY files.path, includes.line
            """,
            "macros.xlsx": """
                SELECT files.path, macros.*
                FROM macros JOIN files ON macros.file_id = files.id
                ORDER BY files.path, macros.line
            """,
            "types.xlsx": """
                SELECT source_table, path, id, file_id, name, kind,
                       start_line, end_line, target_text, line
                FROM (
                    SELECT 'structs' AS source_table, files.path, structs.id, structs.file_id,
                           structs.name, structs.kind, structs.start_line, structs.end_line,
                           NULL AS target_text, NULL AS line, structs.start_line AS sort_line
                    FROM structs JOIN files ON structs.file_id = files.id
                    UNION ALL
                    SELECT 'typedefs' AS source_table, files.path, typedefs.id, typedefs.file_id,
                           typedefs.name, 'typedef' AS kind, NULL AS start_line, NULL AS end_line,
                           typedefs.target_text, typedefs.line, typedefs.line AS sort_line
                    FROM typedefs JOIN files ON typedefs.file_id = files.id
                )
                ORDER BY path, sort_line
            """,
            "middleware_calls.xlsx": """
                SELECT files.path, middleware_calls.*
                FROM middleware_calls JOIN files ON middleware_calls.file_id = files.id
                ORDER BY files.path, middleware_calls.line
            """,
            "message_edges.xlsx": """
                SELECT files.path, message_edges.*
                FROM message_edges JOIN files ON message_edges.file_id = files.id
                ORDER BY files.path, message_edges.line
            """,
            "data_accesses.xlsx": """
                SELECT files.path, data_accesses.*
                FROM data_accesses JOIN files ON data_accesses.file_id = files.id
                ORDER BY files.path, data_accesses.line
            """,
        }

        for filename, query in queries.items():
            pd.read_sql_query(query, conn).to_excel(output_dir / filename, index=False)


def export_call_graph(db_path: Path = DB_PATH, output_dir: Path = OUTPUT_DIR) -> Path:
    import networkx as nx

    output_dir.mkdir(parents=True, exist_ok=True)
    graph = nx.DiGraph()

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT caller_name, callee_name, COUNT(*) AS weight
            FROM calls
            WHERE caller_name IS NOT NULL AND caller_name != ''
              AND callee_name IS NOT NULL AND callee_name != ''
            GROUP BY caller_name, callee_name
            """
        ).fetchall()

    for caller, callee, weight in rows:
        graph.add_edge(caller, callee, weight=weight)

    dot_path = output_dir / "call_graph.dot"
    nx.drawing.nx_pydot.write_dot(graph, dot_path)
    return dot_path

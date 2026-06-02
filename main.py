from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from tqdm import tqdm

from analyzer.db_writer import connect, export_call_graph, export_excel, init_db, insert_extracted, insert_file
from analyzer.extractor import extract_file
from analyzer.middleware import (
    analyze_middleware,
    event_route,
    generate_rules_template,
    inspect_calls,
    list_active_rules,
    suggest_rules,
)
from analyzer.parser_c import ast_to_text, parse_file
from analyzer.source_scanner import scan_sources
from config import BASE_DIR, COMMIT_INTERVAL, DB_PATH, EXCLUDE_DIRS, OUTPUT_DIR


def print_ast(src: str, exclude_dirs: list[str]) -> None:
    files = scan_sources(src, exclude_dirs=exclude_dirs)
    if not files:
        print(f"No C source files found: {src}")
        return

    for path in files:
        try:
            tree, source = parse_file(path)
            print(f"===== {path} =====")
            print(ast_to_text(tree.root_node, source))
        except Exception as exc:
            print(f"[ERROR] AST parse failed: {path}: {exc}")


def analyze(src: str, exclude_dirs: list[str]) -> None:
    files = scan_sources(src, exclude_dirs=exclude_dirs)
    print(f"Found {len(files)} source files.")

    with connect(DB_PATH) as conn:
        init_db(conn, reset=True)

        for index, path in enumerate(tqdm(files, desc="Analyzing C files"), start=1):
            try:
                tree, source = parse_file(path)
                file_id = insert_file(conn, path, source)
                extracted = extract_file(path, tree, source)
                insert_extracted(conn, file_id, extracted)
            except Exception as exc:
                print(f"[ERROR] Analysis skipped: {path}: {exc}")

            if index % COMMIT_INTERVAL == 0:
                conn.commit()

        conn.commit()

    print(f"Database written: {DB_PATH}")


def export() -> None:
    export_excel(DB_PATH)
    print("Excel files written to output/")


def graph() -> None:
    dot_path = export_call_graph(DB_PATH)
    print(f"Call graph written: {dot_path}")


def middleware() -> None:
    counts = analyze_middleware(DB_PATH)
    print(
        "Middleware analysis completed: "
        f"middleware_calls={counts['middleware_calls']}, "
        f"message_edges={counts['message_edges']}, "
        f"data_accesses={counts['data_accesses']}"
    )


def suggest_rule_candidates() -> None:
    counts = suggest_rules(DB_PATH)
    print(
        "Rule suggestion completed: "
        f"callee_summary={counts['callee_summary']}, "
        f"middleware_rule_candidates={counts['middleware_rule_candidates']}"
    )


def generate_rule_template() -> None:
    counts = generate_rules_template()
    total = sum(counts.values())
    detail = ", ".join(f"{key}={value}" for key, value in counts.items())
    print(f"Rule template generated: total={total}, {detail}")


def inspect_named_calls(name: str | None) -> None:
    if not name:
        raise SystemExit("--name is required for --mode inspect-calls")
    result = inspect_calls(name, DB_PATH)
    print(f"Inspect calls completed: count={result['count']}, output={result['output_path']}")


def show_event_route(event: str | None) -> None:
    if not event:
        raise SystemExit("--event is required for --mode event-route")
    result = event_route(event, DB_PATH)
    print(f"Event route completed: count={result['count']}, output={result['output_path']}")


def list_rules() -> None:
    result = list_active_rules()
    print(f"Active rules exported: count={result['count']}, output={result['output_path']}")


def is_under_workspace(path: Path) -> bool:
    workspace = BASE_DIR.resolve()
    try:
        path.resolve().relative_to(workspace)
        return True
    except ValueError:
        return False


def is_under_output(path: Path) -> bool:
    try:
        path.resolve().relative_to(OUTPUT_DIR.resolve())
        return True
    except ValueError:
        return False


def clean_project() -> None:
    cleanup_extensions = {".db", ".xlsx", ".dot", ".csv"}
    targets: list[Path] = []

    if OUTPUT_DIR.exists():
        for path in OUTPUT_DIR.rglob("*"):
            if path.is_file() and path.suffix.lower() in cleanup_extensions:
                targets.append(path)

    for path in BASE_DIR.rglob("__pycache__"):
        if path.is_dir():
            targets.append(path)

    targets = sorted(set(targets), key=lambda item: (item.is_dir(), str(item).lower()))

    if not targets:
        print("No cleanup targets found.")
        return

    print("Cleanup targets:")
    for target in targets:
        print(target)

    deleted = 0
    skipped = 0
    for target in targets:
        if not is_under_workspace(target):
            print(f"[SKIP] Outside workspace: {target}")
            skipped += 1
            continue
        if target.is_file() and not is_under_output(target):
            print(f"[SKIP] Output artifact outside output directory: {target}")
            skipped += 1
            continue

        try:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
            deleted += 1
        except Exception as exc:
            print(f"[ERROR] Could not delete {target}: {exc}")
            skipped += 1

    print(f"Cleanup completed: deleted={deleted}, skipped={skipped}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="tree-sitter based C analysis map generator")
    parser.add_argument(
        "--mode",
        choices=[
            "ast",
            "scan",
            "analyze",
            "suggest-rules",
            "generate-rules-template",
            "inspect-calls",
            "event-route",
            "list-rules",
            "clean-project",
            "middleware",
            "export",
            "graph",
        ],
        required=True,
    )
    parser.add_argument("--src", default="samples", help="Source file or directory for ast/scan/analyze modes")
    parser.add_argument("--name", help="Callee function name for inspect-calls mode")
    parser.add_argument("--event", help="Event ID for event-route mode")
    parser.add_argument(
        "--exclude",
        nargs="*",
        default=sorted(EXCLUDE_DIRS),
        help="Directory names to exclude while scanning",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()

    if args.mode == "ast":
        print_ast(args.src, args.exclude)
    elif args.mode == "scan":
        for path in scan_sources(Path(args.src), exclude_dirs=args.exclude):
            print(path)
    elif args.mode == "analyze":
        analyze(args.src, args.exclude)
    elif args.mode == "export":
        export()
    elif args.mode == "graph":
        graph()
    elif args.mode == "middleware":
        middleware()
    elif args.mode == "suggest-rules":
        suggest_rule_candidates()
    elif args.mode == "generate-rules-template":
        generate_rule_template()
    elif args.mode == "inspect-calls":
        inspect_named_calls(args.name)
    elif args.mode == "event-route":
        show_event_route(args.event)
    elif args.mode == "list-rules":
        list_rules()
    elif args.mode == "clean-project":
        clean_project()


if __name__ == "__main__":
    main()

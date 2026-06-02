from __future__ import annotations

import re
from pathlib import Path
from typing import Any


TARGET_NODE_TYPES = {
    "function_definition",
    "call_expression",
    "preproc_include",
    "preproc_def",
    "preproc_function_def",
    "struct_specifier",
    "union_specifier",
    "enum_specifier",
    "type_definition",
}


def node_text(node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="ignore")


def one_line_text(node, source: bytes) -> str:
    return " ".join(node_text(node, source).strip().split())


def point_row(point) -> int:
    if hasattr(point, "row"):
        return point.row
    return point[0]


def walk(node):
    yield node
    for child in node.children:
        yield from walk(child)


def find_first(node, types: set[str]):
    for child in walk(node):
        if child.type in types:
            return child
    return None


def declarator_name(node, source: bytes) -> str | None:
    if node is None:
        return None
    if node.type in {"identifier", "type_identifier", "field_identifier"}:
        return node_text(node, source).strip()

    named = node.child_by_field_name("declarator")
    if named is not None:
        found = declarator_name(named, source)
        if found:
            return found

    name_node = node.child_by_field_name("name")
    if name_node is not None:
        found = declarator_name(name_node, source)
        if found:
            return found

    for child in node.named_children:
        found = declarator_name(child, source)
        if found:
            return found
    return None


def function_name(node, source: bytes) -> str | None:
    return declarator_name(node.child_by_field_name("declarator"), source)


def function_signature(node, source: bytes) -> str:
    body = node.child_by_field_name("body")
    end_byte = body.start_byte if body is not None else node.end_byte
    return " ".join(source[node.start_byte : end_byte].decode("utf-8", errors="ignore").split())


def call_name(node, source: bytes) -> str | None:
    func = node.child_by_field_name("function")
    if func is None:
        return None
    if func.type in {"identifier", "field_identifier", "scoped_identifier"}:
        return node_text(func, source).strip()
    if func.type == "field_expression":
        field = func.child_by_field_name("field")
        if field is not None:
            return node_text(field, source).strip()
    if func.type in {"pointer_expression", "parenthesized_expression"}:
        return one_line_text(func, source)
    identifier = find_first(func, {"identifier", "field_identifier"})
    return node_text(identifier, source).strip() if identifier else one_line_text(func, source)


def include_name(text: str) -> str:
    match = re.search(r"#\s*include\s*[<\"]([^>\"]+)[>\"]", text)
    return match.group(1) if match else ""


def macro_name(text: str) -> str:
    match = re.search(r"#\s*define\s+([A-Za-z_]\w*)", text)
    return match.group(1) if match else ""


def type_name(node, source: bytes) -> str:
    name_node = node.child_by_field_name("name")
    if name_node is not None:
        return node_text(name_node, source).strip()
    found = find_first(node, {"type_identifier", "identifier"})
    return node_text(found, source).strip() if found else ""


def extract_file(path: Path, tree: Any, source: bytes) -> dict[str, list[dict[str, Any]]]:
    functions: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    includes: list[dict[str, Any]] = []
    structs: list[dict[str, Any]] = []
    typedefs: list[dict[str, Any]] = []
    macros: list[dict[str, Any]] = []

    function_ranges: list[tuple[int, int, str]] = []
    root = tree.root_node

    for node in walk(root):
        if node.type != "function_definition":
            continue
        name = function_name(node, source) or ""
        function_ranges.append((node.start_byte, node.end_byte, name))
        functions.append(
            {
                "name": name,
                "start_line": point_row(node.start_point) + 1,
                "end_line": point_row(node.end_point) + 1,
                "signature_text": function_signature(node, source),
            }
        )

    for node in walk(root):
        node_type = node.type

        if node_type == "call_expression":
            caller = ""
            for start, end, name in function_ranges:
                if start <= node.start_byte <= end:
                    caller = name
                    break
            calls.append(
                {
                    "caller_name": caller,
                    "callee_name": call_name(node, source) or "",
                    "line": point_row(node.start_point) + 1,
                    "call_text": one_line_text(node, source),
                }
            )
        elif node_type == "preproc_include":
            text = one_line_text(node, source)
            includes.append(
                {
                    "include_text": text,
                    "include_name": include_name(text),
                    "line": point_row(node.start_point) + 1,
                }
            )
        elif node_type in {"preproc_def", "preproc_function_def"}:
            text = one_line_text(node, source)
            macros.append(
                {
                    "name": macro_name(text),
                    "macro_text": text,
                    "line": point_row(node.start_point) + 1,
                }
            )
        elif node_type in {"struct_specifier", "union_specifier", "enum_specifier"}:
            structs.append(
                {
                    "name": type_name(node, source),
                    "kind": node_type.replace("_specifier", ""),
                    "start_line": point_row(node.start_point) + 1,
                    "end_line": point_row(node.end_point) + 1,
                }
            )
        elif node_type == "type_definition":
            typedefs.append(
                {
                    "name": declarator_name(node.child_by_field_name("declarator"), source)
                    or type_name(node, source),
                    "target_text": one_line_text(node, source),
                    "line": point_row(node.start_point) + 1,
                }
            )

    return {
        "functions": functions,
        "calls": calls,
        "includes": includes,
        "structs": structs,
        "typedefs": typedefs,
        "macros": macros,
    }

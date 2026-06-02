from __future__ import annotations

from pathlib import Path
from typing import Union

import tree_sitter_c
from tree_sitter import Language, Parser


PathLike = Union[str, Path]


def create_c_parser() -> Parser:
    parser = Parser()
    c_language = tree_sitter_c.language()
    try:
        language = Language(c_language)
    except TypeError:
        language = c_language

    if hasattr(parser, "set_language"):
        parser.set_language(language)
    else:
        parser.language = language

    return parser


def read_source_bytes(path: PathLike) -> bytes:
    return Path(path).read_bytes()


def decode_source(source: bytes) -> str:
    return source.decode("utf-8", errors="ignore")


def parse_bytes(source: bytes):
    return create_c_parser().parse(source)


def parse_file(path: PathLike):
    source = read_source_bytes(path)
    return parse_bytes(source), source


def point_to_tuple(point) -> tuple[int, int]:
    if hasattr(point, "row") and hasattr(point, "column"):
        return point.row, point.column
    return point[0], point[1]


def ast_to_text(node, source: bytes, indent: int = 0) -> str:
    start_row, start_col = point_to_tuple(node.start_point)
    end_row, end_col = point_to_tuple(node.end_point)
    field = ""
    node_text = ""

    if not node.children and node.end_byte >= node.start_byte:
        raw = source[node.start_byte : node.end_byte]
        text = raw.decode("utf-8", errors="ignore").strip()
        if text:
            node_text = f" text={text!r}"

    line = (
        f"{'  ' * indent}{node.type}"
        f" [{start_row + 1}:{start_col + 1}-{end_row + 1}:{end_col + 1}]"
        f"{field}{node_text}"
    )
    child_lines = [ast_to_text(child, source, indent + 1) for child in node.children]
    return "\n".join([line, *child_lines])

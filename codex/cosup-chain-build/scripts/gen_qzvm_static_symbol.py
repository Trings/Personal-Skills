#!/usr/bin/env python3
"""Generate qzvm_static_symbol.c from a generated qzvm_mask.c file."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


PKG_TABLE_SYMBOL = "com/cmcc/qzvm/impl/PackageMgr/pkgTable"
STATIC_NAMES_RE = re.compile(
    r"const\s+char\s*\*\s*static_field_names\s*\[\]\s*=\s*\{(?P<body>.*?)\};",
    re.DOTALL,
)
STATICINIT_RE = re.compile(
    r"ROM_ARRAY\s*\(\s*u8\s*,\s*staticinit\s*\)\s*=\s*\{(?P<body>.*?)\};",
    re.DOTALL,
)
STRING_RE = re.compile(r'"([^"]+)"')
STATICINIT_COMMENT_RE = re.compile(
    r"/\*\s*(?P<symbol>.*?)\s+@\s*(?P<addr>0x[0-9a-fA-F]+)\s*:\s*0x[0-9a-fA-F]+\s*\*/"
)
SYMS_TABLE_RE = re.compile(
    r"(?P<decl>^[ \t]*const\s+u8\s+syms_static_table\s*\[\]\s*=\s*\{)"
    r"(?P<body>.*?)"
    r"(?P<end>^[ \t]*\};)",
    re.DOTALL | re.MULTILINE,
)
INT_LITERAL_RE = re.compile(r"\b0x[0-9a-fA-F]+\b|\b\d+\b")
APPEND_ONLY_PHRASE = "append-only for COS upgrade compatibility"
APPEND_ONLY_COMMENT = f"/* Generated using {APPEND_ONLY_PHRASE}. */"
APPEND_ONLY_COMMENT_LINE = (
    " * 1. GENERATED BASIS: Symbol order is append-only for COS upgrade compatibility.         *"
)
MAINTENANCE_RULE_RE = re.compile(
    r"^[ \t]*\*\s*1\.\s*(?:MANUAL MAINTENANCE|MAINTAIN ORDER|GENERATED BASIS):.*$",
    re.MULTILINE,
)


HEADER = """#include "common/base.h"

/* Generated using append-only for COS upgrade compatibility. */

// TODO: com/cmcc/qzvm/impl/PackageMgr/pkgTable
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate COS upgrade static symbol table from qzvm_mask.c. "
            "When --base-symbols is provided, symbols from the current mask "
            "that are not in the base list are appended in mask order."
        )
    )
    parser.add_argument("mask", type=Path, help="generated qzvm_mask.c")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("COS/components/qzvm/core/arch/qzvm/qzvm_static_symbol.c"),
        help="output qzvm_static_symbol.c path",
    )
    parser.add_argument(
        "--base-symbols",
        type=Path,
        help="existing ordered symbol list used as the append-only prefix",
    )
    parser.add_argument(
        "--symbols-out",
        type=Path,
        help="write the final ordered symbol list to this file",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[PKG_TABLE_SYMBOL],
        help="symbol to exclude; can be passed more than once",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="fail if any final symbol cannot be found in qzvm_mask.c staticinit",
    )
    return parser.parse_args()


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text()


def write_text_if_changed(path: Path, text: str) -> None:
    if path.exists() and read_text(path) == text:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def ordered_unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def parse_symbol_list(path: Path) -> list[str]:
    symbols: list[str] = []
    for raw_line in read_text(path).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if " " in line:
            index, symbol = line.split(maxsplit=1)
            if index.isdigit():
                line = symbol.strip()
        symbols.append(line)
    return ordered_unique(symbols)


def parse_static_field_names(mask_text: str) -> list[str]:
    match = STATIC_NAMES_RE.search(mask_text)
    if not match:
        raise ValueError("cannot find static_field_names[] in qzvm_mask.c")
    return STRING_RE.findall(match.group("body"))


def count_initializer_bytes(prefix: str) -> int:
    return len(INT_LITERAL_RE.findall(prefix))


def parse_staticinit_entries(mask_text: str) -> dict[str, tuple[int, int]]:
    match = STATICINIT_RE.search(mask_text)
    if not match:
        raise ValueError("cannot find ROM_ARRAY(u8, staticinit) in qzvm_mask.c")

    entries: dict[str, tuple[int, int]] = {}
    body = match.group("body")
    last_end = 0
    for comment in STATICINIT_COMMENT_RE.finditer(body):
        prefix = body[last_end : comment.start()]
        size = count_initializer_bytes(prefix)
        symbol = comment.group("symbol").strip()
        addr = int(comment.group("addr"), 16)
        entries[symbol] = (addr, size)
        last_end = comment.end()
    return entries


def append_symbol_sort_key(symbol: str) -> str:
    """Match the historical manual append order used by COSUP-MASKC notebooks."""
    return (
        symbol.replace("/static_bdata", "/static_0_bdata")
        .replace("/static_idata", "/static_1_idata")
        .replace("/static_sdata", "/static_2_sdata")
    )


def make_final_symbols(mask_symbols: list[str], base_symbols: list[str], excludes: set[str]) -> list[str]:
    mask_symbols = [symbol for symbol in ordered_unique(mask_symbols) if symbol not in excludes]
    if not base_symbols:
        return mask_symbols

    final_symbols = [symbol for symbol in ordered_unique(base_symbols) if symbol not in excludes]
    known = set(final_symbols)
    additions = sorted(
        (symbol for symbol in mask_symbols if symbol not in known),
        key=append_symbol_sort_key,
    )
    final_symbols.extend(additions)
    return final_symbols


def make_table_bytes(symbols: list[str], entries: dict[str, tuple[int, int]], strict: bool) -> tuple[list[int], list[str]]:
    missing: list[str] = []
    table: list[int] = list(len(symbols).to_bytes(4, byteorder="big"))

    for symbol in symbols:
        entry = entries.get(symbol)
        if entry is None:
            if strict:
                missing.append(symbol)
            table.extend([0, 0, 0, 0])
            continue

        addr, size = entry
        if addr > 0xFFFFFF:
            raise ValueError(f"{symbol} address 0x{addr:x} does not fit in 3 bytes")
        if size > 0xFF:
            raise ValueError(f"{symbol} size 0x{size:x} does not fit in 1 byte")
        table.extend([(addr >> 16) & 0xFF, (addr >> 8) & 0xFF, addr & 0xFF, size])

    return table, missing


def parse_int_literals(text: str) -> list[int]:
    return [int(token, 0) for token in INT_LITERAL_RE.findall(text)]


def find_syms_table(text: str) -> re.Match[str] | None:
    return SYMS_TABLE_RE.search(text)


def most_common_count(counts: list[int], default: int = 20) -> int:
    if not counts:
        return default
    return max(sorted(set(counts)), key=lambda count: (counts.count(count), count))


def array_style(existing_body: str | None) -> tuple[str, int, int, list[tuple[list[int], str]]]:
    indent = "    "
    per_line = 20
    literal_width = 4
    existing_lines: list[tuple[list[int], str]] = []
    counts: list[int] = []

    if existing_body is None:
        return indent, per_line, literal_width, existing_lines

    for line in existing_body.splitlines():
        tokens = INT_LITERAL_RE.findall(line)
        if not tokens:
            continue
        values = [int(token, 0) for token in tokens]
        existing_lines.append((values, line))
        counts.append(len(values))
        indent = re.match(r"\s*", line).group(0)
        literal_width = max(literal_width, *(len(token) for token in tokens))

    per_line = most_common_count(counts, default=per_line)
    return indent, per_line, literal_width, existing_lines


def format_array_line(chunk: list[int], indent: str, literal_width: int) -> str:
    items = [f"0x{byte:x}".rjust(literal_width) for byte in chunk]
    return indent + ", ".join(items).lstrip() + ","


def update_existing_array_line(line: str, values: list[int]) -> str:
    tokens = INT_LITERAL_RE.findall(line)
    if len(tokens) != len(values):
        return line

    formatted = iter(
        f"0x{value:x}".rjust(len(token)) if len(f"0x{value:x}") <= len(token) else f"0x{value:x}"
        for token, value in zip(tokens, values)
    )
    return INT_LITERAL_RE.sub(lambda _: next(formatted), line)


def format_c_array_body(data: list[int], existing_body: str | None = None) -> str:
    indent, per_line, literal_width, existing_lines = array_style(existing_body)
    lines: list[str] = []

    for line_index, offset in enumerate(range(0, len(data), per_line)):
        chunk = data[offset : offset + per_line]
        if line_index < len(existing_lines):
            existing_values, existing_line = existing_lines[line_index]
            if len(existing_values) == len(chunk):
                lines.append(update_existing_array_line(existing_line, chunk))
                continue
        lines.append(format_array_line(chunk, indent, literal_width))

    return "\n".join(lines)


def format_c_array(data: list[int], existing_body: str | None = None) -> str:
    return "const u8 syms_static_table[] = {\n" + format_c_array_body(data, existing_body) + "\n};\n"


def ensure_append_only_comment(text: str) -> str:
    if APPEND_ONLY_PHRASE.lower() in text.lower():
        return text

    match = find_syms_table(text)
    if not match:
        return text

    prefix = text[: match.start()]
    updated_prefix, count = MAINTENANCE_RULE_RE.subn(APPEND_ONLY_COMMENT_LINE, prefix, count=1)
    if count:
        return updated_prefix + text[match.start() :]

    insert = APPEND_ONLY_COMMENT + "\n"
    if match.start() > 0 and not text[: match.start()].endswith("\n"):
        insert = "\n" + insert
    return text[: match.start()] + insert + text[match.start() :]


def render_output(output_path: Path, table: list[int]) -> str:
    if not output_path.exists():
        return HEADER + format_c_array(table)

    current = read_text(output_path)
    match = find_syms_table(current)
    if not match:
        return HEADER + format_c_array(table)

    updated = current
    existing_table = parse_int_literals(match.group("body"))
    if existing_table != table:
        replacement = (
            match.group("decl")
            + "\n"
            + format_c_array_body(table, existing_body=match.group("body"))
            + "\n"
            + match.group("end")
        )
        updated = current[: match.start()] + replacement + current[match.end() :]

    return ensure_append_only_comment(updated)


def main() -> int:
    args = parse_args()
    excludes = set(args.exclude or [])

    mask_text = read_text(args.mask)
    mask_symbols = parse_static_field_names(mask_text)
    entries = parse_staticinit_entries(mask_text)
    base_symbols = parse_symbol_list(args.base_symbols) if args.base_symbols else []
    final_symbols = make_final_symbols(mask_symbols, base_symbols, excludes)
    table, missing = make_table_bytes(final_symbols, entries, args.strict)

    if missing:
        print("missing staticinit symbols:", file=sys.stderr)
        for symbol in missing:
            print(f"  {symbol}", file=sys.stderr)
        return 1

    output = render_output(args.output, table)
    write_text_if_changed(args.output, output)

    if args.symbols_out:
        symbols_text = "\n".join(final_symbols) + "\n"
        write_text_if_changed(args.symbols_out, symbols_text)

    found = sum(1 for symbol in final_symbols if symbol in entries)
    print(f"generated {args.output}: {len(final_symbols)} symbols, {found} found in staticinit")
    if args.symbols_out:
        print(f"wrote {args.symbols_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

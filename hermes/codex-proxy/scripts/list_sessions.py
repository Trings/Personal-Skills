#!/usr/bin/env python3
"""
List recent Codex CLI sessions.

Auto-detects the Codex SQLite database, supports configurable count
and JSON output, and handles schema differences across Codex versions.

Usage:
    python3 list_sessions.py              # default: last 15
    python3 list_sessions.py -n 3         # last 3
    python3 list_sessions.py -n 5 --json  # JSON output
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

CODEX_DIR = Path.home() / ".codex"
SOURCE_ICONS = {
    "vscode": "📝",
    "cli": "💻",
    "codex_cli": "💻",
}


def _find_db():
    """Find the Codex state database (handles state_5, state_6, etc.)."""
    if not CODEX_DIR.is_dir():
        return None
    candidates = sorted(
        CODEX_DIR.glob("state_*.sqlite"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _get_columns(db):
    """Return set of column names in the threads table."""
    try:
        cols = db.execute("PRAGMA table_info(threads)").fetchall()
        return {c[1] for c in cols}
    except sqlite3.OperationalError:
        return set()


def list_sessions(limit=15):
    """Query sessions and return list of dicts."""
    db_path = _find_db()
    if db_path is None:
        raise FileNotFoundError(f"No state_*.sqlite found in {CODEX_DIR}")

    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    cols = _get_columns(db)

    # Build query based on available columns (cross-version safety)
    select_parts = ["id", "title", "source", "cwd"]
    # thread_source added in later Codex versions
    if "thread_source" in cols:
        select_parts.append("thread_source")
    if "model" in cols:
        select_parts.append("model")
    select_parts.extend([
        "datetime(created_at, 'unixepoch', 'localtime') as created",
        "datetime(updated_at, 'unixepoch', 'localtime') as updated",
    ])

    sql = f"""
        SELECT {', '.join(select_parts)}
        FROM threads
        ORDER BY updated_at DESC
        LIMIT ?
    """
    rows = db.execute(sql, (limit,)).fetchall()

    results = []
    for r in rows:
        d = dict(r)
        # Normalize source: prefer thread_source if available
        raw_source = d.get("thread_source") or d.get("source") or ""
        source = raw_source.lower()
        d["source_icon"] = SOURCE_ICONS.get(source, "💻" if source else "❓")
        d["title"] = (d.get("title") or "(无标题)")[:80]
        d["cwd"] = d.get("cwd") or ""
        results.append(d)

    db.close()
    return results


def format_text(sessions):
    """Human-readable output."""
    for i, s in enumerate(sessions, 1):
        cwd = s["cwd"]
        if len(cwd) > 45:
            cwd = "..." + cwd[-42:]
        print(f"{i:2d}. {s['source_icon']} {s['title']}")
        print(f"    UUID: {s['id']}")
        print(f"    更新: {s['updated']} | 目录: {cwd or '?'}")
        print()


def format_json(sessions):
    """Machine-readable JSON output."""
    out = []
    for s in sessions:
        out.append({
            "index": s.get("index"),
            "uuid": s["id"],
            "title": s["title"],
            "source": s.get("thread_source") or s.get("source"),
            "cwd": s["cwd"],
            "model": s.get("model", ""),
            "updated": s["updated"],
            "created": s["created"],
        })
    # Re-index after building
    for i, item in enumerate(out, 1):
        item["index"] = i
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
    print()


def main():
    parser = argparse.ArgumentParser(
        description="List recent Codex CLI sessions"
    )
    parser.add_argument(
        "-n", "--count",
        type=int, default=15,
        help="Number of sessions to list (default: 15)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON",
    )
    args = parser.parse_args()

    try:
        sessions = list_sessions(args.count)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except sqlite3.OperationalError as e:
        print(f"DB error: {e}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        format_json(sessions)
    else:
        format_text(sessions)


if __name__ == "__main__":
    main()


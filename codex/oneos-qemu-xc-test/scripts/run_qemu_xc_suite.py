#!/usr/bin/env python3
"""Portable runner for oneos-sim QEMU XC testcase suites."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence
from uuid import uuid4


CONSISTENCY = "一致性"
DEFAULT_MAX_JOBS = 4


@dataclass(frozen=True)
class Shard:
    name: str
    inputs: tuple[Path, ...]
    persist_path: Path
    output_log: Path
    shared_log: Path


@dataclass(frozen=True)
class ParallelLayout:
    run_id: str
    run_dir: Path
    baseline_path: Path
    init_output_log: Path
    init_shared_log: Path
    summary_text: Path
    summary_json: Path


@dataclass(frozen=True)
class ShardResult:
    shard: Shard
    returncode: int


@dataclass(frozen=True)
class ShardSummary:
    shard: Shard
    returncode: int
    total: int | None
    passed: int | None
    failed: int | None
    first_issue: str


SUMMARY_RE = re.compile(r"^\[xc\] total=(\d+) passed=(\d+) failed=(\d+)\s*$")
ISSUE_PATTERNS = (
    "[xc] FAIL",
    "Traceback",
    "Exception",
    "Assert failed",
    "Load access fault",
    "Store access fault",
    "SW mismatch",
    "ERROR",
    "error",
    "fault",
)


def _quote_command(args: Sequence[object]) -> str:
    return " ".join(subprocess.list2cmdline([str(arg)]) for arg in args)


def _safe_name(value: str) -> str:
    sanitized = re.sub(r"[^0-9A-Za-z._-]+", "_", value.strip())
    return sanitized.strip("._-") or "root"


def _make_run_id() -> str:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return f"{timestamp}_pid{os.getpid()}_{uuid4().hex[:8]}"


def _split_csv(values: Sequence[str]) -> list[str]:
    items: list[str] = []
    for value in values:
        for item in value.split(","):
            stripped = item.strip()
            if stripped:
                items.append(stripped)
    return items


def _contains_txt(path: Path) -> bool:
    if path.is_file():
        return path.suffix.lower() == ".txt"
    if not path.is_dir():
        return False
    return any(item.is_file() and item.suffix.lower() == ".txt" for item in path.rglob("*.txt"))


def _script_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path] if path.suffix.lower() == ".txt" else []
    if not path.is_dir():
        return []
    return sorted(item.resolve() for item in path.rglob("*.txt") if item.is_file())


def _run_capture(cmd: Sequence[str], cwd: Path) -> str:
    completed = subprocess.run(
        list(cmd),
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"{_quote_command(cmd)} failed with {completed.returncode}: {detail}")
    return completed.stdout.strip()


def _find_repo_root(start: Path) -> Path:
    current = start.resolve()
    if current.is_file():
        current = current.parent

    for candidate in (current, *current.parents):
        if (candidate / "Script" / "qemu" / "run_xc_script.py").is_file():
            return candidate

    try:
        git_root = Path(_run_capture(["git", "rev-parse", "--show-toplevel"], current)).resolve()
    except Exception as exc:
        raise FileNotFoundError(
            "Cannot find oneos-sim repo root. Run from inside oneos-sim or pass --repo-root."
        ) from exc

    if not (git_root / "Script" / "qemu" / "run_xc_script.py").is_file():
        raise FileNotFoundError(f"Not a oneos-sim repo root: {git_root}")
    return git_root


def _resolve_testcase_root(repo_root: Path, override: str | None) -> Path:
    raw = override or os.environ.get("ONEOS_SIMMASTER_TESTCASE_ROOT")
    root = Path(raw).expanduser().resolve() if raw else (repo_root.parent / "simmaster_auto_lite" / "testcase").resolve()
    if not root.is_dir():
        raise FileNotFoundError(
            f"Testcase root not found: {root}\n"
            "Expected sibling layout ../simmaster_auto_lite/testcase, or pass --testcase-root."
        )
    return root


def _dedupe_paths(paths: Iterable[Path]) -> list[Path]:
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(resolved)
    return unique


def _candidate_paths(target: str, testcase_root: Path) -> tuple[list[Path], list[Path]]:
    raw = target.strip().strip("/")
    normalized = raw.replace("\\", "/")
    lower = normalized.lower()
    preferred: list[Path] = []
    fallback: list[Path] = []

    target_path = Path(raw).expanduser()
    if target_path.is_absolute():
        preferred.append(target_path)
    else:
        preferred.append(testcase_root / normalized)

    aliases: dict[str, list[str]] = {
        "consistency": [CONSISTENCY],
        "consistent": [CONSISTENCY],
        "yizhixing": [CONSISTENCY],
        "一致性测试": [CONSISTENCY],
        "一致性": [CONSISTENCY],
        "api": [f"{CONSISTENCY}/api"],
        "core": [f"{CONSISTENCY}/api/core"],
        "api/core": [f"{CONSISTENCY}/api/core"],
        "base": [f"{CONSISTENCY}/api/base"],
        "api/base": [f"{CONSISTENCY}/api/base"],
        "cryptography": [f"{CONSISTENCY}/api/cryptography"],
        "api/cryptography": [f"{CONSISTENCY}/api/cryptography"],
        "ema": [f"{CONSISTENCY}/api/ema"],
        "api/ema": [f"{CONSISTENCY}/api/ema"],
        "re": [f"{CONSISTENCY}/re"],
        "vm": [f"{CONSISTENCY}/vm"],
        "ram": ["RAM/RAM", "RAM"],
        "ram/ram": ["RAM/RAM"],
        "rfm": ["RFM/RFM", "RFM"],
        "rfm/rfm": ["RFM/RFM"],
        "rfm/sim": ["RFM/RFM/SIM", "RFM/SIM"],
        "rfm/usim": ["RFM/RFM/USIM", "RFM/USIM"],
        "sim": ["RFM/RFM/SIM"],
        "usim": ["RFM/RFM/USIM"],
    }

    if lower in aliases:
        preferred = [testcase_root / item for item in aliases[lower]]
        if target_path.is_absolute():
            preferred.append(target_path)

    if not normalized.startswith(f"{CONSISTENCY}/"):
        fallback.append(testcase_root / CONSISTENCY / normalized)
    if normalized.startswith("api/"):
        fallback.append(testcase_root / CONSISTENCY / normalized)
    if lower.startswith("rfm/") and not lower.startswith("rfm/rfm/"):
        fallback.append(testcase_root / "RFM" / "RFM" / normalized.split("/", 1)[1])

    return (_dedupe_paths(preferred), _dedupe_paths(fallback))


def _resolve_target(target: str, testcase_root: Path) -> Path:
    preferred, fallback = _candidate_paths(target, testcase_root)
    matches = [path for path in preferred if _contains_txt(path)]
    if not matches:
        matches = [path for path in fallback if _contains_txt(path)]
    if not matches:
        checked = "\n  ".join(str(path) for path in [*preferred, *fallback])
        raise FileNotFoundError(f"No XC *.txt scripts found for target {target!r}. Checked:\n  {checked}")

    existing = []
    seen = set()
    for path in matches:
        real = path.resolve()
        if real not in seen:
            seen.add(real)
            existing.append(real)

    return existing[0]


def _build_child_command(args: argparse.Namespace, child_inputs: Sequence[Path], *, input_option: bool = False) -> list[str]:
    cmd = [
        args.python,
        "Script/qemu/run_xc_script.py",
    ]
    if args.fresh_state:
        cmd.append("--fresh-state")
    if args.keep_going:
        cmd.append("--keep-going")
    if args.quiet:
        cmd.append("--quiet")
    for path in child_inputs:
        if input_option:
            cmd.extend(["-i", str(path)])
        else:
            cmd.append(str(path))
    return cmd


def _make_parallel_layout(repo_root: Path, run_id: str) -> ParallelLayout:
    run_dir = repo_root / "build" / "cc2560a_qemu" / "skill_parallel_runs" / _safe_name(run_id)
    return ParallelLayout(
        run_id=run_id,
        run_dir=run_dir,
        baseline_path=run_dir / "baseline" / "customerlib_ram.bin",
        init_output_log=run_dir / "baseline" / "qemu_init.output.log",
        init_shared_log=run_dir / "baseline" / "qemu_init.serial.txt",
        summary_text=run_dir / "summary.txt",
        summary_json=run_dir / "summary.json",
    )


def _make_shards(target: Path, layout: ParallelLayout) -> list[Shard]:
    state_dir = layout.run_dir / "state"
    output_dir = layout.run_dir / "output"
    shared_dir = layout.run_dir / "shared_serial"

    shard_inputs: list[tuple[str, tuple[Path, ...]]] = []
    direct_files = sorted(item for item in target.iterdir() if item.is_file() and item.suffix.lower() == ".txt") if target.is_dir() else _script_files(target)
    if direct_files:
        shard_inputs.append(("root_files", tuple(path.resolve() for path in direct_files)))

    if target.is_dir():
        for child in sorted(target.iterdir()):
            if child.is_dir() and _contains_txt(child):
                shard_inputs.append((child.name, tuple(_script_files(child))))

    if not shard_inputs:
        shard_inputs.append((target.stem if target.is_file() else target.name, tuple(_script_files(target))))

    shards: list[Shard] = []
    for index, (name, inputs) in enumerate(shard_inputs, start=1):
        safe = f"{index:03d}_{_safe_name(name)}"
        shards.append(
            Shard(
                name=name,
                inputs=inputs,
                persist_path=state_dir / f"{safe}.bin",
                output_log=output_dir / f"{safe}.log",
                shared_log=shared_dir / f"{safe}.txt",
            )
        )
    return shards


def _filter_shards(shards: Sequence[Shard], selected_names: Sequence[str]) -> list[Shard]:
    selected = _split_csv(selected_names)
    if not selected:
        return list(shards)

    wanted = set(selected)
    filtered = [shard for shard in shards if shard.name in wanted]
    missing = sorted(wanted - {shard.name for shard in filtered})
    if missing:
        available = ", ".join(shard.name for shard in shards)
        raise ValueError(f"--only shard(s) not found: {', '.join(missing)}. Available: {available}")
    return filtered


def _is_ram_rfm_target(target: Path, testcase_root: Path) -> bool:
    try:
        rel = target.resolve().relative_to(testcase_root.resolve())
    except ValueError:
        return False
    return rel.parts[:1] in {("RAM",), ("RFM",)}


def _init_script_for_target(target: Path, testcase_root: Path) -> str:
    return "Script/qemu/qemu_ram_rfm_init.py" if _is_ram_rfm_target(target, testcase_root) else "Script/qemu/qemu_init.py"


def _prepare_parallel_baseline(args: argparse.Namespace, repo_root: Path, testcase_root: Path, target: Path, layout: ParallelLayout) -> Path | None:
    baseline = repo_root / "build" / "cc2560a_qemu" / "customerlib_ram.bin"
    if baseline.exists() and not args.reinit_baseline:
        return baseline

    if args.no_init_baseline or args.fresh_state:
        return None

    layout.baseline_path.parent.mkdir(parents=True, exist_ok=True)
    layout.init_output_log.parent.mkdir(parents=True, exist_ok=True)
    if layout.baseline_path.exists():
        layout.baseline_path.unlink()
    if layout.init_shared_log.exists():
        layout.init_shared_log.unlink()

    env = os.environ.copy()
    env["ONEOS_QEMU_PERSIST_PATH"] = str(layout.baseline_path)
    env["ONEOS_QEMU_SHARED_LOG_PATH"] = str(layout.init_shared_log)
    env.pop("ONEOS_QEMU_KEEP_STATE", None)

    cmd = [args.python, _init_script_for_target(target, testcase_root)]
    print(f"[oneos-qemu-xc] preparing baseline: {_quote_command(cmd)}", flush=True)
    print(f"[oneos-qemu-xc] baseline_bin={layout.baseline_path}", flush=True)
    with layout.init_output_log.open("w", encoding="utf-8") as log:
        log.write(f"$ {_quote_command(cmd)}\n")
        log.write(f"# ONEOS_QEMU_PERSIST_PATH={layout.baseline_path}\n")
        log.write(f"# ONEOS_QEMU_SHARED_LOG_PATH={layout.init_shared_log}\n\n")
        log.flush()
        completed = subprocess.run(
            cmd,
            cwd=repo_root,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
        )
    if completed.returncode != 0:
        raise RuntimeError(
            f"baseline initialization failed with exit={completed.returncode}; see {layout.init_output_log}"
        )
    if not layout.baseline_path.exists():
        raise RuntimeError(f"baseline initialization did not create {layout.baseline_path}")
    return layout.baseline_path


def _prepare_parallel_state(shards: Iterable[Shard], baseline: Path | None, fresh_state: bool) -> None:
    for shard in shards:
        shard.persist_path.parent.mkdir(parents=True, exist_ok=True)
        shard.output_log.parent.mkdir(parents=True, exist_ok=True)
        shard.shared_log.parent.mkdir(parents=True, exist_ok=True)
        if shard.shared_log.exists():
            shard.shared_log.unlink()
        if fresh_state:
            if shard.persist_path.exists():
                shard.persist_path.unlink()
        elif baseline is not None:
            shutil.copy2(baseline, shard.persist_path)


def _run_shard(args: argparse.Namespace, repo_root: Path, shard: Shard) -> ShardResult:
    env = os.environ.copy()
    env["ONEOS_QEMU_PERSIST_PATH"] = str(shard.persist_path)
    env["ONEOS_QEMU_SHARED_LOG_PATH"] = str(shard.shared_log)
    if shard.persist_path.exists() and not args.fresh_state:
        env.setdefault("ONEOS_QEMU_KEEP_STATE", "1")

    cmd = _build_child_command(args, shard.inputs, input_option=True)
    with shard.output_log.open("w", encoding="utf-8") as log:
        log.write(f"$ {_quote_command(cmd)}\n")
        log.write(f"# ONEOS_QEMU_PERSIST_PATH={shard.persist_path}\n")
        log.write(f"# ONEOS_QEMU_SHARED_LOG_PATH={shard.shared_log}\n\n")
        log.flush()
        completed = subprocess.run(
            cmd,
            cwd=repo_root,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
        )
    return ShardResult(shard=shard, returncode=completed.returncode)


def _parse_shard_summary(result: ShardResult) -> ShardSummary:
    total: int | None = None
    passed: int | None = None
    failed: int | None = None
    first_issue = ""

    try:
        lines = result.shard.output_log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return ShardSummary(
            shard=result.shard,
            returncode=result.returncode,
            total=None,
            passed=None,
            failed=None,
            first_issue=f"unable to read output log: {exc}",
        )

    for line in lines:
        match = SUMMARY_RE.match(line.strip())
        if match:
            total, passed, failed = (int(match.group(index)) for index in range(1, 4))
        if not first_issue and any(pattern in line for pattern in ISSUE_PATTERNS):
            first_issue = line.strip()

    if not first_issue and result.returncode != 0:
        first_issue = "nonzero exit without matched failure line"

    return ShardSummary(
        shard=result.shard,
        returncode=result.returncode,
        total=total,
        passed=passed,
        failed=failed,
        first_issue=first_issue,
    )


def _format_count(value: int | None) -> str:
    return "?" if value is None else str(value)


def _write_parallel_summary(layout: ParallelLayout, results: Sequence[ShardResult]) -> int:
    summaries = [_parse_shard_summary(result) for result in sorted(results, key=lambda item: item.shard.name)]
    shard_total = len(summaries)
    shard_passed = sum(1 for item in summaries if item.returncode == 0)
    shard_failed = shard_total - shard_passed
    script_total = sum(item.total or 0 for item in summaries)
    script_passed = sum(item.passed or 0 for item in summaries)
    script_failed = sum(item.failed or 0 for item in summaries)
    unknown_script_counts = any(item.total is None for item in summaries)

    lines = [
        "[oneos-qemu-xc] Parallel Summary",
        f"[oneos-qemu-xc] run_id={layout.run_id}",
        f"[oneos-qemu-xc] run_dir={layout.run_dir}",
        f"[oneos-qemu-xc] shards total={shard_total} passed={shard_passed} failed={shard_failed}",
        (
            f"[oneos-qemu-xc] scripts total={script_total} passed={script_passed} failed={script_failed}"
            + (" unknown_counts=yes" if unknown_script_counts else "")
        ),
        "[oneos-qemu-xc] shard details:",
    ]

    for item in summaries:
        status = "PASS" if item.returncode == 0 else "FAIL"
        lines.append(
            "[oneos-qemu-xc] "
            f"{status} shard={item.shard.name} exit={item.returncode} "
            f"total={_format_count(item.total)} passed={_format_count(item.passed)} failed={_format_count(item.failed)}"
        )
        lines.append(f"  output={item.shard.output_log}")
        lines.append(f"  shared_serial={item.shard.shared_log}")
        if item.first_issue:
            lines.append(f"  first_issue={item.first_issue}")

    text = "\n".join(lines) + "\n"
    layout.summary_text.parent.mkdir(parents=True, exist_ok=True)
    layout.summary_text.write_text(text, encoding="utf-8")

    payload = {
        "run_id": layout.run_id,
        "run_dir": str(layout.run_dir),
        "shards": {
            "total": shard_total,
            "passed": shard_passed,
            "failed": shard_failed,
        },
        "scripts": {
            "total": script_total,
            "passed": script_passed,
            "failed": script_failed,
            "unknown_counts": unknown_script_counts,
        },
        "items": [
            {
                "shard": item.shard.name,
                "returncode": item.returncode,
                "total": item.total,
                "passed": item.passed,
                "failed": item.failed,
                "first_issue": item.first_issue,
                "output_log": str(item.shard.output_log),
                "shared_serial": str(item.shard.shared_log),
                "persist_path": str(item.shard.persist_path),
            }
            for item in summaries
        ],
    }
    layout.summary_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(text, end="", flush=True)
    print(f"[oneos-qemu-xc] summary_text={layout.summary_text}", flush=True)
    print(f"[oneos-qemu-xc] summary_json={layout.summary_json}", flush=True)

    return 1 if shard_failed or script_failed else 0


def _print_resolution(target: Path, testcase_root: Path, repo_root: Path) -> None:
    print(f"[oneos-qemu-xc] repo_root={repo_root}")
    print(f"[oneos-qemu-xc] testcase_root={testcase_root}")
    print(f"[oneos-qemu-xc] target={target}")


def _run_sequential(args: argparse.Namespace, repo_root: Path, target: Path) -> int:
    cmd = _build_child_command(args, [target], input_option=True)
    print(f"[oneos-qemu-xc] running: {_quote_command(cmd)}", flush=True)
    if args.dry_run:
        return 0
    return subprocess.run(cmd, cwd=repo_root, check=False).returncode


def _run_parallel(args: argparse.Namespace, repo_root: Path, testcase_root: Path, target: Path) -> int:
    run_id = args.run_id or _make_run_id()
    layout = _make_parallel_layout(repo_root, run_id)
    try:
        shards = _filter_shards(_make_shards(target, layout), args.only)
    except ValueError as exc:
        print(f"[oneos-qemu-xc] error: {exc}", file=sys.stderr)
        return 2
    jobs = args.jobs or min(DEFAULT_MAX_JOBS, os.cpu_count() or 1, len(shards))
    jobs = max(1, min(jobs, len(shards)))

    print(f"[oneos-qemu-xc] run_id={run_id}", flush=True)
    print(f"[oneos-qemu-xc] run_dir={layout.run_dir}", flush=True)
    print(f"[oneos-qemu-xc] parallel shards={len(shards)} jobs={jobs}", flush=True)
    for shard in shards:
        cmd = _build_child_command(args, shard.inputs, input_option=True)
        print(
            f"[oneos-qemu-xc] shard {shard.name}: {_quote_command(cmd)}\n"
            f"  persist={shard.persist_path}\n"
            f"  output={shard.output_log}\n"
            f"  shared_log={shard.shared_log}",
            flush=True,
        )

    if args.dry_run:
        return 0

    try:
        baseline = _prepare_parallel_baseline(args, repo_root, testcase_root, target, layout)
        _prepare_parallel_state(shards, baseline, args.fresh_state)
    except RuntimeError as exc:
        print(f"[oneos-qemu-xc] error: {exc}", file=sys.stderr)
        return 2

    results: list[ShardResult] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as executor:
        future_map = {executor.submit(_run_shard, args, repo_root, shard): shard for shard in shards}
        for future in concurrent.futures.as_completed(future_map):
            result = future.result()
            results.append(result)
            status = "PASS" if result.returncode == 0 else "FAIL"
            print(
                f"[oneos-qemu-xc] {status} shard={result.shard.name} "
                f"exit={result.returncode} output={result.shard.output_log}",
                flush=True,
            )

    return _write_parallel_summary(layout, results)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run oneos-sim QEMU XC suites using portable simmaster_auto_lite paths.",
    )
    parser.add_argument("target", help="module/path, for example 一致性, api/core, RFM, RFM/SIM")
    parser.add_argument("--repo-root", help="oneos-sim repo root; default: discover from cwd")
    parser.add_argument("--testcase-root", help="simmaster_auto_lite/testcase root; default: sibling of repo root")
    parser.add_argument("--parallel", action="store_true", help="run immediate child directories concurrently")
    parser.add_argument("--jobs", type=int, help=f"parallel worker limit; default: min({DEFAULT_MAX_JOBS}, cpu, shards)")
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        help="parallel shard name to run; repeat or use comma-separated names",
    )
    parser.add_argument("--run-id", help="parallel run directory id; default: timestamp, pid, and random suffix")
    parser.add_argument("--fresh-state", action="store_true", help="do not reuse/copy the default QEMU persist image")
    parser.add_argument("--no-init-baseline", action="store_true", help="do not auto-run qemu_init.py when no baseline bin exists")
    parser.add_argument("--reinit-baseline", action="store_true", help="always create a run-local baseline instead of reusing the default bin")
    parser.add_argument("--keep-going", action="store_true", help="continue within each run after script failures")
    parser.add_argument("--quiet", action="store_true", help="pass --quiet to run_xc_script.py")
    parser.add_argument("--dry-run", action="store_true", help="print resolution and commands without running QEMU")
    parser.add_argument("--python", default=os.environ.get("PYTHON", "python3"), help="Python executable")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        repo_root = Path(args.repo_root).expanduser().resolve() if args.repo_root else _find_repo_root(Path.cwd())
        if not (repo_root / "Script" / "qemu" / "run_xc_script.py").is_file():
            raise FileNotFoundError(f"run_xc_script.py not found under repo root: {repo_root}")
        testcase_root = _resolve_testcase_root(repo_root, args.testcase_root)
        target = _resolve_target(args.target, testcase_root)
    except Exception as exc:
        print(f"[oneos-qemu-xc] error: {exc}", file=sys.stderr)
        return 2

    _print_resolution(target, testcase_root, repo_root)
    if args.parallel:
        return _run_parallel(args, repo_root, testcase_root, target)
    return _run_sequential(args, repo_root, target)


if __name__ == "__main__":
    raise SystemExit(main())

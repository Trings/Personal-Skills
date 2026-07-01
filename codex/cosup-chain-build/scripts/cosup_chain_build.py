#!/usr/bin/env python3
"""Build COSA -> COSB -> COSC mask/COS artifacts in upgrade order."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


QZVM_DIR = Path("COS/components/qzvm/core/arch/qzvm")
QZVM_HOME_ARCH_DIR = Path("qzvm/core/arch/qzvm")
MASK_OUTPUT_NAMES = (
    "qzvm_mask.c",
    "qzvm_java_native_methods.c",
    "qzvm_java_native_methods.h",
    "qzvm_opcode.h",
)
SDK_EXECUTABLE_NAMES = (
    "javac",
    "qzvmdk_linux",
    "qzsim_linux",
)
THIS_DIR = Path(__file__).resolve().parent
GEN_STATIC_SYMBOL = THIS_DIR / "gen_qzvm_static_symbol.py"


@dataclass(frozen=True)
class Stage:
    name: str
    fw_branch: str
    cos_branch: str
    symbol_input_from: str | None
    partition: str


STAGES: tuple[Stage, ...] = (
    Stage("COSA", "cosup_COSA_V1", "dev_4.0_COSA", None, "A"),
    Stage("COSB", "cosup_COSB_V1", "dev_4.0_COSB", "COSA", "B"),
    Stage("COSC", "cosup_COSC_V1", "dev_4.0_COSC", "COSB", "A"),
)


class CommandError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Automate the manual COS upgrade chain: build fw mask, pass symbols.txt "
            "forward, generate qzvm_static_symbol.c, and build COS."
        )
    )
    parser.add_argument(
        "--slpvmfw",
        type=Path,
        default=Path("../slpvmfw"),
        help="slpvmfw repository path",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("build/cosup_chain"),
        help="directory used for per-stage artifacts",
    )
    parser.add_argument(
        "--stages",
        default="COSA,COSB,COSC",
        help="comma-separated stages to run, selected from COSA,COSB,COSC",
    )
    parser.add_argument(
        "--cos-ref",
        help="override the oneos-sim ref used for every selected COS stage",
    )
    parser.add_argument(
        "--fw-ref",
        help="override the slpvmfw ref used for every selected fw stage",
    )
    parser.add_argument(
        "--cos-stage-ref",
        action="append",
        default=[],
        metavar="STAGE=REF",
        help="override oneos-sim ref for one stage; can be repeated",
    )
    parser.add_argument(
        "--fw-stage-ref",
        action="append",
        default=[],
        metavar="STAGE=REF",
        help="override slpvmfw ref for one stage; can be repeated",
    )
    parser.add_argument(
        "--jobs",
        default=os.environ.get("JOBS"),
        help="parallel jobs for COS CMake build; defaults to build script behavior",
    )
    parser.add_argument(
        "--cos-build-script",
        type=Path,
        default=Path("build_cc2560a_qemu.sh"),
        help="COS build script to run from oneos-sim root",
    )
    parser.add_argument(
        "--qzvm-sdk-home",
        type=Path,
        default=os.environ.get("QZVM_SDK_HOME"),
        help="SDK root used as QZVM_SDK_HOME for fw ant builds",
    )
    parser.add_argument(
        "--qzvm-home",
        type=Path,
        default=os.environ.get("QZVM_HOME"),
        help=(
            "QZVM_HOME used for fw ant builds; defaults to the current environment, "
            "or this repo's COS/components when QZVM_HOME is unset"
        ),
    )
    parser.add_argument(
        "--xctools-exe",
        type=Path,
        default=os.environ.get("XCTOOLS_EXE"),
        help="optional XCTOOLS_EXE override for fw ant builds",
    )
    parser.add_argument(
        "--cos-build-arg",
        action="append",
        default=[],
        help="extra argument passed to the COS build script; can be repeated",
    )
    parser.add_argument(
        "--riscv-toolchain-bin",
        type=Path,
        default=os.environ.get("RISCV_TOOLCHAIN_BIN"),
        help=(
            "directory containing riscv64-unknown-elf-* tools for generated "
            "CMakeUserPresets.json; defaults to RISCV_TOOLCHAIN_BIN, PATH, "
            "or ~/toolchains/gcc/bin"
        ),
    )
    parser.add_argument(
        "--git-pull",
        action="store_true",
        help=(
            "fetch origin and fast-forward required oneos-sim/slpvmfw branches when "
            "safe; worktrees are then created from origin/<branch>"
        ),
    )
    parser.add_argument(
        "--no-git-pull",
        action="store_true",
        help="deprecated compatibility option; this is now the default behavior",
    )
    parser.add_argument(
        "--no-local-cmake-preset",
        action="store_true",
        help="do not generate COS/projects/cc2560a/CMakeUserPresets.json",
    )
    parser.add_argument(
        "--skip-fw-build",
        action="store_true",
        help="reuse existing build/mask/qzvm_mask.c and symbols.txt in each fw worktree",
    )
    parser.add_argument(
        "--skip-cos-build",
        action="store_true",
        help="generate/copy COS sources but do not run the COS CMake build",
    )
    parser.add_argument(
        "--no-clean-fw",
        action="store_true",
        help="run ant without ant clean",
    )
    parser.add_argument(
        "--preserve-fw-debug-env",
        action="store_true",
        help=(
            "preserve DEBUG for fw ant builds; by default DEBUG is removed because "
            "slpvmfw/build_common.xml treats any DEBUG value as debug=true"
        ),
    )
    parser.add_argument(
        "--keep-qzvm-home-outputs",
        action="store_true",
        help="do not restore QZVM_HOME qzvm mask files after each fw build",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="allow dirty generated worktrees before building",
    )
    parser.add_argument(
        "--force-recreate-worktrees",
        action="store_true",
        help="remove and recreate generated worktrees under --out-dir",
    )
    parser.add_argument(
        "--strict-symbols",
        action="store_true",
        help="fail if a final symbol is missing from the current mask staticinit",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print commands without changing branches or files",
    )
    return parser.parse_args()


def normalize_optional_path(value: Path | str | None) -> Path | None:
    if value is None:
        return None
    return Path(value)


def ant_tool_prefix(path: Path) -> Path:
    """build_common.xml appends the platform suffix to XCTOOLS_EXE."""
    raw = path.as_posix()
    for suffix in ("_linux", "_mac", "_win.exe"):
        if raw.endswith(suffix):
            return Path(raw[: -len(suffix)])
    return path


def ensure_sdk_executables(sdk_home: Path, dry_run: bool) -> None:
    for name in SDK_EXECUTABLE_NAMES:
        path = sdk_home / "bin" / name
        if not path.exists():
            raise CommandError(f"required SDK executable does not exist: {path}")
        if os.access(path, os.X_OK):
            continue

        print(f"+ chmod +x {path}")
        if dry_run:
            continue
        path.chmod(path.stat().st_mode | 0o111)


def find_riscv_toolchain_bin(configured: Path | None) -> Path | None:
    if configured:
        return configured.resolve()

    gcc = shutil.which("riscv64-unknown-elf-gcc")
    if gcc:
        return Path(gcc).resolve().parent

    candidate = Path.home() / "toolchains/gcc/bin"
    if (candidate / "riscv64-unknown-elf-gcc").exists():
        return candidate

    return None


def riscv_tool(toolchain_bin: Path, name: str) -> str:
    path = toolchain_bin / name
    if not path.exists():
        raise CommandError(f"required RISC-V tool does not exist: {path}")
    return path.as_posix()


def make_cmake_user_presets(toolchain_bin: Path, jobs: str | None) -> str:
    job_count = int(jobs) if jobs and jobs.isdigit() else os.cpu_count() or 8
    gcc = riscv_tool(toolchain_bin, "riscv64-unknown-elf-gcc")
    return json.dumps(
        {
            "version": 3,
            "cmakeMinimumRequired": {"major": 3, "minor": 22, "patch": 1},
            "configurePresets": [
                {
                    "name": "cc2560a-dev-local",
                    "displayName": "cc2560a dev local",
                    "description": "Generated local Linux tool paths",
                    "inherits": "cc2560a-dev",
                    "generator": "Unix Makefiles",
                    "cacheVariables": {
                        "CMAKE_C_COMPILER": gcc,
                        "CMAKE_ASM_COMPILER": gcc,
                        "CMAKE_MAKE_PROGRAM": "/usr/bin/make",
                        "CMAKE_AR": riscv_tool(toolchain_bin, "riscv64-unknown-elf-ar"),
                        "CMAKE_RANLIB": riscv_tool(toolchain_bin, "riscv64-unknown-elf-ranlib"),
                        "CMAKE_OBJCOPY": riscv_tool(toolchain_bin, "riscv64-unknown-elf-objcopy"),
                        "CMAKE_OBJDUMP": riscv_tool(toolchain_bin, "riscv64-unknown-elf-objdump"),
                    },
                },
                {
                    "name": "cc2560a-qemu-local",
                    "displayName": "cc2560a qemu local",
                    "description": "Generated local Linux tool paths",
                    "inherits": "cc2560a-qemu",
                    "generator": "Unix Makefiles",
                    "cacheVariables": {
                        "CMAKE_C_COMPILER": gcc,
                        "CMAKE_ASM_COMPILER": gcc,
                        "CMAKE_MAKE_PROGRAM": "/usr/bin/make",
                        "CMAKE_AR": riscv_tool(toolchain_bin, "riscv64-unknown-elf-ar"),
                        "CMAKE_RANLIB": riscv_tool(toolchain_bin, "riscv64-unknown-elf-ranlib"),
                        "CMAKE_OBJCOPY": riscv_tool(toolchain_bin, "riscv64-unknown-elf-objcopy"),
                        "CMAKE_OBJDUMP": riscv_tool(toolchain_bin, "riscv64-unknown-elf-objdump"),
                    },
                },
            ],
            "buildPresets": [
                {
                    "name": "cc2560a-dev-local",
                    "configurePreset": "cc2560a-dev-local",
                    "jobs": job_count,
                },
                {
                    "name": "cc2560a-qemu-local",
                    "configurePreset": "cc2560a-qemu-local",
                    "jobs": job_count,
                },
            ],
        },
        indent=2,
    ) + "\n"


def ensure_local_cmake_preset(repo: Path, args: argparse.Namespace) -> None:
    if args.no_local_cmake_preset:
        return

    toolchain_bin = find_riscv_toolchain_bin(args.riscv_toolchain_bin)
    if toolchain_bin is None:
        raise CommandError(
            "cannot find riscv64-unknown-elf-gcc; set --riscv-toolchain-bin "
            "or RISCV_TOOLCHAIN_BIN"
        )

    preset_path = repo / "COS/projects/cc2560a/CMakeUserPresets.json"
    text = make_cmake_user_presets(toolchain_bin, args.jobs)
    print(f"+ write {preset_path}")
    if args.dry_run:
        return
    write_text_if_changed(preset_path, text)


def run(cmd: list[str], cwd: Path, dry_run: bool = False, env: dict[str, str] | None = None) -> None:
    display_cwd = cwd.as_posix()
    print(f"+ (cd {display_cwd} && {' '.join(cmd)})", flush=True)
    if dry_run:
        return
    result = subprocess.run(cmd, cwd=cwd, env=env)
    if result.returncode != 0:
        raise CommandError(f"command failed with exit code {result.returncode}: {' '.join(cmd)}")


def capture(cmd: list[str], cwd: Path) -> str:
    result = subprocess.run(cmd, cwd=cwd, check=True, text=True, stdout=subprocess.PIPE)
    return result.stdout.strip()


def ref_exists(repo: Path, ref: str) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        cwd=repo,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def status_short(repo: Path) -> str:
    return capture(["git", "status", "--short"], repo)


def branch(repo: Path) -> str:
    return capture(["git", "branch", "--show-current"], repo)


def rev_parse(repo: Path, ref: str) -> str:
    return capture(["git", "rev-parse", f"{ref}^{{commit}}"], repo)


def ensure_clean(repo: Path, allow_dirty: bool) -> None:
    status = status_short(repo)
    if status and not allow_dirty:
        raise CommandError(
            f"{repo} has local changes; clean it or pass --allow-dirty:\n{status}"
        )


def resolve_ref(repo: Path, wanted: str, args: argparse.Namespace) -> str:
    remote_branch = f"origin/{wanted}"
    if args.git_pull and ref_exists(repo, remote_branch):
        return remote_branch

    if ref_exists(repo, wanted):
        return wanted

    if ref_exists(repo, remote_branch):
        print(
            f"[git] {repo.name}:{wanted} has no local branch; "
            f"using existing remote ref {remote_branch}"
        )
        return remote_branch

    raise CommandError(f"branch {wanted} not found in {repo}")


def checked_out_branch_paths(repo: Path) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    current_path: Path | None = None
    for line in capture(["git", "worktree", "list", "--porcelain"], repo).splitlines():
        if line.startswith("worktree "):
            current_path = Path(line.removeprefix("worktree "))
            continue
        if line.startswith("branch refs/heads/") and current_path is not None:
            branch_name = line.removeprefix("branch refs/heads/")
            paths[branch_name] = current_path
    return paths


def is_ancestor(repo: Path, older: str, newer: str) -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", older, newer],
        cwd=repo,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def update_local_branch_from_remote(repo: Path, branch_name: str, args: argparse.Namespace) -> None:
    remote_branch = f"origin/{branch_name}"
    if not ref_exists(repo, remote_branch):
        raise CommandError(f"remote branch {remote_branch} not found in {repo}")

    if not ref_exists(repo, branch_name):
        print(f"[git] {repo.name}:{branch_name} has no local branch; build will use {remote_branch}")
        return

    local_rev = rev_parse(repo, branch_name)
    remote_rev = rev_parse(repo, remote_branch)
    if local_rev == remote_rev:
        print(f"[git] {repo.name}:{branch_name} is up to date")
        return

    checked_out_paths = checked_out_branch_paths(repo)
    checked_out_path = checked_out_paths.get(branch_name)
    if checked_out_path is not None:
        if status_short(checked_out_path):
            print(
                f"[git] {repo.name}:{branch_name} is checked out with local changes; "
                f"skip local pull and build from {remote_branch}"
            )
            return
        run(["git", "pull", "--ff-only", "origin", branch_name], checked_out_path, args.dry_run)
        return

    if is_ancestor(repo, branch_name, remote_branch):
        run(["git", "branch", "-f", branch_name, remote_branch], repo, args.dry_run)
        return

    print(
        f"[git] {repo.name}:{branch_name} is not a fast-forward of {remote_branch}; "
        f"leave local branch unchanged and build from {remote_branch}"
    )


def update_required_refs(repo: Path, refs: list[str], args: argparse.Namespace) -> None:
    unique_refs = sorted(set(refs))
    if not args.git_pull:
        print(f"[git] skip fetch/pull for {repo}; pass --git-pull to update from origin")
        return

    run(["git", "fetch", "--prune", "origin"], repo, args.dry_run)
    for ref in unique_refs:
        if ref_exists(repo, f"origin/{ref}"):
            update_local_branch_from_remote(repo, ref, args)
        else:
            print(f"[git] {repo.name}:{ref} is not a remote branch; skip pull")


def is_git_worktree(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=path,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def ensure_worktree(
    source_repo: Path,
    wanted_ref: str,
    worktree_path: Path,
    args: argparse.Namespace,
) -> Path:
    ref = resolve_ref(source_repo, wanted_ref, args)
    wanted_rev = rev_parse(source_repo, ref)

    if worktree_path.exists() and args.force_recreate_worktrees:
        if is_git_worktree(worktree_path):
            run(["git", "worktree", "remove", "--force", worktree_path.as_posix()], source_repo, args.dry_run)
        else:
            print(f"+ rm -rf {worktree_path}")
            if not args.dry_run:
                shutil.rmtree(worktree_path)

    if not worktree_path.exists():
        run(
            ["git", "worktree", "add", "--detach", worktree_path.as_posix(), ref],
            source_repo,
            args.dry_run,
        )
        return worktree_path

    if not is_git_worktree(worktree_path):
        raise CommandError(f"{worktree_path} exists but is not a git worktree")

    current_rev = rev_parse(worktree_path, "HEAD")
    if current_rev != wanted_rev:
        raise CommandError(
            f"{worktree_path} is not at {wanted_ref}; pass --force-recreate-worktrees to rebuild it"
        )
    ensure_clean(worktree_path, args.allow_dirty)
    return worktree_path


def selected_stages(raw: str) -> list[Stage]:
    by_name = {stage.name: stage for stage in STAGES}
    names = [name.strip().upper() for name in raw.split(",") if name.strip()]
    unknown = [name for name in names if name not in by_name]
    if unknown:
        raise CommandError(f"unknown stage(s): {', '.join(unknown)}")
    return [by_name[name] for name in names]


def parse_stage_ref_overrides(raw_items: list[str], option_name: str) -> dict[str, str]:
    result: dict[str, str] = {}
    valid_stages = {stage.name for stage in STAGES}
    for item in raw_items:
        if "=" not in item:
            raise CommandError(f"{option_name} must be STAGE=REF, got: {item}")
        stage_name, ref = item.split("=", 1)
        stage_name = stage_name.strip().upper()
        ref = ref.strip()
        if stage_name not in valid_stages:
            raise CommandError(f"{option_name} has unknown stage: {stage_name}")
        if not ref:
            raise CommandError(f"{option_name} has empty ref for {stage_name}")
        result[stage_name] = ref
    return result


def effective_fw_ref(stage: Stage, args: argparse.Namespace) -> str:
    return args.fw_stage_refs.get(stage.name) or args.fw_ref or stage.fw_branch


def effective_cos_ref(stage: Stage, args: argparse.Namespace) -> str:
    return args.cos_stage_refs.get(stage.name) or args.cos_ref or stage.cos_branch


def copy_file(src: Path, dst: Path, dry_run: bool) -> None:
    print(f"+ cp {src} {dst}")
    if dry_run:
        return
    if not src.exists():
        raise CommandError(f"required file does not exist: {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def copy_tree_files(src_dir: Path, dst_dir: Path, names: list[str], dry_run: bool) -> None:
    for name in names:
        copy_file(src_dir / name, dst_dir / name, dry_run)


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


def write_symbols_for_fw(stage: Stage, slpvmfw: Path, artifacts: dict[str, object], dry_run: bool) -> None:
    if stage.symbol_input_from is None:
        return

    src = artifacts.get(f"{stage.symbol_input_from}:symbols")
    if not isinstance(src, Path):
        raise CommandError(
            f"{stage.name} needs symbols from {stage.symbol_input_from}, but that stage was not run"
        )
    copy_file(src, slpvmfw / "symbols.txt", dry_run)


def qzvm_home_arch_dir(qzvm_home: Path) -> Path:
    return qzvm_home / QZVM_HOME_ARCH_DIR


def backup_qzvm_home_outputs(stage: Stage, args: argparse.Namespace) -> dict[str, Path | None]:
    backup_dir = args.out_dir / "backups" / stage.name / "qzvm_home_prebuild"
    install_dir = qzvm_home_arch_dir(args.qzvm_home)
    backups: dict[str, Path | None] = {}

    for name in MASK_OUTPUT_NAMES:
        src = install_dir / name
        dst = backup_dir / name
        if src.exists():
            print(f"+ backup {src} {dst}")
            backups[name] = dst
            if not args.dry_run:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        else:
            backups[name] = None

    return backups


def restore_qzvm_home_outputs(backups: dict[str, Path | None], args: argparse.Namespace) -> None:
    if args.keep_qzvm_home_outputs:
        return

    install_dir = qzvm_home_arch_dir(args.qzvm_home)
    for name, backup in backups.items():
        dst = install_dir / name
        if backup is None:
            print(f"+ restore-remove {dst}")
            if not args.dry_run and dst.exists():
                dst.unlink()
            continue

        print(f"+ restore {backup} {dst}")
        if not args.dry_run:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, dst)


def available_mask_output_names(source_dirs: list[Path]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for name in MASK_OUTPUT_NAMES:
        if any((source_dir / name).exists() for source_dir in source_dirs):
            names.append(name)
            seen.add(name)
    return names


def build_fw(stage: Stage, slpvmfw: Path, args: argparse.Namespace) -> None:
    if args.skip_fw_build:
        print(f"[{stage.name}] skip fw build, reusing {slpvmfw / 'build/mask/qzvm_mask.c'}")
        return

    env = os.environ.copy()
    if args.qzvm_sdk_home:
        env["QZVM_SDK_HOME"] = str(args.qzvm_sdk_home.resolve())
    env["QZVM_HOME"] = str(args.qzvm_home.resolve())
    if args.xctools_exe:
        env["XCTOOLS_EXE"] = str(ant_tool_prefix(args.xctools_exe.resolve()))
    if not args.preserve_fw_debug_env:
        env.pop("DEBUG", None)

    print(f"[{stage.name}] fw QZVM_HOME={env['QZVM_HOME']}")
    print(f"[{stage.name}] fw QZVM_SDK_HOME={env.get('QZVM_SDK_HOME', '')}")
    print(f"[{stage.name}] fw DEBUG={env.get('DEBUG', '<unset>')}")

    ensure_sdk_executables(Path(env["QZVM_SDK_HOME"]), args.dry_run)

    if not args.no_clean_fw:
        run(["ant", "clean"], slpvmfw, dry_run=args.dry_run, env=env)
    run(["ant"], slpvmfw, dry_run=args.dry_run, env=env)


def sync_fw_outputs(stage: Stage, slpvmfw: Path, args: argparse.Namespace) -> dict[str, object]:
    stage_dir = args.out_dir / stage.name
    install_dir = qzvm_home_arch_dir(args.qzvm_home)
    build_mask_dir = slpvmfw / "build/mask"
    mask_source_dir = install_dir if not args.skip_fw_build else build_mask_dir
    if not (mask_source_dir / "qzvm_mask.c").exists():
        mask_source_dir = build_mask_dir

    mask_names = available_mask_output_names([mask_source_dir, build_mask_dir])
    if "qzvm_mask.c" not in mask_names:
        raise CommandError(
            f"cannot find qzvm_mask.c in {mask_source_dir} or {build_mask_dir}"
        )

    mask_src = mask_source_dir / "qzvm_mask.c"
    symbols_src = slpvmfw / "symbols.txt"
    mask_dst = stage_dir / "qzvm_mask.c"
    symbols_dst = stage_dir / "symbols.txt"
    mask_dir_dst = stage_dir / "mask"
    copy_file(mask_src, mask_dst, args.dry_run)
    copy_file(symbols_src, symbols_dst, args.dry_run)
    copy_tree_files(mask_source_dir, mask_dir_dst, mask_names, args.dry_run)
    return {
        f"{stage.name}:mask": mask_dst,
        f"{stage.name}:mask_dir": mask_dir_dst,
        f"{stage.name}:mask_names": mask_names,
        f"{stage.name}:symbols": symbols_dst,
    }


def copy_mask_outputs_to_cos(stage: Stage, cos_repo: Path, artifacts: dict[str, object], args: argparse.Namespace) -> None:
    mask_dir = artifacts.get(f"{stage.name}:mask_dir")
    if mask_dir is None:
        copy_file(Path(artifacts[f"{stage.name}:mask"]), cos_repo / QZVM_DIR / "qzvm_mask.c", args.dry_run)
        return

    names = artifacts.get(f"{stage.name}:mask_names")
    if not isinstance(names, list):
        names = available_mask_output_names([Path(mask_dir)])
    copy_tree_files(Path(mask_dir), cos_repo / QZVM_DIR, names, args.dry_run)


def generate_static_symbols(
    stage: Stage,
    repo: Path,
    artifacts: dict[str, object],
    args: argparse.Namespace,
) -> Path:
    mask_path = QZVM_DIR / "qzvm_mask.c"
    stage_symbols_out = args.out_dir / stage.name / "static_symbols.txt"
    output_path = QZVM_DIR / "qzvm_static_symbol.c"
    cmd = [
        sys.executable,
        GEN_STATIC_SYMBOL.as_posix(),
        mask_path.as_posix(),
        "-o",
        output_path.as_posix(),
        "--symbols-out",
        stage_symbols_out.as_posix(),
    ]
    if stage.symbol_input_from is not None:
        base_symbols = artifacts.get(f"{stage.symbol_input_from}:static_symbols")
        if not isinstance(base_symbols, Path):
            raise CommandError(
                f"{stage.name} needs static symbols from {stage.symbol_input_from}, "
                "but that stage was not run"
            )
        cmd.extend(["--base-symbols", base_symbols.as_posix()])
    if args.strict_symbols:
        cmd.append("--strict")
    run(cmd, repo, dry_run=args.dry_run)
    return stage_symbols_out


def build_cos(stage: Stage, repo: Path, args: argparse.Namespace) -> None:
    if args.skip_cos_build:
        print(f"[{stage.name}] skip COS build")
        return

    ensure_local_cmake_preset(repo, args)
    cmd = ["bash", args.cos_build_script.as_posix()]
    if args.jobs:
        cmd.extend(["--jobs", args.jobs])
    cmd.extend(args.cos_build_arg)
    run(cmd, repo, dry_run=args.dry_run)


def save_cos_outputs(stage: Stage, repo: Path, out_dir: Path, dry_run: bool) -> None:
    build_dir = repo / "build/cc2560a_qemu"
    stage_dir = out_dir / stage.name
    candidates = [
        build_dir / "bin/cc2560a_qemu.elf",
        build_dir / "bin/cc2560a_qemu.bin",
        build_dir / "bin/cc2560a_qemu.hex",
        build_dir / "cc2560a_qemu.elf",
        build_dir / "cc2560a_qemu.bin",
        build_dir / "cc2560a_qemu.hex",
    ]
    for src in candidates:
        if src.exists() or dry_run:
            copy_file(src, stage_dir / src.name, dry_run)


def main() -> int:
    args = parse_args()
    repo = Path.cwd()
    args.slpvmfw = Path(args.slpvmfw)
    args.out_dir = Path(args.out_dir)
    args.cos_build_script = Path(args.cos_build_script)
    args.qzvm_sdk_home = normalize_optional_path(args.qzvm_sdk_home)
    args.qzvm_home = normalize_optional_path(args.qzvm_home) or (repo / "COS/components")
    args.xctools_exe = normalize_optional_path(args.xctools_exe)
    args.riscv_toolchain_bin = normalize_optional_path(args.riscv_toolchain_bin)
    args.cos_stage_refs = parse_stage_ref_overrides(args.cos_stage_ref, "--cos-stage-ref")
    args.fw_stage_refs = parse_stage_ref_overrides(args.fw_stage_ref, "--fw-stage-ref")
    slpvmfw = args.slpvmfw.resolve()
    out_dir = args.out_dir.resolve()
    args.out_dir = out_dir
    oneos_worktrees = out_dir / "worktrees/oneos-sim"
    fw_worktrees = out_dir / "worktrees/slpvmfw"

    if not slpvmfw.exists():
        raise CommandError(f"slpvmfw repository not found: {slpvmfw}")

    stages = selected_stages(args.stages)
    print(f"oneos-sim: {repo} ({branch(repo)})")
    print(f"slpvmfw:   {slpvmfw} ({branch(slpvmfw)})")
    print("stages:    " + " -> ".join(stage.name for stage in stages))

    update_required_refs(slpvmfw, [effective_fw_ref(stage, args) for stage in stages], args)
    update_required_refs(repo, [effective_cos_ref(stage, args) for stage in stages], args)

    artifacts: dict[str, object] = {}
    for stage in stages:
        fw_ref = effective_fw_ref(stage, args)
        cos_ref = effective_cos_ref(stage, args)
        print(f"\n== {stage.name}: fw {fw_ref}, cos {cos_ref} ==")
        fw_repo = ensure_worktree(slpvmfw, fw_ref, fw_worktrees / stage.name, args)
        cos_repo = ensure_worktree(repo, cos_ref, oneos_worktrees / stage.name, args)

        if args.dry_run:
            if stage.symbol_input_from is not None:
                write_symbols_for_fw(stage, fw_repo, artifacts, args.dry_run)
            build_fw(stage, fw_repo, args)
            print(f"[{stage.name}] dry-run stops before syncing generated artifacts")
            artifacts[f"{stage.name}:symbols"] = args.out_dir / stage.name / "symbols.txt"
            artifacts[f"{stage.name}:static_symbols"] = args.out_dir / stage.name / "static_symbols.txt"
            continue

        if args.skip_fw_build:
            build_fw(stage, fw_repo, args)
            artifacts.update(sync_fw_outputs(stage, fw_repo, args))
        else:
            backups = backup_qzvm_home_outputs(stage, args)
            try:
                write_symbols_for_fw(stage, fw_repo, artifacts, args.dry_run)
                build_fw(stage, fw_repo, args)
                artifacts.update(sync_fw_outputs(stage, fw_repo, args))
            finally:
                restore_qzvm_home_outputs(backups, args)

        copy_mask_outputs_to_cos(stage, cos_repo, artifacts, args)
        artifacts[f"{stage.name}:static_symbols"] = generate_static_symbols(stage, cos_repo, artifacts, args)
        build_cos(stage, cos_repo, args)
        if not args.skip_cos_build:
            save_cos_outputs(stage, cos_repo, out_dir, args.dry_run)

    print("\nchain build complete")
    print(f"artifacts: {out_dir}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CommandError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)

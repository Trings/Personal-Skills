#!/usr/bin/env python3
"""Create, build, install, and APDU-test Java-App applets with oneos-sim QEMU."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence


DEFAULT_ISD_AID = "D1560001010001600000000100000000"
DEFAULT_SCP02_KEY = "404142434445464748494A4B4C4D4E4F"
DEFAULT_APDUS = (
    ("001000000100", "9900"),
    ("001000000101", "9901"),
)


@dataclass(frozen=True)
class AppletOpt:
    applet_aid: str
    package_aid: str
    package_name: str
    class_name: str
    full_class_name: str
    version: str

    @property
    def default_module_aid(self) -> str:
        return self.applet_aid


@dataclass(frozen=True)
class ApduCase:
    apdu: str
    expected_sw: str


@dataclass(frozen=True)
class ApduResult:
    apdu: str
    expected_sw: str
    actual_sw: str
    data: str
    passed: bool
    error: str = ""


def _print(message: str) -> None:
    print(f"[java-app] {message}", flush=True)


def _quote_command(args: Sequence[object]) -> str:
    return " ".join(subprocess.list2cmdline([str(arg)]) for arg in args)


def _safe_name(value: str) -> str:
    sanitized = re.sub(r"[^0-9A-Za-z._-]+", "_", value.strip())
    return sanitized.strip("._-") or "app"


def _command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def _normalize_hex(value: str, *, field: str = "hex") -> str:
    text = str(value).strip()
    if not text:
        raise ValueError(f"{field} must not be empty")

    if "0x" in text.lower():
        parts = re.findall(r"0x([0-9A-Fa-f]+)", text)
        if not parts:
            raise ValueError(f"{field} has no 0x byte values: {value!r}")
        data = bytes(int(part, 16) for part in parts)
        return data.hex().upper()

    compact = "".join(ch for ch in text.upper() if ch in "0123456789ABCDEF")
    if len(compact) % 2 != 0:
        raise ValueError(f"{field} hex length must be even: {value!r}")
    if not compact:
        raise ValueError(f"{field} has no hex digits: {value!r}")
    return compact


def _format_opt_aid(aid_hex: str) -> str:
    aid_hex = _normalize_hex(aid_hex, field="AID")
    return ":".join(f"0x{int(aid_hex[index:index + 2], 16):02x}" for index in range(0, len(aid_hex), 2))


def _validate_aid(aid_hex: str, *, field: str) -> str:
    normalized = _normalize_hex(aid_hex, field=field)
    length = len(normalized) // 2
    if length < 5 or length > 16:
        raise ValueError(f"{field} must be 5..16 bytes, got {length}: {aid_hex!r}")
    return normalized


def _java_package_from_app_name(app_name: str) -> str:
    compact = re.sub(r"[^0-9A-Za-z]+", "", app_name).lower()
    if not compact:
        compact = "app"
    if compact[0].isdigit():
        compact = f"app{compact}"
    return compact


def _java_class_from_app_name(app_name: str) -> str:
    words = re.findall(r"[0-9A-Za-z]+", app_name)
    class_name = "".join(word[:1].upper() + word[1:] for word in words) or "App"
    if class_name[0].isdigit():
        class_name = f"App{class_name}"
    if not class_name.endswith("Applet"):
        class_name += "Applet"
    return class_name


def _validate_java_package(package_name: str) -> str:
    parts = package_name.split(".")
    ident = re.compile(r"^[A-Za-z_$][0-9A-Za-z_$]*$")
    if not parts or any(not ident.match(part) for part in parts):
        raise ValueError(f"invalid Java package name: {package_name!r}")
    return package_name


def _validate_java_class(class_name: str) -> str:
    if not re.match(r"^[A-Za-z_$][0-9A-Za-z_$]*$", class_name):
        raise ValueError(f"invalid Java class name: {class_name!r}")
    return class_name


def _default_package_aid(app_name: str) -> str:
    digest = hashlib.sha1(app_name.encode("utf-8")).hexdigest().upper()
    return f"A0000000620301{digest[:4]}"


def _default_applet_aid(package_aid: str) -> str:
    package_aid = _validate_aid(package_aid, field="package AID")
    candidate = f"{package_aid}01"
    return _validate_aid(candidate, field="applet AID")


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


def _find_oneos_root(start: Path, override: str | None = None) -> Path:
    if override:
        root = Path(override).expanduser().resolve()
        if (root / "Script" / "qemu" / "CustomerLib.py").is_file():
            return root
        raise FileNotFoundError(f"not a oneos-sim root: {root}")

    current = start.resolve()
    if current.is_file():
        current = current.parent

    for candidate in (current, *current.parents):
        if (candidate / "Script" / "qemu" / "CustomerLib.py").is_file():
            return candidate

    for candidate in (current, *current.parents):
        sibling = candidate / "oneos-sim"
        if (sibling / "Script" / "qemu" / "CustomerLib.py").is_file():
            return sibling.resolve()

    try:
        git_root = Path(_run_capture(["git", "rev-parse", "--show-toplevel"], current)).resolve()
    except Exception as exc:
        raise FileNotFoundError("cannot find oneos-sim root; pass --oneos-root") from exc

    if (git_root / "Script" / "qemu" / "CustomerLib.py").is_file():
        return git_root
    sibling = git_root.parent / "oneos-sim"
    if (sibling / "Script" / "qemu" / "CustomerLib.py").is_file():
        return sibling.resolve()
    raise FileNotFoundError("cannot find oneos-sim root; pass --oneos-root")


def _find_java_app_root(start: Path, oneos_root: Path | None, override: str | None = None) -> Path:
    raw = override or os.environ.get("JAVA_APP_ROOT")
    if raw:
        root = Path(raw).expanduser().resolve()
        if (root / "simple_app").is_dir():
            return root
        raise FileNotFoundError(f"Java-App root missing simple_app: {root}")

    current = start.resolve()
    if current.is_file():
        current = current.parent

    for candidate in (current, *current.parents):
        if (candidate / "simple_app").is_dir() and candidate.name == "Java-App":
            return candidate

    if oneos_root is not None:
        sibling = oneos_root.parent / "Java-App"
        if (sibling / "simple_app").is_dir():
            return sibling.resolve()

    for candidate in (current, *current.parents):
        sibling = candidate / "Java-App"
        if (sibling / "simple_app").is_dir():
            return sibling.resolve()

    raise FileNotFoundError("cannot find Java-App root; pass --java-app-root")


def _resolve_app_dir(app: str, java_app_root: Path) -> Path:
    raw = Path(app).expanduser()
    candidates = []
    if raw.is_absolute():
        candidates.append(raw)
    else:
        candidates.append((Path.cwd() / raw).resolve())
        candidates.append((java_app_root / raw).resolve())

    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if (candidate / "build.xml").is_file() and (candidate / "applet.opt").is_file():
            return candidate

    checked = "\n  ".join(str(path) for path in candidates)
    raise FileNotFoundError(f"Java app not found for {app!r}. Checked:\n  {checked}")


def _read_applet_opt(app_dir: Path) -> AppletOpt:
    opt_path = app_dir / "applet.opt"
    lines = [
        line.strip()
        for line in opt_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if len(lines) < 3:
        raise ValueError(f"{opt_path} must contain at least 3 non-empty lines")

    first = lines[0].split()
    if len(first) < 3 or first[0] != "-applet":
        raise ValueError(f"unsupported first applet.opt line: {lines[0]!r}")

    third = lines[2].split()
    if len(third) < 2:
        raise ValueError(f"unsupported package applet.opt line: {lines[2]!r}")

    full_class = first[2]
    package_name = lines[1]
    if "." in full_class:
        class_package, class_name = full_class.rsplit(".", 1)
    else:
        class_package, class_name = package_name, full_class

    if class_package != package_name:
        _print(f"warning: applet.opt package {package_name!r} differs from class package {class_package!r}")

    return AppletOpt(
        applet_aid=_validate_aid(first[1], field="applet AID"),
        package_aid=_validate_aid(third[0], field="package AID"),
        package_name=_validate_java_package(package_name),
        class_name=_validate_java_class(class_name),
        full_class_name=full_class,
        version=third[1],
    )


def _latest_ef(app_dir: Path) -> Path:
    build_dir = app_dir / "build"
    if not build_dir.is_dir():
        raise FileNotFoundError(f"build directory not found: {build_dir}")

    files = [path for path in build_dir.rglob("*") if path.is_file() and path.suffix.lower() in (".ef", ".xef")]
    if not files:
        raise FileNotFoundError(f"no .ef/.xef files found under {build_dir}")

    files.sort(key=lambda path: (path.suffix.lower() != ".ef", -path.stat().st_mtime))
    return files[0].resolve()


def _replace_text(path: Path, replacements: Iterable[tuple[str, str]]) -> None:
    text = path.read_text(encoding="utf-8")
    for old, new in replacements:
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")


def _create_app(args: argparse.Namespace) -> int:
    oneos_root = _find_oneos_root(Path.cwd(), args.oneos_root) if args.oneos_root else None
    java_app_root = _find_java_app_root(Path.cwd(), oneos_root, args.java_app_root)
    template = (java_app_root / args.template).resolve()
    if not template.is_dir():
        raise FileNotFoundError(f"template app not found: {template}")

    app_name = args.app_name
    destination = (java_app_root / app_name).resolve()
    package_name = _validate_java_package(args.package or _java_package_from_app_name(app_name))
    class_name = _validate_java_class(args.class_name or _java_class_from_app_name(app_name))
    package_aid = _validate_aid(args.package_aid or _default_package_aid(app_name), field="package AID")
    applet_aid = _validate_aid(args.applet_aid or _default_applet_aid(package_aid), field="applet AID")

    _print(f"Java-App root: {java_app_root}")
    _print(f"template: {template}")
    _print(f"new app: {destination}")
    _print(f"package: {package_name}")
    _print(f"class: {class_name}")
    _print(f"package AID: {package_aid}")
    _print(f"applet AID: {applet_aid}")

    if args.dry_run:
        _print("dry-run: no files created")
        return 0

    if destination.exists():
        raise FileExistsError(f"destination already exists: {destination}")

    shutil.copytree(template, destination)

    old_src = destination / "src" / "simpleapp"
    new_src = destination / "src" / Path(*package_name.split("."))
    if old_src != new_src:
        new_src.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(old_src), str(new_src))

    old_java = new_src / "SimpleApplet.java"
    new_java = new_src / f"{class_name}.java"
    if old_java != new_java:
        old_java.rename(new_java)

    _replace_text(
        new_java,
        (
            ("package simpleapp;", f"package {package_name};"),
            ("SimpleApplet", class_name),
        ),
    )

    opt_text = (
        f"-applet  {_format_opt_aid(applet_aid)} {package_name}.{class_name}\n"
        f"{package_name}\n"
        f"{_format_opt_aid(package_aid)} 1.0\n"
    )
    (destination / "applet.opt").write_text(opt_text, encoding="utf-8")

    _replace_text(
        destination / "build.xml",
        (('project name="simple-app"', f'project name="{app_name}"'),),
    )

    config_path = destination / "CONFIGURATION.md"
    if config_path.exists():
        _replace_text(
            config_path,
            (
                ("simple_app", app_name),
                ("manual_simple_app", app_name),
                ("simpleapp", package_name),
                ("SimpleApplet", class_name),
                ("0xa0:0x0:0x0:0x0:0x62:0x3:0x1:0xee:0x1:0x1", _format_opt_aid(applet_aid)),
                ("0xa0:0x0:0x0:0x0:0x62:0x3:0x1:0xee:0x1", _format_opt_aid(package_aid)),
            ),
        )

    _print(f"created: {destination}")
    return 0


def _ant_props(args: argparse.Namespace) -> list[str]:
    props: list[str] = []
    if getattr(args, "xcsdk", None):
        props.append(f"-Dxcsdk={Path(args.xcsdk).expanduser()}")
    if getattr(args, "xctool_path", None):
        props.append(f"-Dxctool_path={Path(args.xctool_path).expanduser()}")
    return props


def _run_streaming(cmd: Sequence[str], cwd: Path, *, dry_run: bool = False) -> None:
    _print(f"run: {_quote_command(cmd)}")
    if dry_run:
        return
    completed = subprocess.run(list(cmd), cwd=cwd, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"{_quote_command(cmd)} failed with {completed.returncode}")


def _build_app(args: argparse.Namespace) -> Path:
    oneos_root = _find_oneos_root(Path.cwd(), args.oneos_root) if args.oneos_root else None
    java_app_root = _find_java_app_root(Path.cwd(), oneos_root, args.java_app_root)
    app_dir = _resolve_app_dir(args.app, java_app_root)
    props = _ant_props(args)

    _print(f"app: {app_dir}")
    if not _command_exists(args.ant):
        raise FileNotFoundError(f"Ant executable not found: {args.ant}")

    if not args.no_clean:
        _run_streaming([args.ant, *props, "clean"], app_dir, dry_run=args.dry_run)
    _run_streaming([args.ant, *props], app_dir, dry_run=args.dry_run)

    if not args.dry_run:
        _print(f"output: {_latest_ef(app_dir)}")
    return app_dir


def _parse_apdu_spec(spec: str) -> ApduCase:
    stripped = spec.strip()
    if not stripped:
        raise ValueError("empty APDU spec")
    if "=" in stripped:
        apdu, sw = stripped.split("=", 1)
    else:
        parts = stripped.split()
        if len(parts) != 2:
            raise ValueError(f"APDU spec must be APDU=SW or APDU SW: {spec!r}")
        apdu, sw = parts
    return ApduCase(
        apdu=_normalize_hex(apdu, field="APDU"),
        expected_sw="|".join(_normalize_hex(item, field="SW") for item in sw.split("|")),
    )


def _read_apdu_file(path: Path) -> list[ApduCase]:
    cases: list[ApduCase] = []
    for line_no, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            cases.append(_parse_apdu_spec(line))
        except ValueError as exc:
            raise ValueError(f"{path}:{line_no}: {exc}") from exc
    return cases


def _apdu_cases(args: argparse.Namespace) -> list[ApduCase]:
    cases: list[ApduCase] = []
    for path_text in getattr(args, "apdu_file", []) or []:
        cases.extend(_read_apdu_file(Path(path_text).expanduser()))
    for spec in getattr(args, "apdu", []) or []:
        cases.append(_parse_apdu_spec(spec))
    if not cases:
        cases = [_parse_apdu_spec(f"{apdu}={sw}") for apdu, sw in DEFAULT_APDUS]
    return cases


def _default_workspace(oneos_root: Path, app_dir: Path) -> Path:
    return oneos_root / "build" / "cc2560a_qemu" / "java_app_skill" / _safe_name(app_dir.name)


def _prepare_qemu_layout(args: argparse.Namespace, oneos_root: Path, app_dir: Path) -> tuple[Path, Path]:
    workspace = Path(args.workspace).expanduser().resolve() if args.workspace else _default_workspace(oneos_root, app_dir)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = Path(args.run_dir).expanduser().resolve() if args.run_dir else workspace / "runs" / timestamp
    run_dir.mkdir(parents=True, exist_ok=True)

    state_path = Path(args.persist_path).expanduser().resolve() if args.persist_path else workspace / "qemu_state.bin"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    return state_path, run_dir


def _run_qemu_init(args: argparse.Namespace, oneos_root: Path, state_path: Path, run_dir: Path) -> tuple[Path, Path]:
    init_stdout = run_dir / "qemu_init.out"
    init_shared_log = run_dir / "qemu_init_shared.log"
    env = os.environ.copy()
    env["ONEOS_QEMU_PERSIST_PATH"] = str(state_path)
    env["ONEOS_QEMU_SHARED_LOG_PATH"] = str(init_shared_log)
    env.pop("ONEOS_QEMU_KEEP_STATE", None)

    cmd = [args.python, "Script/qemu/qemu_init.py"]
    _print(f"initialize QEMU state: {_quote_command(cmd)}")
    _print(f"init output: {init_stdout}")
    with init_stdout.open("w", encoding="utf-8") as output:
        completed = subprocess.run(
            cmd,
            cwd=oneos_root,
            env=env,
            text=True,
            stdout=output,
            stderr=subprocess.STDOUT,
            check=False,
        )
    if completed.returncode != 0:
        tail = init_stdout.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
        detail = "\n".join(tail)
        raise RuntimeError(f"qemu_init.py failed with {completed.returncode}; tail:\n{detail}")
    return init_stdout, init_shared_log


def _ensure_qemu_state(args: argparse.Namespace, oneos_root: Path, state_path: Path, run_dir: Path) -> dict[str, str]:
    init_stdout = run_dir / "qemu_init.out"
    init_shared_log = run_dir / "qemu_init_shared.log"

    if args.skip_init:
        if not state_path.exists():
            raise FileNotFoundError(f"QEMU state missing and --skip-init was used: {state_path}")
        _print(f"skip qemu_init.py; reuse initialized state: {state_path}")
        return {
            "init_ran": "false",
            "init_output_log": "",
            "init_shared_log": "",
        }

    if state_path.exists():
        _print(f"reinitialize QEMU state before app download: {state_path}")
        if not args.dry_run:
            state_path.unlink()
    else:
        _print(f"initialize QEMU state before app download: {state_path}")

    if not args.dry_run:
        init_stdout, init_shared_log = _run_qemu_init(args, oneos_root, state_path, run_dir)

    return {
        "init_ran": "true",
        "init_output_log": str(init_stdout),
        "init_shared_log": str(init_shared_log),
    }


def _check_sw(actual: str, expected: str) -> bool:
    allowed = [item.strip().upper() for item in expected.split("|") if item.strip()]
    return actual.upper() in allowed


def _manual_debug_script_text(
    *,
    app_name: str,
    applet_aid: str,
    select_sw: str,
    default_persist_relative: str | None,
    custom_persist: str | None,
    apdus: Sequence[ApduCase],
) -> str:
    apdu_items = [
        {
            "name": f"apdu_{index}",
            "apdu": case.apdu,
            "sw": case.expected_sw,
        }
        for index, case in enumerate(apdus, start=1)
    ]
    apdus_text = json.dumps(apdu_items, indent=4)
    template = textwrap.dedent(
        f"""\
        #!/usr/bin/env python3
        \"\"\"Manual APDU debug script for a Java-App applet running in oneos-sim QEMU.\"\"\"

        from __future__ import annotations

        import os
        import sys
        from datetime import datetime
        from pathlib import Path


        APP_NAME = {app_name!r}
        APPLET_AID = {applet_aid!r}
        SELECT_SW = {select_sw!r}
        DEFAULT_PERSIST_REL = {default_persist_relative!r}
        CUSTOM_PERSIST = {custom_persist!r}

        # Edit this list while debugging. Each entry can be a dict or a string.
        # Dict form: {{"name": "case name", "apdu": "00A40400...", "sw": "9000"}}
        # String form: "00A4040000=9000" or "00A4040000 9000"; omit sw to just print the result.
        APDUS = __APDUS__


        def find_oneos_root(start: Path) -> Path:
            current = start.resolve()
            if current.is_file():
                current = current.parent
            for candidate in (current, *current.parents):
                if (candidate / "Script" / "qemu" / "CustomerLib.py").is_file():
                    return candidate
                sibling = candidate / "oneos-sim"
                if (sibling / "Script" / "qemu" / "CustomerLib.py").is_file():
                    return sibling.resolve()
            raise FileNotFoundError("Cannot find oneos-sim; place this script under git/oneos-sim or git/Java-App.")


        def normalize_hex(value: str, *, field: str) -> str:
            text = "".join(ch for ch in str(value).upper() if ch in "0123456789ABCDEF")
            if not text or len(text) % 2 != 0:
                raise ValueError(f"{{field}} must be non-empty even-length hex: {{value!r}}")
            return text


        def normalize_sw(value: str, *, field: str) -> str:
            parts = []
            for part in str(value).split("|"):
                sw = normalize_hex(part, field=field)
                if len(sw) != 4:
                    raise ValueError(f"{{field}} must be 2-byte SW values: {{value!r}}")
                parts.append(sw)
            return "|".join(parts)


        def sw_matches(actual: str, expected: str | None) -> bool:
            return expected is None or actual.upper() in expected.upper().split("|")


        def select_apdu(aid: str) -> str:
            aid = normalize_hex(aid, field="Applet AID")
            return f"00A40400{{len(aid) // 2:02X}}{{aid}}"


        def resolve_persist_path(oneos_root: Path) -> Path:
            if os.environ.get("ONEOS_QEMU_PERSIST_PATH"):
                return Path(os.environ["ONEOS_QEMU_PERSIST_PATH"]).expanduser().resolve()
            if CUSTOM_PERSIST:
                return Path(CUSTOM_PERSIST).expanduser().resolve()
            if DEFAULT_PERSIST_REL:
                return (oneos_root / DEFAULT_PERSIST_REL).resolve()
            raise RuntimeError("No QEMU persist path configured.")


        def log_paths(oneos_root: Path) -> tuple[Path, Path]:
            log_dir = oneos_root / "build" / "cc2560a_qemu" / "java_app_skill" / APP_NAME / "manual_debug_logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            return log_dir / f"manual_apdu_debug_{{timestamp}}.log", log_dir / f"manual_apdu_debug_{{timestamp}}_shared.log"


        def parse_case(item, index: int) -> tuple[str, str, str | None]:
            if isinstance(item, str):
                raw = item.strip()
                name = f"apdu_{{index}}"
                if "=" in raw:
                    apdu, sw = raw.split("=", 1)
                else:
                    parts = raw.split()
                    if len(parts) == 1:
                        apdu, sw = parts[0], None
                    elif len(parts) == 2:
                        apdu, sw = parts
                    else:
                        raise ValueError(f"Unsupported APDU string at index {{index}}: {{item!r}}")
            elif isinstance(item, dict):
                name = str(item.get("name") or f"apdu_{{index}}")
                apdu = item["apdu"]
                sw = item.get("sw")
            else:
                raise TypeError(f"APDU entry {{index}} must be dict or string, got {{type(item).__name__}}")
            return name, normalize_hex(apdu, field=f"APDU {{index}}"), normalize_sw(sw, field=f"SW {{index}}") if sw else None


        def main() -> int:
            oneos_root = find_oneos_root(Path(__file__))
            qemu_dir = oneos_root / "Script" / "qemu"
            sys.path.insert(0, str(qemu_dir))

            persist_path = resolve_persist_path(oneos_root)
            if not persist_path.exists():
                raise FileNotFoundError(
                    f"QEMU state not found: {{persist_path}}\\n"
                    "Run java_app_qemu.py download-test <app> first, or set ONEOS_QEMU_PERSIST_PATH."
                )

            customer_log, shared_log = log_paths(oneos_root)
            os.environ["ONEOS_QEMU_PERSIST_PATH"] = str(persist_path)
            os.environ["ONEOS_QEMU_KEEP_STATE"] = "1"
            os.environ["ONEOS_QEMU_SHARED_LOG_PATH"] = str(shared_log)

            import CustomerLib as RC

            host = RC.SmartCard("qemu")
            failed = 0
            try:
                host.new_log("manual_apdu_debug", log_path=customer_log, banner=f"manual debug {{APP_NAME}}")
                host.cold_reset()

                select_result = host.send(select_apdu(APPLET_AID), sw=None)
                print(f"SELECT {{APPLET_AID}} -> SW={{select_result['sw']}} DATA={{select_result.get('data', '')}}")
                if not sw_matches(select_result["sw"], normalize_sw(SELECT_SW, field="SELECT SW") if SELECT_SW else None):
                    print(f"SELECT expected {{SELECT_SW}}, got {{select_result['sw']}}")
                    failed += 1

                for index, item in enumerate(APDUS, start=1):
                    name, apdu, expected_sw = parse_case(item, index)
                    result = host.send(apdu, sw=None)
                    actual_sw = result["sw"].upper()
                    data = result.get("data", "")
                    ok = sw_matches(actual_sw, expected_sw)
                    status = "PASS" if ok else "FAIL"
                    print(f"{{status}} {{name}} SEND={{apdu}} SW={{actual_sw}} DATA={{data}} EXPECT={{expected_sw or '<none>'}}")
                    if not ok:
                        failed += 1

                print(f"customer_log={{customer_log}}")
                print(f"shared_log={{shared_log}}")
                return 0 if failed == 0 else 1
            finally:
                host.end_log()


        if __name__ == "__main__":
            raise SystemExit(main())
        """
    )
    return template.replace("APDUS = __APDUS__", f"APDUS = {apdus_text}")


def _generate_debug_script(args: argparse.Namespace) -> int:
    oneos_root = _find_oneos_root(Path.cwd(), args.oneos_root)
    java_app_root = _find_java_app_root(Path.cwd(), oneos_root, args.java_app_root)
    app_dir = _resolve_app_dir(args.app, java_app_root)
    opt = _read_applet_opt(app_dir)
    workspace = Path(args.workspace).expanduser().resolve() if args.workspace else _default_workspace(oneos_root, app_dir)
    output = Path(args.output).expanduser().resolve() if args.output else workspace / "manual_apdu_debug.py"
    state_path = Path(args.persist_path).expanduser().resolve() if args.persist_path else workspace / "qemu_state.bin"
    try:
        default_persist_relative = str(state_path.relative_to(oneos_root))
        custom_persist = None
    except ValueError:
        default_persist_relative = None
        custom_persist = str(state_path)
    cases = _apdu_cases(args)

    script_text = _manual_debug_script_text(
        app_name=app_dir.name,
        applet_aid=opt.applet_aid,
        select_sw=args.select_sw,
        default_persist_relative=default_persist_relative,
        custom_persist=custom_persist,
        apdus=cases,
    )

    if args.stdout:
        print(script_text, end="")
        return 0

    _print(f"oneos-sim root: {oneos_root}")
    _print(f"Java-App root: {java_app_root}")
    _print(f"app: {app_dir}")
    _print(f"applet AID: {opt.applet_aid}")
    _print(f"QEMU state: {state_path}")
    _print(f"debug script: {output}")
    if args.dry_run:
        _print("dry-run: no script written")
        return 0

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(script_text, encoding="utf-8")
    output.chmod(output.stat().st_mode | 0o111)
    _print("edit APDUS in the generated script, then run it with python3")
    return 0


def _install_and_test(args: argparse.Namespace) -> int:
    oneos_root = _find_oneos_root(Path.cwd(), args.oneos_root)
    java_app_root = _find_java_app_root(Path.cwd(), oneos_root, args.java_app_root)
    app_dir = _resolve_app_dir(args.app, java_app_root)
    opt = _read_applet_opt(app_dir)
    ef_path = Path(args.ef).expanduser().resolve() if args.ef else _latest_ef(app_dir)
    module_aid = _validate_aid(args.module_aid, field="module AID") if args.module_aid else opt.default_module_aid
    state_path, run_dir = _prepare_qemu_layout(args, oneos_root, app_dir)
    shared_log = run_dir / "customerlib_shared.log"
    customer_log = run_dir / "customerlib.log"
    summary_json = run_dir / "summary.json"
    summary_txt = run_dir / "summary.txt"
    cases = _apdu_cases(args)

    _print(f"oneos-sim root: {oneos_root}")
    _print(f"Java-App root: {java_app_root}")
    _print(f"app: {app_dir}")
    _print(f"EF/XEF: {ef_path}")
    _print(f"package AID: {opt.package_aid}")
    _print(f"module AID: {module_aid}")
    _print(f"applet AID: {opt.applet_aid}")
    _print(f"run dir: {run_dir}")
    _print(f"state: {state_path}")

    if args.dry_run:
        if args.skip_init:
            _print("dry-run: would reuse an already initialized QEMU state because --skip-init was set")
        else:
            _print("dry-run: would run Script/qemu/qemu_init.py before app download")
        _print("dry-run: no QEMU commands executed")
        return 0

    if not ef_path.is_file():
        raise FileNotFoundError(f"EF/XEF file not found: {ef_path}")

    init_info = _ensure_qemu_state(args, oneos_root, state_path, run_dir)

    os.environ["ONEOS_QEMU_PERSIST_PATH"] = str(state_path)
    os.environ["ONEOS_QEMU_KEEP_STATE"] = "1"
    os.environ["ONEOS_QEMU_SHARED_LOG_PATH"] = str(shared_log)
    os.environ.setdefault("ONEOS_QEMU_STRICT_SW", "1")

    sys.path.insert(0, str(oneos_root / "Script" / "qemu"))
    import CustomerLib as RC  # pylint: disable=import-error,import-outside-toplevel

    host = RC.SmartCard("qemu")
    results: list[ApduResult] = []
    try:
        host.new_log("java_app_qemu", log_path=customer_log, banner=f"install/test {app_dir.name}")
        host.cold_reset()

        isd_aid = _validate_aid(args.isd_aid, field="ISD AID")
        host.show_info("select ISD")
        host.send(f"00A40400{len(isd_aid) // 2:02X}{isd_aid}", sw="9000")

        host.show_info("SCP02 authenticate")
        host.gp.set_expect_info("9000")
        host.gp.SCP_AUTH(
            scp="02",
            secure_level=args.scp_secure_level,
            key_version=args.scp_key_version,
            keylist={
                "ENC": args.scp_enc_key,
                "MAC": args.scp_mac_key,
                "DEK": args.scp_dek_key,
            },
        )

        host.show_info("delete old applet/package if present")
        host.gp.set_expect_info("9000|6A88")
        host.gp.deleteApplication(aids=opt.applet_aid, P2="00")
        host.gp.deleteApplication(aids=opt.package_aid, P2="80")

        host.show_info("load EF/XEF package")
        host.gp.set_expect_info("9000")
        host.gp.load_cap(file=str(ef_path), package_aid=opt.package_aid)

        host.show_info("install and make selectable")
        host.gp.installForInstallAndMakeSelectable(
            packageAID=opt.package_aid,
            moduleAID=module_aid,
            appletAID=opt.applet_aid,
            privileges=args.privileges,
            installParameters=args.install_parameters,
            installToken=args.install_token,
        )

        host.show_info("select applet")
        host.send(f"00A40400{len(opt.applet_aid) // 2:02X}{opt.applet_aid}", sw=args.select_sw)

        for case in cases:
            host.show_info(f"APDU {case.apdu} expect {case.expected_sw}")
            try:
                response = host.send(case.apdu, sw=None)
                actual_sw = response["sw"].upper()
                passed = _check_sw(actual_sw, case.expected_sw)
                results.append(
                    ApduResult(
                        apdu=case.apdu,
                        expected_sw=case.expected_sw,
                        actual_sw=actual_sw,
                        data=response.get("data", ""),
                        passed=passed,
                    )
                )
            except Exception as exc:  # keep summary output for APDU-level failures
                results.append(
                    ApduResult(
                        apdu=case.apdu,
                        expected_sw=case.expected_sw,
                        actual_sw="<error>",
                        data="",
                        passed=False,
                        error=str(exc),
                    )
                )
    finally:
        host.end_log()

    passed = sum(1 for result in results if result.passed)
    failed = len(results) - passed
    summary = {
        "app_dir": str(app_dir),
        "ef_path": str(ef_path),
        "package_aid": opt.package_aid,
        "module_aid": module_aid,
        "applet_aid": opt.applet_aid,
        "state_path": str(state_path),
        "run_dir": str(run_dir),
        "init_ran": init_info["init_ran"] == "true",
        "init_output_log": init_info["init_output_log"],
        "init_shared_log": init_info["init_shared_log"],
        "customer_log": str(customer_log),
        "shared_log": str(shared_log),
        "total": len(results),
        "passed": passed,
        "failed": failed,
        "apdu_results": [result.__dict__ for result in results],
    }
    summary_json.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    lines = [
        f"app: {app_dir}",
        f"ef: {ef_path}",
        f"package_aid: {opt.package_aid}",
        f"module_aid: {module_aid}",
        f"applet_aid: {opt.applet_aid}",
        f"state: {state_path}",
        f"init_ran: {init_info['init_ran']}",
        f"init_output_log: {init_info['init_output_log']}",
        f"init_shared_log: {init_info['init_shared_log']}",
        f"customer_log: {customer_log}",
        f"shared_log: {shared_log}",
        f"APDU total={len(results)} passed={passed} failed={failed}",
    ]
    for result in results:
        status = "PASS" if result.passed else "FAIL"
        suffix = f" error={result.error}" if result.error else ""
        lines.append(
            f"{status} {result.apdu} expected={result.expected_sw} actual={result.actual_sw} data={result.data}{suffix}"
        )
    summary_txt.write_text("\n".join(lines) + "\n", encoding="utf-8")

    _print(f"summary: {summary_txt}")
    _print(f"summary json: {summary_json}")
    _print(f"APDU total={len(results)} passed={passed} failed={failed}")
    for result in results:
        status = "PASS" if result.passed else "FAIL"
        _print(f"{status} {result.apdu} expected={result.expected_sw} actual={result.actual_sw}")

    return 0 if failed == 0 else 1


def _add_root_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--oneos-root", help="oneos-sim repo root; default resolves from cwd/sibling layout")
    parser.add_argument("--java-app-root", help="Java-App repo root; default is sibling ../Java-App")


def _add_build_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--ant", default="ant", help="Ant executable name or path")
    parser.add_argument("--xcsdk", help="pass -Dxcsdk=PATH to Ant")
    parser.add_argument("--xctool-path", help="pass -Dxctool_path=PATH to Ant")
    parser.add_argument("--no-clean", action="store_true", help="skip ant clean")


def _add_install_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--python", default=sys.executable or "python3", help="Python executable for qemu_init.py")
    parser.add_argument("--ef", help="specific .ef/.xef file to install")
    parser.add_argument("--module-aid", help="module AID; default is applet AID from applet.opt")
    parser.add_argument("--workspace", help="base workspace for isolated QEMU state/logs")
    parser.add_argument("--run-dir", help="specific log/summary directory")
    parser.add_argument("--persist-path", help="specific QEMU RAM image path")
    parser.add_argument("--reinit-state", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--skip-init", action="store_true", help="reuse an already initialized QEMU state")
    parser.add_argument("--no-init", dest="skip_init", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--isd-aid", default=DEFAULT_ISD_AID, help="ISD AID used for GP load/install")
    parser.add_argument("--scp-secure-level", default="01", help="SCP02 secure level")
    parser.add_argument("--scp-key-version", default="00", help="SCP02 key version")
    parser.add_argument("--scp-enc-key", default=DEFAULT_SCP02_KEY, help="SCP02 ENC key")
    parser.add_argument("--scp-mac-key", default=DEFAULT_SCP02_KEY, help="SCP02 MAC key")
    parser.add_argument("--scp-dek-key", default=DEFAULT_SCP02_KEY, help="SCP02 DEK key")
    parser.add_argument("--privileges", default="00", help="INSTALL privileges hex")
    parser.add_argument("--install-parameters", default="", help="INSTALL parameters hex")
    parser.add_argument("--install-token", default="", help="INSTALL token hex")
    parser.add_argument("--select-sw", default="9000", help="expected SW after SELECT applet")
    parser.add_argument("--apdu", action="append", default=[], help="APDU expectation, e.g. 001000000100=9900")
    parser.add_argument("--apdu-file", action="append", default=[], help="file with APDU=SW or APDU SW lines")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", aliases=["new"], help="create a Java-App app from simple_app")
    _add_root_args(create)
    create.add_argument("app_name")
    create.add_argument("--template", default="simple_app", help="template directory under Java-App")
    create.add_argument("--package", help="Java package name")
    create.add_argument("--class-name", help="Java applet class name")
    create.add_argument("--package-aid", help="package AID; default is deterministic from app name")
    create.add_argument("--applet-aid", help="applet AID; default is package AID plus 01")
    create.add_argument("--dry-run", action="store_true")
    create.set_defaults(func=_create_app)

    build = subparsers.add_parser("build", help="run ant clean and ant for an app")
    _add_root_args(build)
    _add_build_args(build)
    build.add_argument("app")
    build.add_argument("--dry-run", action="store_true")
    build.set_defaults(func=lambda args: 0 if _build_app(args) else 1)

    install = subparsers.add_parser(
        "download-test",
        aliases=["install-test"],
        help="install built EF/XEF into QEMU and run APDU tests",
    )
    _add_root_args(install)
    _add_install_args(install)
    install.add_argument("app")
    install.add_argument("--dry-run", action="store_true")
    install.set_defaults(func=_install_and_test)

    debug = subparsers.add_parser("debug-script", aliases=["manual-debug"], help="generate editable APDU debug Python script")
    _add_root_args(debug)
    debug.add_argument("app")
    debug.add_argument("--workspace", help="base workspace for default QEMU state and script output")
    debug.add_argument("--persist-path", help="QEMU RAM image path to use in the generated script")
    debug.add_argument("--output", help="generated script path; default is app workspace/manual_apdu_debug.py")
    debug.add_argument("--stdout", action="store_true", help="print script to stdout instead of writing a file")
    debug.add_argument("--select-sw", default="9000", help="expected SW after SELECT applet")
    debug.add_argument("--apdu", action="append", default=[], help="initial APDU entry, e.g. 001000000100=9900")
    debug.add_argument("--apdu-file", action="append", default=[], help="file with initial APDU=SW or APDU SW lines")
    debug.add_argument("--dry-run", action="store_true")
    debug.set_defaults(func=_generate_debug_script)

    all_cmd = subparsers.add_parser("all", help="build, install into QEMU, and run APDU tests")
    _add_root_args(all_cmd)
    _add_build_args(all_cmd)
    _add_install_args(all_cmd)
    all_cmd.add_argument("app")
    all_cmd.add_argument("--dry-run", action="store_true")

    def _run_all(args: argparse.Namespace) -> int:
        _build_app(args)
        return _install_and_test(args)

    all_cmd.set_defaults(func=_run_all)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:
        _print(f"ERROR: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

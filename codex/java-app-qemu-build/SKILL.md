---
name: java-app-qemu-build
description: Use when the user asks to create a new Java-App applet from git/Java-App/simple_app, modify or compile a Java-App application, download/install a compiled .ef/.xef app into oneos-sim QEMU, run APDU interaction smoke tests, or generate a manual editable APDU debug Python script for a Java applet. Resolve portable Ubuntu 22 sibling paths under git/oneos-sim and git/Java-App instead of hardcoding /home paths.
---

# Java App QEMU Build

## Repository Layout

Use the portable sibling layout:

```text
git/
├── oneos-sim/
└── Java-App/
    └── simple_app/
```

Do not hardcode `/home/<user>/...`. Resolve from the current `oneos-sim` root, from a current path under `Java-App`, or pass explicit roots to the helper.

## Helper

Use the bundled helper by default:

```bash
"${CODEX_HOME:-$HOME/.codex}/skills/java-app-qemu-build/scripts/java_app_qemu.py" <subcommand>
```

Run `--dry-run` first when checking path resolution, generated AIDs, Ant commands, or QEMU state/log locations.

## Create A New App

When the user asks to create a new Java app, copy `Java-App/simple_app` into `Java-App/<app-name>` and keep `src/`, Java package/class, and `applet.opt` consistent. Prefer the helper:

```bash
"${CODEX_HOME:-$HOME/.codex}/skills/java-app-qemu-build/scripts/java_app_qemu.py" create my_app
```

The helper:

- copies `simple_app`
- renames the Java package and applet class
- rewrites `applet.opt`
- generates a deterministic package AID from the app name and uses `applet AID = package AID + 01`

Use `--package`, `--class-name`, `--package-aid`, or `--applet-aid` only when the user requests specific names/AIDs.

## Build

Build from the app directory with Ant. Default to a clean build:

```bash
"${CODEX_HOME:-$HOME/.codex}/skills/java-app-qemu-build/scripts/java_app_qemu.py" build my_app
```

This runs:

```bash
ant clean
ant
```

`build_common.xml` requires `QZVM_SDK_HOME` or Ant `-Dxcsdk=...`; `XCTOOLS_EXE` or `-Dxctool_path=...` may override the converter. If Ant fails because these are missing, report the exact missing variable or tool path.

Useful options:

- `--xcsdk PATH`: pass `-Dxcsdk=PATH`
- `--xctool-path PATH`: pass `-Dxctool_path=PATH`
- `--no-clean`: skip `ant clean` only when the user asks to preserve build output

## Download To QEMU And Test

When the user asks to download/install the compiled app into QEMU, use:

```bash
"${CODEX_HOME:-$HOME/.codex}/skills/java-app-qemu-build/scripts/java_app_qemu.py" download-test my_app
```

The helper parses `applet.opt`, finds the latest `build/*.ef` or `build/*.xef`, runs `Script/qemu/qemu_init.py` to initialize the card state, then uses `Script/qemu/CustomerLib.py` to:

1. initialize the isolated QEMU RAM image with `Script/qemu/qemu_init.py`
2. select ISD `D1560001010001600000000100000000`
3. perform SCP02 auth with default development keys
4. delete any old applet/package for the same AIDs
5. load the EF/XEF package
6. install for install and make selectable
7. select the applet and run APDU tests

By default QEMU state is isolated under:

```text
oneos-sim/build/cc2560a_qemu/java_app_skill/<app-name>/qemu_state.bin
```

Every normal `download-test` or `all` run reinitializes this isolated state before installing the app, because app download tests require a freshly initialized card. Logs and summaries are written under the same app workspace in `runs/<timestamp>/`. The command prints a summary with the EF path, AIDs, QEMU state path, `qemu_init.py` logs, CustomerLib log, shared serial log, and APDU pass/fail results.

Default APDUs match `simple_app` behavior:

```text
001000000100 => 9900
001000000101 => 9901
```

For custom app behavior, pass repeated `--apdu APDU=SW` arguments or `--apdu-file FILE`. The file format is one APDU expectation per line, using `APDU=SW` or `APDU SW`; `#` starts a comment.

Useful options:

- `--ef PATH`: install a specific compiled EF/XEF file
- `--module-aid AID`: override module AID; default is the applet AID from `applet.opt`
- `--persist-path PATH`: use a specific QEMU RAM image
- `--skip-init`: reuse an already initialized QEMU RAM image only when the user explicitly requests reuse/debugging
- `--scp-key-version 00|20`: default is `00`, matching several local init scripts; use `20` if the selected QEMU image expects it

## Manual APDU Debug

When the user says they want to manually send APDUs, manually debug the app, or edit APDU instructions themselves, generate an editable Python script:

```bash
"${CODEX_HOME:-$HOME/.codex}/skills/java-app-qemu-build/scripts/java_app_qemu.py" debug-script my_app
```

The helper writes by default:

```text
oneos-sim/build/cc2560a_qemu/java_app_skill/<app-name>/manual_apdu_debug.py
```

Tell the user to edit the `APDUS = [...]` list in that generated script, then run it with `python3`. The script automatically:

- resolves the local `oneos-sim` checkout portably
- reuses the app's isolated `qemu_state.bin`
- imports `Script/qemu/CustomerLib.py`
- cold-resets QEMU, selects the applet AID from `applet.opt`, sends each APDU, and prints SW/data

Manual debug scripts do not run `qemu_init.py`, because initialization would remove the already downloaded app from the RAM image. If the state file is missing or the applet is not installed, run `download-test` first. Use `--persist-path PATH` when the app was installed into a custom QEMU RAM image, and `--output PATH` when the user asks for a specific script location.

## End-To-End

For a normal edit/build/install/test loop:

```bash
"${CODEX_HOME:-$HOME/.codex}/skills/java-app-qemu-build/scripts/java_app_qemu.py" all my_app
```

For manual source edits, preserve unrelated user changes. After editing Java source, run `build` or `all` and report:

- app directory
- changed Java/config files
- generated EF/XEF path
- QEMU state/log paths
- APDU summary and first failing APDU if any

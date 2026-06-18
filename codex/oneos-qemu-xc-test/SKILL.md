---
name: oneos-qemu-xc-test
description: Use when the user asks to run oneos-sim QEMU XC script tests, qemu一致性测试, 使用qemu进行一致性测试, qemu跑RFM/RAM/API/core/cryptography/vm/re模块, or requests sequential or parallel execution of simmaster_auto_lite test cases through Script/qemu/run_xc_script.py. Builds portable commands by resolving the sibling simmaster_auto_lite/testcase tree instead of hardcoding /home paths.
---

# OneOS QEMU XC Test

## Purpose

Run `oneos-sim` XC text-script suites in QEMU with portable testcase paths. The testcase repository is expected to be a sibling of this repo:

```text
git/
├── oneos-sim/
└── simmaster_auto_lite/testcase/
```

Never hardcode `/home/<user>/...` paths. Resolve paths from the current `oneos-sim` git root, or let the helper script do it.

## Default Runner

Use the bundled helper by default:

```bash
"${CODEX_HOME:-$HOME/.codex}/skills/oneos-qemu-xc-test/scripts/run_qemu_xc_suite.py" <target>
```

The helper runs from the `oneos-sim` repo root and ultimately invokes:

```bash
python3 Script/qemu/run_xc_script.py -i <resolved-testcase-path>
```

Use `--dry-run` first when checking target resolution or when the user asks what would run.

## Target Selection

Map the user's requested module to a testcase path:

- "一致性测试" or "使用 qemu 进行一致性测试": target `一致性`.
- "一致性 core" or "api/core": target `api/core`, which resolves to `一致性/api/core`.
- "一致性 base", "cryptography", "ema", "re", or "vm": resolve under `一致性/`.
- "RAM": target `RAM`, which resolves to `RAM/RAM` when that directory exists.
- "RFM": target `RFM`, which resolves to `RFM/RFM` when that directory exists.
- "RFM SIM" or "RFM USIM": target `RFM/SIM` or `RFM/USIM`.
- If the user gives an exact testcase relative path such as `一致性/api/core/userpin`, pass it as the target.

If a module name is ambiguous, run the helper with `--dry-run` or inspect `../simmaster_auto_lite/testcase` before choosing.

## Sequential vs Parallel

Default to sequential execution unless the user explicitly asks for concurrency, acceleration, parallelism, or says "并发进行一致性测试".

Sequential examples:

```bash
"${CODEX_HOME:-$HOME/.codex}/skills/oneos-qemu-xc-test/scripts/run_qemu_xc_suite.py" 一致性
"${CODEX_HOME:-$HOME/.codex}/skills/oneos-qemu-xc-test/scripts/run_qemu_xc_suite.py" api/core
"${CODEX_HOME:-$HOME/.codex}/skills/oneos-qemu-xc-test/scripts/run_qemu_xc_suite.py" RFM
```

Parallel example for large consistency suites:

```bash
"${CODEX_HOME:-$HOME/.codex}/skills/oneos-qemu-xc-test/scripts/run_qemu_xc_suite.py" api/core --parallel --jobs 4
```

Parallel mode splits the resolved target by immediate subdirectories and starts one `run_xc_script.py` process per shard, limited by `--jobs`. Each parallel invocation gets a unique run directory under `build/cc2560a_qemu/skill_parallel_runs/`. Inside that run directory, every shard gets its own `state/*.bin`, `output/*.log`, and `shared_serial/*.txt`, so neither QEMU RAM backend `.bin` files nor helper logs collide across shards or across two helper invocations running at the same time.

After all shards finish, the helper prints a combined parallel summary and writes it to `summary.txt` plus machine-readable `summary.json` in the run directory. The summary includes shard pass/fail counts, script pass/fail totals parsed from each `[xc] Summary`, each shard's output/shared-log paths, and the first matched failure line for failed shards.

For baseline state, the helper copies `build/cc2560a_qemu/customerlib_ram.bin` when it exists. If it does not exist, parallel mode automatically creates a run-local baseline with `Script/qemu/qemu_init.py`; RAM/RFM targets use `Script/qemu/qemu_ram_rfm_init.py`. Then it copies that baseline to each shard's separate `state/*.bin`. Use `--fresh-state` only for scripts that are expected to pass from an empty card image.

For a small parallel smoke test, filter shards with `--only`:

```bash
"${CODEX_HOME:-$HOME/.codex}/skills/oneos-qemu-xc-test/scripts/run_qemu_xc_suite.py" api/core --parallel --jobs 3 --only customexception,moduleexception,seexception
```

Use `--keep-going` only when the user wants to continue after failures and collect more failures. Without it, `run_xc_script.py` stops at the first failed script in each sequential run or shard.

## Useful Options

- `--dry-run`: print resolved paths and child commands without running QEMU.
- `--parallel`: split immediate child directories and run shards concurrently.
- `--jobs N`: limit parallel workers; default is conservative.
- `--only NAME[,NAME...]`: in parallel mode, run only selected shard names.
- `--run-id ID`: choose the parallel run directory id; normally let the helper generate it.
- `--fresh-state`: start from fresh QEMU state instead of copying/reusing the default persist image.
- `--no-init-baseline`: skip automatic baseline creation when the default baseline is missing.
- `--reinit-baseline`: always create a run-local baseline instead of reusing `customerlib_ram.bin`.
- `--keep-going`: pass through to `run_xc_script.py`.
- `--testcase-root PATH`: override the sibling `simmaster_auto_lite/testcase` location for unusual layouts.
- `--repo-root PATH`: override the `oneos-sim` root.

## Success Check

Treat exit code `0` as pass. On parallel failures, start with the run directory's `summary.txt` or `summary.json`, then inspect the per-shard `output/*.log` and `shared_serial/*.txt` paths listed there. Report the failing target/shard and the first relevant `[xc] FAIL`, exception, assert, fault, or nonzero summary line.

---
name: cos-partition-switch-verify
description: Use when the user asks to verify COS partition switching, COS A/B upgrade, COSA->COSB switching, A->B->C two-step COS upgrade, or says "验证分区切换"/"一次升级和二次升级测试". Runs the portable oneos-sim clean QEMU build, cos_partition_switch.py workflow, and optional post-upgrade business suites.
---

# COS Partition Switch Verification

## Trigger

Use this skill when the user asks to:

- 验证分区切换
- 验证 COS/COSA/COSB 升级
- 跑 COSA -> COSB upgrade/switch test
- 重新清理编译后验证 `cos_partition_switch.py`
- 进行一次升级和二次升级测试
- 验证 COSA -> COSB -> COSC 升级后的业务用例

## Mode Selection

The skill has two modes. Choose the mode from the user's wording before running the script.

- Simple A -> B mode, `--mode ab`, is the default. Use it when the user asks for a simple partition switch verification, simple COSA -> COSB upgrade, or "简单的切分区验证".
- Full A -> B -> C mode, `--mode abc`, is explicit. Use it when the user asks for "一次升级和二次升级测试", "两次升级验证", COSA -> COSB -> COSC, or wants the first and second post-upgrade business suites run.

Do not run full mode when the user only asks for a simple switch verification; it is slower and runs business suites.

## Default Layout

- Run from inside the COSA `oneos-sim` repo, or set `COSA_ROOT`.
- COSA repo/worktree: inferred from the current git root.
- COSA branch: `dev_4.0`
- COSB worktree: `COSB_ROOT`, else an existing `git worktree` whose branch is `dev_4.0_COSB`, else an existing sibling worktree such as `../oneos-sim-cosup-COSB`, else sibling `../dev_4.0_COSB`.
- COSB branch: `dev_4.0_COSB`
- COSC worktree, only for full mode: `COSC_ROOT`, else an existing `git worktree` whose branch is `dev_4.0_COSC`, else an existing sibling worktree such as `../oneos-sim-cosup-COSC`, else sibling `../dev_4.0_COSC`.
- COSC branch: `dev_4.0_COSC`
- Project dir: `COS/projects/cc2560a`
- Build preset: `PRESET` env var, else `cc2560a-qemu-local` if present, else `cc2560a-qemu`.
- COSA ELF: `build/cc2560a_qemu/bin/cc2560a_qemu.elf`
- COSB ELF: `$COSB_ROOT/build/cc2560a_qemu/bin/cc2560a_qemu.elf`
- COSC ELF, only for full mode: `$COSC_ROOT/build/cc2560a_qemu/bin/cc2560a_qemu.elf`
- Persist/log dir: `build/cosup`
- Business suite root, only for full mode: sibling `../simmaster_auto_lite/testcase/COS升级`
- First post-upgrade suite: `第一次测试测试`
- Second post-upgrade suite: `第二次升级测试`

This skill is portable across Ubuntu machines as long as the repository and test-case relative layout is preserved. Do not hardcode `/home/<user>` paths in commands. Machine-local compiler paths should live in `COS/projects/cc2560a/CMakeUserPresets.json`; the script copies that file from COSA to COSB/COSC when it exists. If no user preset exists, `cc2560a-qemu` is used and `riscv64-unknown-elf-gcc` must be available from `PATH`.

## Portability Contract

On a new Ubuntu machine, the script expects these stable facts and discovers everything else at runtime:

- The COSA repository is still this `oneos-sim` repository and contains `Script/qemu/cos_partition_switch.py`.
- Branch names stay `dev_4.0`, `dev_4.0_COSB`, and `dev_4.0_COSC`.
- COSB/COSC are available either as `git worktree` entries for those branches or can be created from those branch names.
- The business testcase repo is a sibling of COSA by default: `../simmaster_auto_lite/testcase/COS升级`.
- If the testcase repo or worktrees live elsewhere, pass `COSB_ROOT`, `COSC_ROOT`, or `COSUP_CASE_ROOT`; do not edit the script.
- Machine-specific compiler/toolchain paths belong in `COS/projects/cc2560a/CMakeUserPresets.json`, not in the skill.
- Before running a real migration on a new machine, run `--dry-run` for the intended mode and check the resolved paths.

If accidentally run from the COSB/COSC worktree, the script tries to normalize back to the sibling `oneos-sim` COSA root. If the current repo is on `dev_4.0_COSB`/`dev_4.0_COSC` and no sibling COSA repo is found, it fails before cleaning or building. For unusual layouts, set `COSA_ROOT`, `COSB_ROOT`, and `COSC_ROOT` explicitly.

## Workflow

Run the bundled script from anywhere inside the COSA repo for simple A -> B verification:

```bash
"${CODEX_HOME:-$HOME/.codex}/skills/cos-partition-switch-verify/scripts/verify_cos_switch.sh"
```

or equivalently:

```bash
"${CODEX_HOME:-$HOME/.codex}/skills/cos-partition-switch-verify/scripts/verify_cos_switch.sh" --mode ab
```

Run full two-step upgrade and business-suite verification:

```bash
"${CODEX_HOME:-$HOME/.codex}/skills/cos-partition-switch-verify/scripts/verify_cos_switch.sh" --mode abc
```

For a migration sanity check that does not clean, build, copy presets, or create files:

```bash
"${CODEX_HOME:-$HOME/.codex}/skills/cos-partition-switch-verify/scripts/verify_cos_switch.sh" --mode abc --dry-run
```

In simple A -> B mode, the script will:

1. Infer `COSA_ROOT` from the current git root.
2. Infer `COSB_ROOT`, and create that worktree if missing.
3. Copy `COS/projects/cc2560a/CMakeUserPresets.json` from COSA to COSB if it exists.
4. Remove only generated test/build directories:
   - `build/cc2560a_qemu`
   - `build/cosup`
5. Select a configure/build preset pair: `PRESET`, `cc2560a-qemu-local`, then `cc2560a-qemu`.
6. Build COSA with the selected preset.
7. Build COSB the same way.
8. Run:

```bash
python3 Script/qemu/cos_partition_switch.py \
  --from A \
  --to B \
  --cosa-elf "$COSA_ROOT/build/cc2560a_qemu/bin/cc2560a_qemu.elf" \
  --cosb-elf "$COSB_ROOT/build/cc2560a_qemu/bin/cc2560a_qemu.elf" \
  --persist-path "$COSA_ROOT/build/cosup/cos_ab_test.bin" \
  --shared-log "$COSA_ROOT/build/cosup/cos_ab_test.log"
```

9. Scan `build/cosup/*.log` for:
   - `Exception`
   - `Assert failed`
   - `Load access fault`
   - `Store access fault`
   - `fault`
   - `ERROR`
   - `error`
   - `[xc] FAIL`
   - non-zero `failed=N`
   - `qzvm_main init error`

In full A -> B -> C mode, the script will:

1. Infer or create COSB and COSC worktrees.
2. Sync `CMakeUserPresets.json` from COSA to COSB/COSC when present.
3. Clean generated `build/cc2560a_qemu` directories for COSA/COSB/COSC and COSA `build/cosup`.
4. Build COSA, COSB, and COSC.
5. Run COSA -> COSB:

```bash
python3 Script/qemu/cos_partition_switch.py \
  --from A \
  --to B \
  --cosa-elf "$COSA_ROOT/build/cc2560a_qemu/bin/cc2560a_qemu.elf" \
  --cosb-elf "$COSB_ROOT/build/cc2560a_qemu/bin/cc2560a_qemu.elf" \
  --persist-path "$COSA_ROOT/build/cosup/cos_ab_test.bin" \
  --shared-log "$COSA_ROOT/build/cosup/cosa_to_cosb_test.log"
```

6. Run the first post-upgrade business suite using COSB ELF and the A -> B persistent state:

```bash
ONEOS_QEMU_ELF_PATH="$COSB_ROOT/build/cc2560a_qemu/bin/cc2560a_qemu.elf" \
ONEOS_QEMU_PERSIST_PATH="$COSA_ROOT/build/cosup/cos_ab_test.bin" \
ONEOS_QEMU_KEEP_STATE=1 \
ONEOS_QEMU_APDU_TIMEOUT=60 \
python3 Script/qemu/run_xc_script.py --keep-going \
  -i "$COSA_ROOT/../simmaster_auto_lite/testcase/COS升级/第一次测试测试"
```

7. Copy the A -> B persistent state to `build/cosup/cos_abc_test.bin`.
8. Run COSB -> COSC. The partition switch tool treats COSC as the A-slot ELF, so the command is `--from B --to A --skip-init`:

```bash
python3 Script/qemu/cos_partition_switch.py \
  --from B \
  --to A \
  --cosa-elf "$COSC_ROOT/build/cc2560a_qemu/bin/cc2560a_qemu.elf" \
  --cosb-elf "$COSB_ROOT/build/cc2560a_qemu/bin/cc2560a_qemu.elf" \
  --persist-path "$COSA_ROOT/build/cosup/cos_abc_test.bin" \
  --shared-log "$COSA_ROOT/build/cosup/cosb_to_cosc_test.log" \
  --skip-init
```

9. Run the second post-upgrade business suite using COSC ELF and the A -> B -> C persistent state:

```bash
ONEOS_QEMU_ELF_PATH="$COSC_ROOT/build/cc2560a_qemu/bin/cc2560a_qemu.elf" \
ONEOS_QEMU_PERSIST_PATH="$COSA_ROOT/build/cosup/cos_abc_test.bin" \
ONEOS_QEMU_KEEP_STATE=1 \
ONEOS_QEMU_APDU_TIMEOUT=60 \
python3 Script/qemu/run_xc_script.py --keep-going \
  -i "$COSA_ROOT/../simmaster_auto_lite/testcase/COS升级/第二次升级测试"
```

10. Scan all `build/cosup/*.log` files for exception/assert/fault/FAIL indicators.

Useful overrides:

```bash
COSA_ROOT=/path/to/oneos-sim \
COSB_ROOT=/path/to/dev_4.0_COSB \
COSC_ROOT=/path/to/dev_4.0_COSC \
PRESET=cc2560a-qemu-local \
"${CODEX_HOME:-$HOME/.codex}/skills/cos-partition-switch-verify/scripts/verify_cos_switch.sh" --mode abc
```

Use `ALLOW_BRANCH_MISMATCH=1` only when intentionally testing a non-default branch layout. The normal expectation is COSA on `dev_4.0`, COSB on `dev_4.0_COSB`, and COSC on `dev_4.0_COSC`.

## Expected Success Signals

For simple A -> B mode:

- COSA build succeeds.
- COSB build succeeds.
- `cos_partition_switch.py` exits with code 0.
- Stage 3 boots COSB and `SELECT ISD` returns `9000`.
- No exception/assert/fault keywords appear in `build/cosup/*.log`.

For full A -> B -> C mode:

- COSA/COSB/COSC builds succeed.
- A -> B partition switch exits with code 0.
- First business suite prints `failed=0`.
- B -> C partition switch exits with code 0.
- Second business suite prints `failed=0`.
- No exception/assert/fault/FAIL keywords appear in `build/cosup/*.log`.

## Known Failure: COSB ROMBASE Mismatch

If COSB build fails with:

```text
QZ_ROM_BASE is not equal with Rom_Base in maskfile
```

compare:

```bash
rg -n "#define ROMBASE|#define FLASHBASE|#define QZ_ROM_BASE|#define QZ_E2P_BASE" \
  COS/components/qzvm COS/platform/cc2560a COS/projects/cc2560a -g'*.h' -g'*.c'
```

The COSB `QZ_ROM_BASE` in `COS/components/qzvm/port/platform_cc2560a.h` must match `ROMBASE` from `COS/components/qzvm/core/arch/qzvm/qzvm_mask.c`.

When making a local fix to continue verification, keep it minimal, report the exact diff, and do not hide the fact that the worktree is dirty.

## Reporting

In the final response, include:

- COSA/COSB branch and short commit; include COSC too for full mode.
- Whether clean builds passed.
- Whether switch verification passed; include business suite summaries for full mode.
- Persist/log paths.
- Any local modifications made to make the test runnable.
- Key failure message if any stage failed.

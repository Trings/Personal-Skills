---
name: cosup-chain-build
description: Use when the user asks to automate, verify, debug, or repeat the COS upgrade chain build for COSA to COSB to COSC, including slpvmfw mask builds, symbols.txt handoff, qzvm_static_symbol.c generation, COS fw/COS branch alignment, or making the COSUP-MASKC manual symbol workflow portable on Ubuntu 22.
---

# COSUP Chain Build

## Purpose

Automate and verify the manual COS upgrade chain:

```text
COSA fw -> COSA COS -> COSB fw with COSA symbols -> COSB COS -> COSC fw with COSB symbols -> COSC COS
```

Use the deterministic scripts bundled with this skill. Do not recreate ad hoc chain-build or symbol-generation scripts inside the target repository.

## Portable Layout

Assume the Ubuntu 22 workspace keeps related repos under one `git/` directory:

```text
git/
├── oneos-sim/
├── slpvmfw/
└── slpvmsdk/        # optional but expected for full fw mask builds
```

Resolve paths relative to the current `oneos-sim` repo. Do not hardcode `/home/<user>` except when reporting discovered machine-local tool paths.

The fw build uses the caller's `QZVM_HOME` and `QZVM_SDK_HOME` by default. In the usual setup, `QZVM_HOME` points at the main `oneos-sim/COS/components`, so Ant installs generated mask files back into the main checkout rather than into each COS worktree. The script backs up that install location, collects the generated files, restores the original main checkout files, and then copies the collected files into the COS worktree for the current stage.

By default, do not fetch `origin`, do not pull, and do not update local branches. The bundled script creates detached build worktrees from the existing local branch tips for the required oneos-sim and slpvmfw branches, matching a manual local checkout-oriented workflow. If the user explicitly wants remote updates first, pass `--git-pull`; then the script fetches `origin`, fast-forwards required local branches when safe, and creates detached build worktrees from `origin/<branch>`.

For fw Ant builds, sanitize the Codex execution environment before invoking Ant. The bundled chain script inherits the user's real toolchain variables (`QZVM_HOME`, `QZVM_SDK_HOME`, `ANT_HOME`, `PATH`, `XCTOOLS_EXE`, and related settings), but removes `DEBUG` from the Ant child environment by default. `slpvmfw/build_common.xml` treats any exported `DEBUG` value as `qzvm.debug=true`, so a parent-process value such as `DEBUG=release` still passes `-debug` to QZVMDK. Preserve it only with `--preserve-fw-debug-env` when a debug mask build is intentional.

The COS build needs a RISC-V GCC toolchain. Let the bundled `scripts/cosup_chain_build.py` discover it from `RISCV_TOOLCHAIN_BIN`, `PATH`, or `~/toolchains/gcc/bin`; otherwise pass `--riscv-toolchain-bin`.

Before each fw Ant build, the bundled script verifies the SDK Linux executables under `$QZVM_SDK_HOME/bin` (`javac`, `qzvmdk_linux`, and `qzsim_linux`). If an executable bit is missing, set it with `chmod +x` and print the action. Treat missing files as SDK installation blockers.

## Branch Mapping

Use this exact stage mapping:

| Stage | slpvmfw fw branch | oneos-sim COS branch | fw `symbols.txt` input |
| --- | --- | --- | --- |
| COSA | `cosup_COSA_V1` | `dev_4.0_COSA` | none |
| COSB | `cosup_COSB_V1` | `dev_4.0_COSB` | COSA `symbols.txt` |
| COSC | `cosup_COSC_V1` | `dev_4.0_COSC` | COSB `symbols.txt` |

The script creates detached worktrees under `oneos-sim/build/cosup_chain/worktrees/` so the user's current `oneos-sim` and `slpvmfw` branches are not switched.

## Hard Rules

- Do not manually edit `slpvmfw/build/mask/qzvm_mask.c` or `$QZVM_HOME/qzvm/core/arch/qzvm/qzvm_mask.c`; they are generated/installed by Ant/QZVMDK.
- Preserve the COSUP static-symbol compatibility order. For COSB/COSC, use the previous stage's static symbol list as the append-only prefix.
- Exclude `com/cmcc/qzvm/impl/PackageMgr/pkgTable`; this matches the manual `build/COSUP-MASKC` notebook flow.
- Write the generated static table to `COS/components/qzvm/core/arch/qzvm/qzvm_static_symbol.c`, which provides `syms_static_table` used by `COS/components/qzvm/core/sys_tbl.c`.
- Preserve `qzvm_static_symbol.c` diff readability: when the file already exists, only update the `syms_static_table` initializer and the existing maintenance-rule comment that says the table is generated using append-only order for COS upgrade compatibility. Do not rewrite the full file header or reformat unchanged array lines.
- Preserve the user's main `QZVM_HOME` checkout. Back up `$QZVM_HOME/qzvm/core/arch/qzvm/{qzvm_mask.c,qzvm_java_native_methods.c,qzvm_java_native_methods.h,qzvm_opcode.h}` before each fw build and restore it after collecting stage artifacts.
- Normalize SDK Linux executable permissions before invoking Ant; this matches the manual chmod fix needed on Ubuntu/WSL checkouts.
- Treat SDK authorization failures as toolchain blockers, not script logic failures.

## Full Build

From `git/oneos-sim`, run the bundled script:

```bash
python3 ~/.codex/skills/cosup-chain-build/scripts/cosup_chain_build.py \
  --jobs 8 \
  --strict-symbols
```

This uses the active shell environment, for example `QZVM_HOME=/.../git/oneos-sim/COS/components` and `QZVM_SDK_HOME=/.../git/slpvmsdk/r1.8.4`.

Useful options:

```bash
python3 ~/.codex/skills/cosup-chain-build/scripts/cosup_chain_build.py --stages COSA
python3 ~/.codex/skills/cosup-chain-build/scripts/cosup_chain_build.py --skip-cos-build
python3 ~/.codex/skills/cosup-chain-build/scripts/cosup_chain_build.py --force-recreate-worktrees
python3 ~/.codex/skills/cosup-chain-build/scripts/cosup_chain_build.py --riscv-toolchain-bin ~/toolchains/gcc/bin
python3 ~/.codex/skills/cosup-chain-build/scripts/cosup_chain_build.py --preserve-fw-debug-env
python3 ~/.codex/skills/cosup-chain-build/scripts/cosup_chain_build.py --git-pull
python3 ~/.codex/skills/cosup-chain-build/scripts/cosup_chain_build.py --cos-ref <oneos-ref> --fw-ref <slpvmfw-ref>
```

`--xctools-exe` may be a full Linux executable path such as `.../qzvmdk_linux`; the script converts it to the Ant prefix expected by `build_common.xml`.

Use `--cos-ref` or `--fw-ref` when the chain must be based on explicit local commits or tags instead of the mapped branch tips. These override all selected stages. For per-stage overrides, use `--cos-stage-ref COSB=<ref>` or `--fw-stage-ref COSC=<ref>`.

## Offline Verification

When QZVMDK authorization blocks fw generation, verify the chain with known-good historical mask outputs:

```bash
for spec in \
  "COSA cosup_COSA_V1 dev_4.0_COSA" \
  "COSB cosup_COSB_V1 dev_4.0_COSB" \
  "COSC cosup_COSC_V1 dev_4.0_COSC"
do
  set -- $spec
  stage="$1"; fw="$2"; cos="$3"
  fw_path="build/cosup_chain/worktrees/slpvmfw/$stage"
  cos_path="build/cosup_chain/worktrees/oneos-sim/$stage"
  [ -e "$fw_path/.git" ] || git -C ../slpvmfw worktree add --detach "$PWD/$fw_path" "$fw"
  [ -e "$cos_path/.git" ] || git worktree add --detach "$PWD/$cos_path" "$cos"
  mkdir -p "$fw_path/build/mask"
  git show "$cos:COS/components/qzvm/core/arch/qzvm/qzvm_mask.c" > "$fw_path/build/mask/qzvm_mask.c"
  git -C ../slpvmfw show "$fw:symbols.txt" > "$fw_path/symbols.txt"
done
```

Then run:

```bash
python3 ~/.codex/skills/cosup-chain-build/scripts/cosup_chain_build.py \
  --skip-fw-build \
  --jobs 8 \
  --strict-symbols \
  --allow-dirty
```

Expected symbol counts from the historical branches are:

| Stage | static symbols | table bytes |
| --- | ---: | ---: |
| COSA | 126 | 508 |
| COSB | 253 | 1016 |
| COSC | 316 | 1268 |

If these table bytes match the branch `qzvm_static_symbol.c` data, the automatic symbol ordering matches the manual `build/COSUP-MASKC` workflow.

## Known SDK Blockers

If `r1.8.4` reports `授权检查失败: 无法获取当前时间，请检查网络连接`, report it as a QZVMDK authorization/network-time blocker. Do not switch SDK versions unless the user explicitly asks; continue with offline verification only if the user accepts using historical mask artifacts.

## Success Report

Report:

- Branch mapping used for all stages.
- Whether full fw Ant builds ran or were skipped due SDK authorization.
- Static symbol counts and whether `qzvm_static_symbol.c` matches historical/manual output.
- COS build result and artifact paths under `build/cosup_chain/<stage>/`.
- Any remaining dirty files, especially detached worktree generated files.

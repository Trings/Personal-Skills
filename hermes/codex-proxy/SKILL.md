---
name: codex-proxy
description: "Transparent proxy to Codex CLI — forward user messages verbatim and return Codex output unmodified. Full permissions, no sandbox."
version: 1.0.0
category: autonomous-ai-agents
metadata:
  hermes:
    tags: [Codex, Proxy, Session-Management]
    related_skills: [codex]
---

# Codex 透明代理

把 Hermes 当作 Codex CLI 的透明代理。用户选中一个 session 后，后续所有发给 codex 的消息**原封不动**转发，codex 的回复**原封不动**返回。Hermes 不在中间添加任何额外内容。

## 触发条件

当用户说以下内容时加载此 skill：
- "用 codex 继续" / "继续 codex 对话"
- "codex session" / "codex 对话"
- "跟 codex 说 XXX" / "问 codex XXX"
- 明确提到要通过 codex 做某件事

## 第一步：选择 Session

先检查 memory 中是否已有活跃的 codex session ID（key: `codex_active_session`）。如果有，直接跳到第二步。

如果没有，用内置脚本列出最近 session：

```bash
python3 ~/.hermes/skills/codex-proxy/scripts/list_sessions.py -n 10
```

### list_sessions.py 详解

脚本路径：`scripts/list_sessions.py`

**功能**：从 Codex 的 SQLite 数据库（自动检测最新 `state_*.sqlite`）读取 `threads` 表，按更新时间倒序列出最近 N 个会话。

**跨版本兼容**：
- 自动检测 `state_5.sqlite`、`state_6.sqlite` 等（选 mtime 最新的）
- 检测 `thread_source` 列是否存在（新版 Codex 新增的列），不存在则 fallback 到 `source`
- 检测 `model` 列是否存在

**参数**：

| 参数 | 说明 |
|------|------|
| `-n <N>`, `--count <N>` | 列出最近 N 个 session（默认 15） |
| `--json` | JSON 格式输出，适合程序化处理 |

**文本输出示例**（默认）：
```
 1. 💻 编译 QEMU for cc2560a
    UUID: 019e05d5-xxxx-xxxx-xxxx-xxxxxxxxxxxx
    更新: 2026-05-11 10:30:00 | 目录: /home/tanrui/work/git/oneos-sim

 2. 📝 分析 qemu_init.py 流程
    UUID: 019e0716-xxxx-xxxx-xxxx-xxxxxxxxxxxx
    更新: 2026-05-10 18:00:00 | 目录: .../git/oneos-sim/Script/qemu
```

Source 图标：`📝` = VS Code，`💻` = CLI，`❓` = 未知来源

**JSON 输出示例**（`--json`）：
```json
[
  {
    "index": 1,
    "uuid": "019e05d5-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
    "title": "编译 QEMU for cc2560a",
    "source": "codex_cli",
    "cwd": "/home/tanrui/work/git/oneos-sim",
    "model": "gpt-5.4",
    "updated": "2026-05-11 10:30:00",
    "created": "2026-05-11 09:00:00"
  }
]
```

用户通过序号选择后，从输出中找到对应的 UUID，用 `memory(action='add', target='memory', content='codex_active_session=<UUID>')` 保存。

如果用户说 `--last` 或"最近那个"，直接用 `codex exec resume --last` 对应的 session，不需要列表。

## 第二步：透明转发

**核心规则：你（Hermes）是一个透明代理。**
- 用户说什么，你就原样传给 codex，不加任何前缀/后缀/解释
- Codex 返回什么，你就原样显示给用户，不加"Codex 说："之类的包装
- 不要问"需要我转发给 codex 吗？"——用户在这个模式下说的每句话都是给 codex 的

**命令模板：**

**第1步 — 执行 Codex（等待完成）：**
```bash
cd <WORKDIR> && codex exec \
  --dangerously-bypass-approvals-and-sandbox \
  -o /tmp/codex_response.txt \
  resume <SESSION_UUID> \
  "<用户消息原文>"
```
`timeout=600`（终端前台最大 600 秒；如果预计更久，用 background=true + notify_on_complete=true）

**第2步 — 读取纯净回复：**
调用 `read_file("/tmp/codex_response.txt")` 获取 codex 的最终回复，**只展示这个文件内容给用户**。不要展示 stdout 中的元信息（model、session id、tokens used 等）。

**重要：**
- `WORKDIR`：从 session 的 cwd 字段获取（见"获取 session workdir"），如果获取不到则用 `$HOME`
- `SESSION_UUID`：从 memory 中的 `codex_active_session` 获取
- 用户消息中的双引号要转义：`"` → `\"`
- `-o /tmp/codex_response.txt`：codex 只把最终回复写入此文件，不含元信息
- 如果 `/tmp/codex_response.txt` 为空或 codex 报错，展示完整 stderr/stdout

## 第三步：退出代理模式

当用户说以下内容时退出代理模式：
- "退出 codex" / "关闭 codex" / "结束 codex"
- "不用 codex 了"

退出时用 `memory(action='remove', target='memory', old_text='codex_active_session=<UUID>')` 清理。

## 查看历史会话内容

直接从 JSONL 提取，不经过 codex CLI。JSONL 文件位于 `~/.codex/sessions/**/*<uuid>*.jsonl`。

JSONL 事件结构（关键字段）：
- `type: "event_msg"` — 对话消息（用户和 Codex 都用这个类型）
- `type: "response_item"` — 工具调用/结果（大量，通常忽略）
- `type: "turn_context"` — 回合边界标记
- `type: "session_meta"` — 会话元信息

**区分用户消息和 Codex 消息**：`event_msg` 同时承载两者。用户消息的 payload 只有 `{"type", "message", "images", "local_images", "text_elements"}`；Codex 消息的 payload 多了 `phase` 和 `memory_citation` 字段。判断方法：payload 中无 `phase` 键 → 用户消息；有 `phase` → Codex 消息。

### 模式 A：只看 Codex 回复

用户说"抓一下 codex 说了什么"、"codex 的回复"时用此模式：

```bash
python3 -c "
import glob, json, os
session_id = '<UUID>'
pattern = os.path.expanduser(f'~/.codex/sessions/**/*{session_id}*.jsonl')
files = sorted(glob.glob(pattern, recursive=True))
if not files:
    print('未找到会话文件')
else:
    msgs = []
    with open(files[-1], 'r') as f:
        for line in f:
            if '\"agent_message\"' in line:
                msgs.append(json.loads(line))
    print(f'共 {len(msgs)} 条 codex 回复')
    for i, m in enumerate(msgs, 1):
        print(f'\\n--- 第 {i} 条 ---')
        print(m['payload'].get('message', '')[:5000])
"
```

### 模式 B：完整交叉对话（用户 + Codex 交替）

用户说"完整对话"、"我和 codex 都说了什么"、"交叉展示"时用此模式。

**第一步 — 提取并统计**（过滤 Codex 进度噪音，保留关键对话）：

```bash
python3 -c "
import glob, json, os

session_id = '<UUID>'
pattern = os.path.expanduser(f'~/.codex/sessions/**/*{session_id}*.jsonl')
files = sorted(glob.glob(pattern, recursive=True))

with open(files[-1], 'r') as f:
    events = [json.loads(line) for line in f if line.strip()]

msgs = []
for e in events:
    if e.get('type') != 'event_msg':
        continue
    p = e.get('payload', {})
    if 'message' not in p:
        continue
    role = '👤 你' if 'phase' not in p else '🤖 Codex'
    msgs.append((role, p['message']))

total = len(msgs)
print(f'共 {total} 条消息，过滤琐碎进度后展示关键对话')
print('=' * 70)

for i, (role, text) in enumerate(msgs, 1):
    is_user = role == '👤 你'
    # 用户消息全保留；Codex 消息过滤短进度（<120 字）
    if is_user or len(text) > 120:
        print(f'\\n{role} [{i}/{total}]')
        print(text[:1500])
        print()
"
```

**第二步 — 仅展示用户消息**（如果想快速看用户说了什么）：

```bash
python3 -c "
import glob, json, os
session_id = '<UUID>'
pattern = os.path.expanduser(f'~/.codex/sessions/**/*{session_id}*.jsonl')
files = sorted(glob.glob(pattern, recursive=True))
with open(files[-1], 'r') as f:
    events = [json.loads(line) for line in f if line.strip()]
user_msgs = []
for e in events:
    if e.get('type') != 'event_msg':
        continue
    p = e.get('payload', {})
    if 'phase' not in p and 'message' in p:
        user_msgs.append(p['message'])
print(f'共 {len(user_msgs)} 条用户消息')
for i, msg in enumerate(user_msgs, 1):
    print(f'[{i}] {msg}')
"
```

### 查看对话时需注意

- **过滤噪音**：Codex 的长回归任务会产生大量轮询进度消息。对非用户消息按长度阈值（建议 120 字）过滤。详见 `references/conv-extraction.md`
- **先统计、再展示**：300+ 条消息的会话先打统计（事件类型分布、消息数），让用户了解规模后再决定看多少
- **用户消息全保留**：用户消息通常很短但都很重要，不做过滤
- **可运行脚本**：`references/conv-extraction.md` 末尾有完整的可执行提取脚本，支持 `--full` 参数

## 获取 session 的 workdir

```bash
python3 -c "
import sqlite3
from pathlib import Path
codex_dir = Path.home() / '.codex'
dbs = sorted(codex_dir.glob('state_*.sqlite'), key=lambda p: p.stat().st_mtime, reverse=True)
db = sqlite3.connect(str(dbs[0])) if dbs else None
if db:
    r = db.execute('SELECT cwd FROM threads WHERE id=?', ('<UUID>',)).fetchone()
    print(r[0] if r and r[0] else str(Path.home()))
else:
    print(str(Path.home()))
"
```

如果获取失败，fallback 到 `$HOME`。

### Pitfalls

1. **永远不要在用户消息外包裹任何内容**。如果用户说"你好"，就传 `"你好"`，不要传 `"用户让我问你：你好"`
2. **永远不要在 codex 回复外包裹任何内容**。直接用 `read_file("/tmp/codex_response.txt")` 获取。这个文件通过 `-o` 参数只包含 codex 的最终回复，没有元信息。
3. **超时处理**：如果超时（exit_code=124），告知用户"Codex 超时，可能需要拆分任务或加大超时"
4. **特殊字符**：用户消息中的 `"` `$` `` ` `` `\` 需要正确转义后放入 shell 命令
5. **如果 `-o` 文件为空或 codex 报错**，把完整的 stdout/stderr 展示给用户以便排查
6. **Codex 自动更新**：Codex CLI 启动时可能自动检查并安装更新（`~/.codex/version.json` 记录 `last_checked_at`）。更新会改变 session 存储格式、事件结构等。如果 session 行为异常，先检查 `codex --version` 是否变化。

## 示例工作流

```
用户: 用 codex 继续最近的对话
Hermes: [加载 skill → 查询 memory → 没有活跃 session]
Hermes: [运行 list_sessions.py → 展示列表]
用户: 选第1个
Hermes: [提取 UUID → 保存到 memory]
Hermes: [运行 codex exec resume <UUID> --dangerously... 但不加 prompt，仅确认连接]
Hermes: 已连接 session: "编译 QEMU" (019e05d5...)。请发送你的消息。

用户: 帮我看看 COS/sim 目录的结构
Hermes: [运行 codex exec --dangerously... resume <UUID> "帮我看看 COS/sim 目录的结构"]
Hermes: [输出 codex 的回复，不加任何包装]
```


# Codex JSONL 会话提取详解

## JSONL 文件位置

```
~/.codex/sessions/**/*<uuid>*.jsonl
```

取 `sorted(glob.glob(...))[-1]`（最新文件）。

## 事件类型

| type | 说明 | 数量占比 |
|------|------|----------|
| `response_item` | 工具调用/结果（含 `tool_use`、`tool_result`、`reasoning` 等子类型）。极大量，通常忽略。 | ~65% |
| `event_msg` | 对话消息。用户和 Codex 都用此类型。 | ~35% |
| `turn_context` | 回合边界。一轮交互 = 用户消息 + N 个 Codex 消息 + 工具调用。 | <1% |
| `session_meta` | 会话元信息（模型、system prompt 等） | <1% |
| `compacted` | 上下文压缩（compaction）时的摘要信息 | <1% |

## 区分用户 vs Codex 消息

`event_msg` 的 payload 结构不同：

**用户消息** — 无 `phase` 键：
```json
{
  "type": "event_msg",
  "payload": {
    "type": "user_prompt",
    "message": "用户的原文...",
    "images": [],
    "local_images": [],
    "text_elements": []
  }
}
```

**Codex 消息** — 有 `phase` 键：
```json
{
  "type": "event_msg",
  "payload": {
    "type": "agent_message",
    "message": "Codex 的回复...",
    "phase": "planning|executing|reflecting|summarizing",
    "memory_citation": {}
  }
}
```

判断代码：
```python
role = '👤 你' if 'phase' not in p else '🤖 Codex'
```

## 噪音过滤

Codex 在长时间回归测试中产生大量琐碎进度消息（"PASS 454、0 FAIL"、"继续跑"、"编译通过了"）。对 Codex 消息用长度阈值过滤（建议 120 字），用户消息全保留。

### 常见噪音模式

- 进度轮询：`"api 进度 90/1967"`、`"PASS 暂停在 454"`、`"已完成 68/179"`
- 同步确认：`"两边同步"`、`"当前和基线完全同步"`、`"仍同步"`
- 状态过渡：`"编译已经启动"`、`"编译通过了"`、`"构建完成了"`
- 继续指令：`"继续跑"`、`"继续等它收尾"`、`"我继续"`

## 完整提取脚本

```python
import glob, json, os, sys

session_id = sys.argv[1] if len(sys.argv) > 1 else None
if not session_id:
    print("Usage: python3 extract_conv.py <UUID> [--full]")
    sys.exit(1)

full_mode = '--full' in sys.argv
pattern = os.path.expanduser(f'~/.codex/sessions/**/*{session_id}*.jsonl')
files = sorted(glob.glob(pattern, recursive=True))
if not files:
    print('未找到会话文件')
    sys.exit(1)

with open(files[-1], 'r') as f:
    events = [json.loads(line) for line in f if line.strip()]

# 统计
from collections import Counter
types = Counter(e.get('type', '?') for e in events)
print(f'文件: {files[-1]}')
print(f'事件总数: {len(events)}')
print(f'事件类型: {dict(types)}')
print()

# 提取消息
msgs = []
for e in events:
    if e.get('type') != 'event_msg':
        continue
    p = e.get('payload', {})
    if 'message' not in p:
        continue
    role = '👤 你' if 'phase' not in p else '🤖 Codex'
    msgs.append((role, p['message']))

print(f'对话消息: {len(msgs)}')
user_count = sum(1 for r, _ in msgs if r == '👤 你')
agent_count = len(msgs) - user_count
print(f'用户: {user_count}, Codex: {agent_count}')
print('=' * 70)

min_len = 0 if full_mode else 120
for i, (role, text) in enumerate(msgs, 1):
    if role == '👤 你' or len(text) > min_len:
        print(f'\n{role} [{i}/{len(msgs)}]')
        max_len = 5000 if full_mode else 1500
        print(text[:max_len])
```

用法：
```bash
python3 extract_conv.py <UUID>        # 过滤模式（默认）
python3 extract_conv.py <UUID> --full # 全量，不限长度
```

## 注意事项

- **版本差异**：Codex 更新可能改变 JSONL 字段名。`phase` 键是可用的稳定区分标志（v0.116 验证通过）
- **大文件**：256 轮以上的会话 JSONL 可能超过 50MB，`glob` + 逐行 `json.loads` 内存友好
- **compacted 事件**：当上下文过长触发压缩时，旧消息会被替换为摘要。长时间会话的第一条消息可能不是用户原文而是摘要
- **工具调用**：`response_item` 包含完整的工具调用链（`tool_use` → `tool_result`），如需排查 Codex 的操作细节，解析这些事件

---
name: file-watchdog
description: Monitor a file for changes — notify when content updates, auto-cleanup when file is deleted.
category: devops
---

# File Watchdog

Monitor text files for content changes. Designed for the common case: another agent writes a report, you get notified.

## Portability

To install on another machine: copy the entire `file-watchdog/` directory into `~/.hermes/skills/`. The GC job is auto-created on first use — no manual setup needed.

## When to use

"监控这个文件" / "monitor this file" / "watch X for changes"

## Architecture

1. **Script** `~/.hermes/scripts/watch_<safe_name>.sh` — pure bash, hash check, runs via cron
2. **Cron job** `no_agent=true` — runs the script on schedule
3. **GC job** — daily 3 AM, cleans monitors whose target file was deleted. Auto-bootstrapped.

## Bootstrap (first use)

Before creating the first monitor, ensure a GC job exists:

```python
jobs = cronjob(action='list')
gc_exists = any(j.get('name') == 'Watchdog GC' for j in jobs['jobs'])
if not gc_exists:
    cronjob(
        action='create',
        name='Watchdog GC',
        schedule='0 3 * * *',
        deliver='origin',
        enabled_toolsets=['terminal', 'file', 'cronjob'],
        prompt='''You are the watchdog garbage collector.
1. List files in ~/.hermes/scripts/.watchdog_markers/
2. For each marker (filename = job_id):
   a. Remove cron job: cronjob(action='remove', job_id=...)
   b. Delete script: ~/.hermes/scripts/watch_*.sh for this job
   c. Delete state: ~/.hermes/scripts/.watchdog_<job_id>_hash
   d. Delete the marker itself
3. Report what was cleaned, or "No orphans" if empty.'''
    )
```

To find the GC job later, search by name: filter `cronjob(action='list')` for `name == 'Watchdog GC'`.

## Creating a monitor

### Step 1: Parse user request

- **File path**: absolute. Resolve relative paths with current workdir.
- **Interval**: default `every 10m`. Parse from natural language:

| User says | Schedule |
|-----------|----------|
| 每分钟 / every 1m | `every 1m` |
| 每5分钟 / every 5m | `every 5m` |
| (unspecified) | `every 10m` |
| 每30分钟 / 半小时 / every 30m | `every 30m` |
| 每小时 / every 1h | `every 1h` |
| 每2小时 / every 2h | `every 2h` |

Minimum: `every 1m`. Don't ask if already in the message.

### Step 2: Generate script name

```bash
# /home/user/log.txt → watch_home_user_log_txt.sh
echo "watch_$(echo '$PATH' | tr '/' '_' | tr -c 'a-zA-Z0-9_' '_' | sed 's/__*/_/g').sh"
```

### Step 3: Create script + cron job

1. Bootstrap GC if needed (see Bootstrap section)
2. Write script from `templates/watchdog.sh` to `~/.hermes/scripts/<safe_name>`, using `JOB_ID="TBD"` as placeholder
3. Create cron job: `cronjob(action='create', name='监控 <filename>', schedule='<interval>', script='<safe_name>', no_agent=True, deliver='origin')`
4. Patch script: replace `JOB_ID="TBD"` with the returned job_id

### Step 4: Confirm

```
✅ 已创建监控: /path/to/file (每10分钟, job: abc123)
```

## Querying monitors

User: "有哪些监控？" / "show watchers"

1. `cronjob(action='list')` → filter where `script` starts with `watch_`
2. For each, `grep -oP '^FILE="\K[^"]+' ~/.hermes/scripts/<script>` to get path
3. Present table:

```
📊 监控任务：

| 文件 | 间隔 | 状态 | Job ID |
|------|------|------|--------|
| /path/a | every 10m | ✅ | abc123 |
| /path/b | every 5m  | ⚠️ 文件不存在 | def456 |
```

## Stopping a monitor

User: "停止监控 /path/to/file" / "stop watching X"

1. Query to find the job_id
2. `cronjob(action='remove', job_id=...)`
3. Delete `~/.hermes/scripts/watch_<safe_name>.sh`
4. Delete `~/.hermes/scripts/.watchdog_<job_id>_hash`

## Auto-cleanup (GC)

When a monitored file is deleted, the watchdog script marks `~/.hermes/scripts/.watchdog_markers/<job_id>`. The GC job (found by name `"Watchdog GC"`) picks these up at 3 AM daily and removes the cron job + script + state file. Deletion alert is instant; cleanup may take up to 24h.

## Pitfalls

- **Text files assumed** — `cat` output may be garbled for binaries
- **Content-based** — only actual content changes trigger; `touch` alone won't
- **Baseline on first run** — current content is the baseline; writes must differ to trigger
- **Min interval 1 minute** — sub-minute not supported
- **Requires** `md5sum` (coreutils) and `grep -P` — standard on Ubuntu

#!/bin/bash
# File Watchdog — monitors a text file for content changes.
# Replace <FILE_PATH> and <JOB_ID> at creation time.
FILE="<FILE_PATH>"
JOB_ID="<JOB_ID>"
STATE="$HOME/.hermes/scripts/.watchdog_${JOB_ID}_hash"
GC_DIR="$HOME/.hermes/scripts/.watchdog_markers"

# File deleted → alert once, mark for GC
if [ ! -f "$FILE" ]; then
    if [ -f "$STATE" ]; then
        echo "⚠️ 文件已删除: $FILE"
        mkdir -p "$GC_DIR" && touch "$GC_DIR/${JOB_ID}"
        rm -f "$STATE"
    fi
    exit 0
fi

# Check hash, notify on change
CURRENT=$(md5sum "$FILE" | awk '{print $1}')
LAST=$(cat "$STATE" 2>/dev/null)

if [ "$CURRENT" != "$LAST" ]; then
    echo "$CURRENT" > "$STATE"
    echo "📄 $FILE 更新："
    echo "---"
    cat "$FILE"
    echo "---"
fi

"""
Append-only JSONL audit trail. Every agent decision, gate verdict, and MCP
tool call gets one line here. Nothing is ever overwritten or deleted -
that's the point of an audit trail.
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path


class AuditLogger:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, event_type: str, **fields) -> dict:
        entry = {
            "event_id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            **fields,
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
        return entry

    def read_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

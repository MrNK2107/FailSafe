import json
import os
import threading
import uuid
from datetime import UTC, datetime, timezone
from pathlib import Path


class AuditLogger:
    _lock = threading.Lock()

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = str(uuid.uuid4())

    def log(self, event_type: str, **fields) -> dict:
        entry = {
            "event_id": str(uuid.uuid4()),
            "run_id": self.run_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "event_type": event_type,
            **fields,
        }
        with self._lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
                f.flush()
                os.fsync(f.fileno())
        return entry

    def read_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        results = []
        with self.path.open(encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    results.append(json.loads(line))
                except json.JSONDecodeError:
                    print(f"[audit_log] warning: skipping corrupted line in {self.path}")
                    continue
        return results

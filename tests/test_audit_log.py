import datetime
import json
import threading

from failsafe.audit_log import AuditLogger


def test_read_all_missing_file_returns_empty(tmp_path):
    logger = AuditLogger(tmp_path / "nonexistent.jsonl")
    assert logger.read_all() == []


def test_corrupted_jsonl_line_ignored(tmp_path):
    path = tmp_path / "audit.jsonl"
    logger = AuditLogger(path)
    logger.log("ok_event", value=1)
    with path.open("a", encoding="utf-8") as f:
        f.write("NOT VALID JSON {{{\n")
    logger.log("ok_event2", value=2)
    entries = logger.read_all()
    assert len(entries) == 2
    assert entries[0]["value"] == 1
    assert entries[1]["value"] == 2


def test_mkdir_parents(tmp_path):
    deep = tmp_path / "a" / "b" / "c" / "audit.jsonl"
    assert not deep.parent.exists()
    logger = AuditLogger(deep)
    logger.log("test", x=1)
    assert deep.exists()
    assert deep.parent.exists()


def test_non_serializable_default_str(tmp_path):
    logger = AuditLogger(tmp_path / "audit.jsonl")
    dt = datetime.datetime(2026, 1, 1, 12, 0, 0)
    entry = logger.log("test", weird_object=dt, also_set={1, 2, 3})
    assert "weird_object" in entry
    assert isinstance(entry["weird_object"], datetime.datetime)
    raw = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    parsed = json.loads(raw.strip())
    assert isinstance(parsed["weird_object"], str)
    assert "2026" in parsed["weird_object"]


def test_concurrent_append(tmp_path):
    path = tmp_path / "audit.jsonl"
    logger = AuditLogger(path)
    n_threads = 5
    n_per_thread = 20

    def worker(tid):
        for i in range(n_per_thread):
            logger.log("concurrent", tid=tid, idx=i)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    entries = logger.read_all()
    assert len(entries) == n_threads * n_per_thread
    assert len({e["event_id"] for e in entries}) == n_threads * n_per_thread


def test_empty_lines_ignored(tmp_path):
    path = tmp_path / "audit.jsonl"
    logger = AuditLogger(path)
    logger.log("a", v=1)
    with path.open("a", encoding="utf-8") as f:
        f.write("\n\n   \n")
    logger.log("b", v=2)
    assert len(logger.read_all()) == 2


def test_log_returns_entry_and_persists(tmp_path):
    logger = AuditLogger(tmp_path / "audit.jsonl")
    entry = logger.log("my_event", foo="bar")
    assert entry["event_type"] == "my_event"
    assert entry["foo"] == "bar"
    assert "event_id" in entry
    assert "timestamp" in entry
    assert logger.read_all()[0]["event_id"] == entry["event_id"]

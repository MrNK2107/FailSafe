"""Every pipeline appends to its checkpoint by writing a temp copy and
os.replace()-ing it over the original, so a crash mid-write can never
leave a truncated checkpoint (which would make a resumed run redo, or
skip, real money actions). These tests make the replace fail and check
that the original checkpoint is untouched, the temp file is cleaned up,
and the error still propagates (a failed checkpoint must not be silent)."""

import json
import os

import pytest

from failsafe import (
    agent,
    agent_onetime,
    checkout_abandonment_agent,
    integrated_pipeline,
    receivables_agent,
)

MODULES = [
    (agent, "CHECKPOINT_PATH"),
    (agent_onetime, "CHECKPOINT_PATH"),
    (checkout_abandonment_agent, "CHECKPOINT_PATH"),
    (receivables_agent, "CHECKPOINT_PATH"),
    (integrated_pipeline, "INTEGRATED_CHECKPOINT_PATH"),
]


@pytest.mark.parametrize(
    ("mod", "attr"), MODULES, ids=[m.__name__.rsplit(".", 1)[1] for m, _ in MODULES]
)
def test_append_checkpoint_appends_then_survives_failed_replace(mod, attr, tmp_path, monkeypatch):
    ckpt = tmp_path / "ck" / "checkpoint.jsonl"
    monkeypatch.setattr(mod, attr, ckpt)

    mod._append_checkpoint({"id": 1})  # creates the file
    mod._append_checkpoint({"id": 2})  # temp-copy + replace path
    assert [json.loads(line)["id"] for line in ckpt.read_text(encoding="utf-8").splitlines()] == [
        1,
        2,
    ]
    before = ckpt.read_bytes()

    def boom(src, dst):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        mod._append_checkpoint({"id": 3})

    assert ckpt.read_bytes() == before
    assert sorted(p.name for p in ckpt.parent.iterdir()) == ["checkpoint.jsonl"]

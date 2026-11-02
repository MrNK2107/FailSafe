import importlib.util
import json
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "corrected_override_metric",
    Path(__file__).resolve().parent.parent / "scripts" / "corrected_override_metric.py",
)
assert _spec and _spec.loader
com = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(com)


def _ev(rid, et, **kw):
    return {"subscription_id": rid, "event_type": et, **kw}


def test_reconstruct_last_terminal_event_wins_and_failures_are_not_proposals():
    events = [
        _ev("a", "gate_decision", llm_matched_policy=False),  # first run: override
        _ev("a", "gate_decision", llm_matched_policy=True),  # resumed re-log: match wins
        _ev("b", "llm_parse_failure"),
        _ev("b", "gate_decision", llm_matched_policy=False),  # fallback, not a proposal
        _ev("c", "llm_invalid_action"),
        _ev("c", "gate_decision", llm_matched_policy=False),
        _ev("c", "gate_decision", llm_matched_policy=False),  # later clean re-run: real override
        _ev("d", "unknown_decline_code"),
        _ev("e", "circuit_open_short_circuit"),
        {"event_type": "run_started"},
    ]
    rows = com.reconstruct(events, "subscription_id")
    assert rows == {
        "a": {"llm_matched_policy": True, "llm_proposed": True},
        "b": {"llm_matched_policy": False, "llm_proposed": False},
        "c": {"llm_matched_policy": False, "llm_proposed": True},
        "d": {"llm_matched_policy": False, "llm_proposed": False},
        "e": {"llm_matched_policy": False, "llm_proposed": False},
    }


def test_render_writes_both_metrics(tmp_path):
    log = tmp_path / "a.jsonl"
    log.write_text(
        "\n".join(
            json.dumps(e)
            for e in [
                _ev("a", "gate_decision", llm_matched_policy=False),
                _ev("b", "llm_parse_failure"),
                _ev("b", "gate_decision", llm_matched_policy=False),
            ]
        )
        + "\nnot json\n",
        encoding="utf-8",
    )
    md = com.render(
        [("Subs", log, "subscription_id"), ("Missing", tmp_path / "none.jsonl", "payment_id")]
    )
    assert "| Subs | 2/2 (100.0%) | 1/1 (100.0%) | 1 |" in md
    assert "(no log at none.jsonl)" in md

"""
End-to-end tests of run() for the two standalone diagnosis-driven domains
(checkout abandonment, overdue receivables). process_one() is already
covered by test_checkout_abandonment_pipeline.py / test_receivables_pipeline.py;
these tests cover the batch loop around it, which nothing else reaches:

  * checkpoint resume (skips done records, seeds the gate's spend/idempotency
    state from the checkpoint so a resumed run cannot double-act),
  * corrupted checkpoint lines are skipped, not fatal,
  * a per-record crash degrades to "needs human review" instead of killing
    the batch,
  * the RESULTS markdown's accuracy / "misdiagnosis changed the action"
    numbers are what the per-record results say they are,
  * an empty batch does not crash (regression: ZeroDivisionError),
  * force_simulate restores the global SIMULATE switches afterwards.

Diagnosis (the LLM call) is patched; the gate, policy, audit log and the
real in-process MCP server (forced into SIMULATE) are all real.
"""

import asyncio
import json
import re
from unittest.mock import patch

import pytest

from failsafe import checkout_abandonment_agent as cart_agent
from failsafe import mcp_server
from failsafe import receivables_agent as recv_agent


def _cart(cart_id, reason="otp_delay_or_failure", **kw):
    rec = {
        "cart_id": cart_id,
        "amount_paise": 29900,
        "item": "Test Item",
        "checkout_stage": "otp_entry",
        "minutes_since_abandonment": 3,
        "device_type": "mobile_web",
        "is_returning_customer": True,
        "abandonment_reason": reason,
        "simulated_customer_response": True,
    }
    rec.update(kw)
    return rec


def _invoice(invoice_id, reason="payment_process_friction", **kw):
    rec = {
        "invoice_id": invoice_id,
        "amount_paise": 45000,
        "business_name": "Test Business Pvt Ltd",
        "days_overdue": 5,
        "payment_terms": "net_30",
        "customer_payment_history_signal": "first_time_overdue",
        "reminders_sent_count": 0,
        "last_reminder_response": None,
        "typical_order_amount_paise": 45000,
        "amount_vs_typical_ratio": 1.0,
        "case_reason": reason,
        "simulated_customer_response": True,
    }
    rec.update(kw)
    return rec


DOMAINS = {
    "checkout": {
        "module": cart_agent,
        "make": _cart,
        "id": "cart_id",
        "truth": "abandonment_reason",
        "diag_key": "reason",
        "diagnosed": "diagnosed_reason",
        "diagnose_fn": "diagnose_abandonment_reason",
        "right": "otp_delay_or_failure",
        "right_action": "immediate_payment_link_resend",
        # misdiagnosis with a genuinely different policy action
        "wrong": "trust_or_security_concern",
        "wrong_action": "no_action_respect_hesitation",
        "gate_cls": "AbandonmentGate",
    },
    "receivables": {
        "module": recv_agent,
        "make": _invoice,
        "id": "invoice_id",
        "truth": "case_reason",
        "diag_key": "case_reason",
        "diagnosed": "diagnosed_case_reason",
        "diagnose_fn": "diagnose_receivable",
        "right": "payment_process_friction",
        "right_action": "friendly_reminder",
        "wrong": "invoice_dispute_likely",
        "wrong_action": "no_action_needs_dispute_review",
        "gate_cls": "ReceivableGate",
    },
}


@pytest.fixture(params=list(DOMAINS))
def dom(request, tmp_path):
    d = dict(DOMAINS[request.param])
    mod = d["module"]
    paths = {
        "DATA_PATH": tmp_path / "data.json",
        "AUDIT_PATH": tmp_path / "audit.jsonl",
        "CHECKPOINT_PATH": tmp_path / "checkpoint.jsonl",
        "RESULTS_PATH": tmp_path / "RESULTS.md",
    }
    patches = [patch.object(mod, k, v) for k, v in paths.items()]
    for p in patches:
        p.start()
    d.update(paths)
    yield d
    for p in reversed(patches):
        p.stop()


def _diag(d, reason):
    return {d["diag_key"]: reason, "reasoning": "mocked"}


def _write_data(d, records):
    d["DATA_PATH"].write_text(json.dumps(records), encoding="utf-8")


def _run(d, diagnoses, **kw):
    with patch.object(d["module"], d["diagnose_fn"], side_effect=diagnoses) as m:
        results = asyncio.run(d["module"].run(**kw))
    return results, m


def _checkpoint(d):
    return [
        json.loads(line)
        for line in d["CHECKPOINT_PATH"].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _audit(d):
    return [
        json.loads(line)
        for line in d["AUDIT_PATH"].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_missing_data_file_exits_with_hint(dom):
    with pytest.raises(SystemExit, match="No data found"):
        asyncio.run(dom["module"].run())


def test_run_mixed_batch_writes_checkpoint_audit_and_honest_metrics(dom):
    mk = dom["make"]
    _write_data(dom, [mk("r1"), mk("r2"), mk("r3")])
    results, _ = _run(
        dom,
        [
            _diag(dom, dom["right"]),  # correct
            _diag(dom, dom["wrong"]),  # wrong, and changes the action
            _diag(dom, None),  # diagnosis failure -> human review
        ],
    )

    by_id = {r[dom["id"]]: r for r in results}
    assert by_id["r1"]["final_action"] == dom["right_action"]
    assert by_id["r1"]["diagnosis_matched_ground_truth"] is True
    assert by_id["r2"]["final_action"] == dom["wrong_action"]
    assert by_id["r2"]["diagnosis_matched_ground_truth"] is False
    assert by_id["r3"]["final_action"] == "no_action_needs_human_review"
    assert by_id["r3"][dom["diagnosed"]] is None

    assert [r[dom["id"]] for r in _checkpoint(dom)] == ["r1", "r2", "r3"]

    types = [e["event_type"] for e in _audit(dom)]
    assert types[0] == "run_started" and types[-1] == "run_finished"

    md = dom["RESULTS_PATH"].read_text(encoding="utf-8")
    assert "1/3 (33.3%)" in md  # diagnosis accuracy
    # r2 (wrong + different action) and r3 (failure) both changed the outcome.
    assert "**2/3**" in md
    # Only r1 was an executed, actionable recovery.
    assert re.search(r"Actions executed[^*]*\*\*1\*\*", md)
    assert f"| r2 | {dom['right']} | {dom['wrong']} | no | {dom['wrong_action']} |" in md


def test_n_limits_the_batch(dom):
    mk = dom["make"]
    _write_data(dom, [mk("r1"), mk("r2"), mk("r3")])
    results, m = _run(dom, [_diag(dom, dom["right"])], n=1)
    assert [r[dom["id"]] for r in results] == ["r1"]
    assert m.call_count == 1


def test_empty_batch_does_not_crash(dom):
    """Regression: write_results divided by len(results) and raised
    ZeroDivisionError on an empty data file / n=0 / fully-resumed empty run."""
    _write_data(dom, [])
    results, m = _run(dom, [])
    assert results == []
    assert m.call_count == 0
    assert "No records processed" in dom["RESULTS_PATH"].read_text(encoding="utf-8")


def test_resume_skips_done_records_and_seeds_gate_state(dom):
    mk = dom["make"]
    _write_data(dom, [mk("r1"), mk("r2")])
    # First run processes only r1.
    _run(dom, [_diag(dom, dom["right"])], n=1)

    gate_cls = getattr(dom["module"], dom["gate_cls"])
    with patch.object(
        gate_cls, "seed_from_checkpoint", autospec=True, side_effect=gate_cls.seed_from_checkpoint
    ) as seed:
        results, m = _run(dom, [_diag(dom, dom["right"])])

    # Only r2 reached diagnosis; r1 came from the checkpoint.
    assert m.call_count == 1
    assert [r[dom["id"]] for r in results] == ["r1", "r2"]
    assert [r[dom["id"]] for r in _checkpoint(dom)] == ["r1", "r2"]
    # The executed r1 action counts against the spending cap and its
    # idempotency key is pre-seen, so a resumed run cannot repeat it.
    _, spent, seen = seed.call_args.args
    assert spent == mk("r1")["amount_paise"]
    assert ("r1", dom["right_action"]) in seen


def test_corrupted_checkpoint_line_is_skipped(dom, capsys):
    mk = dom["make"]
    _write_data(dom, [mk("r1"), mk("r2")])
    _run(dom, [_diag(dom, dom["right"])], n=1)
    with dom["CHECKPOINT_PATH"].open("a", encoding="utf-8") as f:
        f.write("{not json\n\n")

    results, m = _run(dom, [_diag(dom, dom["right"])])
    assert m.call_count == 1
    assert {r[dom["id"]] for r in results} == {"r1", "r2"}
    assert "skipping corrupted line" in capsys.readouterr().out


def test_per_record_crash_degrades_to_human_review_and_batch_continues(dom):
    mk = dom["make"]
    _write_data(dom, [mk("r1"), mk("r2")])
    results, _ = _run(dom, [RuntimeError("ollama exploded"), _diag(dom, dom["right"])])

    crashed, ok = results
    assert crashed["final_action"] == "no_action_needs_human_review"
    assert crashed["gate_executed"] is False
    assert crashed["simulated_customer_response"] is False
    assert ok["final_action"] == dom["right_action"]

    errs = [e for e in _audit(dom) if e["event_type"] == "record_processing_error"]
    assert len(errs) == 1 and "ollama exploded" in errs[0]["error"]
    # A crash is a changed outcome, never counted as a recovery.
    md = dom["RESULTS_PATH"].read_text(encoding="utf-8")
    assert "**1/2**" in md


def test_force_simulate_restores_global_switches(dom):
    _write_data(dom, [dom["make"]("r1")])
    before = (mcp_server.SIMULATE, mcp_server._rp.simulate)
    with (
        patch.object(mcp_server, "SIMULATE", False),
        patch.object(mcp_server._rp, "simulate", False),
    ):
        seen = {}

        def _spy(**kw):
            seen["sim"] = (mcp_server.SIMULATE, mcp_server._rp.simulate)
            return _diag(dom, dom["right"])

        with patch.object(dom["module"], dom["diagnose_fn"], side_effect=_spy):
            asyncio.run(dom["module"].run())
        assert seen["sim"] == (True, True)
        assert (mcp_server.SIMULATE, mcp_server._rp.simulate) == (False, False)
    assert (mcp_server.SIMULATE, mcp_server._rp.simulate) == before


def test_extract_tool_text_handles_empty_and_textless_content(dom):
    class Call:
        def __init__(self, content):
            self.content = content

    class NoText:
        pass

    class Text:
        text = "hello"

    f = dom["module"].extract_tool_text
    assert f(Call([])) is None
    assert f(Call([NoText()])) is None
    assert f(Call([Text()])) == "hello"

"""
Offline demo: runs the real agent.run() batch loop - real gate, real policy
table, real in-process MCP server (SIMULATE mode), real audit log - with NO
Ollama and NO Razorpay keys. Everything is written to a throwaway temp
directory; the committed logs/, RESULTS.md and REPORT.html are never touched.

The LLM is replaced by a deliberately naive stand-in that proposes
`immediate_retry` for every record, so you can watch the deterministic gate
override it wherever policy disagrees (that is the point of the design: the
model proposes, the gate decides). Diagnosis is stubbed with an exact lookup
of the synthetic dataset's own raw messages - clearly NOT a model.

Usage:
    python scripts/offline_demo.py            # 12 records
    python scripts/offline_demo.py --n 40
    python scripts/offline_demo.py --keep     # keep the temp dir + REPORT.html
"""

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))  # so the `failsafe` package is importable without an install

from failsafe import (  # noqa: E402
    agent,
    decision_trace,
    generate_report,
    mcp_server,
    recovery_pipeline,
    replay,
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument(
        "--n", type=int, default=12, help="number of records from data/halted_subscriptions.json"
    )
    ap.add_argument("--keep", action="store_true", help="keep the temp output directory")
    args = ap.parse_args()

    all_subs = json.loads((REPO / "data" / "halted_subscriptions.json").read_text(encoding="utf-8"))
    subs = all_subs[: args.n]
    raw_to_code = {
        s["raw_decline_message"]: s["decline_code"]
        for s in all_subs
        if s.get("raw_decline_message")
    }

    out = Path(tempfile.mkdtemp(prefix="failsafe_demo_"))
    data = out / "halted_subscriptions.json"
    data.write_text(json.dumps(subs), encoding="utf-8")

    naive_llm = MagicMock(
        side_effect=lambda *a, **k: {
            "action": "immediate_retry",
            "reasoning": "[offline stand-in] always retries immediately",
        }
    )

    def stub_diagnosis(raw: str, amount_paise=None):
        code = raw_to_code.get(raw)
        return {"decline_code": code, "reasoning": "[offline stub] dataset lookup"}

    with (
        patch.object(agent, "DATA_PATH", data),
        patch.object(agent, "AUDIT_PATH", out / "audit_log.jsonl"),
        patch.object(agent, "CHECKPOINT_PATH", out / "results_checkpoint.jsonl"),
        patch.object(agent, "RESULTS_PATH", out / "RESULTS.md"),
        mcp_server.simulate_mode(True),
        recovery_pipeline.use_proposer(naive_llm),
        patch.object(recovery_pipeline, "diagnose_decline_code", side_effect=stub_diagnosis),
    ):
        print(
            f"== FailSafe offline demo: {len(subs)} halted subscriptions, naive stand-in LLM, SIMULATE mode ==\n"
        )
        asyncio.run(agent.run(skip_healthcheck=True))

    with (
        patch.object(generate_report, "AUDIT_PATH", out / "audit_log.jsonl"),
        patch.object(generate_report, "REPORT_PATH", out / "REPORT.html"),
    ):
        generate_report.build_report()

    # Decision Trace page + a deterministic replay of what the gate just did.
    trace = decision_trace.build(
        out / "audit_log.jsonl", out / "TRACE.html", replay.DEFAULT_POLICY, data
    )

    events = [
        json.loads(line)
        for line in (out / "audit_log.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    decisions = [e for e in events if e["event_type"] == "gate_decision"]
    overridden = [d for d in decisions if d["llm_proposed_action"] != d["final_action"]]
    tools = Counter(e["tool"] for e in events if e["event_type"] == "mcp_tool_call")

    print("\n== Summary ==")
    print(f"gate decisions:            {len(decisions)}")
    print(
        f"LLM proposals overridden:  {len(overridden)}/{len(decisions)} (stand-in always says immediate_retry)"
    )
    print(f"MCP tool calls (simulated): {dict(tools)}")
    print(f"audit events written:      {len(events)}")
    print(
        f"replay (gate re-run):      {trace['reproduced']}/{trace['replayed']} decisions reproduce exactly"
    )

    if args.keep:
        print(f"\nOutputs kept in {out}  (open TRACE.html and REPORT.html there)")
    else:
        shutil.rmtree(out, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

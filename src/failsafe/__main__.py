"""
Unified command-line entry point: ``python -m failsafe <command> [args]``.

Thin dispatcher over the per-module entry points. Each command maps to one
module and every argument after the command is handed to that module's own
CLI unchanged, so ``python -m failsafe agent --inject-failure X`` behaves
exactly like ``python -m failsafe.agent --inject-failure X`` (same
healthchecks, same argparse usage errors, same exit codes).

Every module used to require ``cd src && python agent.py`` to make its
bare imports resolve; with the package layout, this dispatcher (or
``python -m failsafe.<module>``) is the documented way in.
"""

import runpy
import sys
import warnings

# command -> (module, one-line help)
COMMANDS: dict[str, tuple[str, str]] = {
    "generate-data": ("failsafe.generate_data", "Synthesize the halted-subscriptions dataset."),
    "generate-data-onetime": (
        "failsafe.generate_data_onetime",
        "Synthesize the failed one-time payments dataset.",
    ),
    "generate-checkout-abandonment": (
        "failsafe.generate_checkout_abandonment_data",
        "Synthesize the abandoned-checkouts dataset.",
    ),
    "generate-receivables": (
        "failsafe.generate_receivables_data",
        "Synthesize the overdue-invoices dataset.",
    ),
    "generate-detection-pool": (
        "failsafe.generate_detection_pool",
        "Synthesize the detection pool dataset.",
    ),
    "agent": ("failsafe.agent", "Flagship: halted subscriptions, full pipeline."),
    "agent-onetime": ("failsafe.agent_onetime", "Failed one-time payments pipeline."),
    "checkout-abandonment": (
        "failsafe.checkout_abandonment_agent",
        "Checkout abandonment pipeline.",
    ),
    "receivables": ("failsafe.receivables_agent", "Overdue receivables pipeline."),
    "integrated": ("failsafe.integrated_pipeline", "All four domains in one dispatch loop."),
    "detect-demo": ("failsafe.detection_live_demo", "Live detection-stage demo."),
    "diagnose-demo": ("failsafe.diagnosis_live_demo", "Live diagnosis-stage demo."),
    "route-demo": ("failsafe.route_demo", "Route split demo (simulated unless --live)."),
    "real-mcp-demo": ("failsafe.real_mcp_demo", "Real Razorpay MCP server demo."),
    "report": ("failsafe.generate_report", "Build REPORT.html from the flagship audit log."),
    "dashboard": (
        "failsafe.generate_policy_dashboard",
        "Build POLICY_DASHBOARD.html from the policy tables.",
    ),
    "replay": ("failsafe.replay", "Replay recorded gate decisions; --policy for a what-if."),
    "trace": ("failsafe.decision_trace", "Build TRACE.html, the per-record decision trace."),
    "console": ("failsafe.console", "Serve the read-only FailSafe console (HOST/PORT env)."),
}


def usage() -> str:
    width = max(len(c) for c in COMMANDS)
    lines = [
        "usage: python -m failsafe <command> [args...]",
        "",
        "FailSafe - gated, replayable Razorpay payment recovery.",
        "Arguments after <command> go to that module's own CLI (try <command> --help).",
        "",
        "commands:",
    ]
    lines += [f"  {c.ljust(width)}  {h}" for c, (_, h) in COMMANDS.items()]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print(usage())
        return 0 if args else 2
    cmd, rest = args[0], args[1:]
    if cmd not in COMMANDS:
        print(f"unknown command: {cmd}\n\n{usage()}", file=sys.stderr)
        return 2
    module = COMMANDS[cmd][0]
    saved = sys.argv
    sys.argv = [f"python -m failsafe {cmd}", *rest]
    try:
        with warnings.catch_warnings():
            # Harmless when the module was already imported in-process (tests).
            warnings.filterwarnings("ignore", r"'failsafe\.", RuntimeWarning)
            runpy.run_module(module, run_name="__main__", alter_sys=False)
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        print(code, file=sys.stderr)
        return 1
    finally:
        sys.argv = saved
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

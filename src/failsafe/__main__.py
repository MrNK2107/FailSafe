"""
Unified command-line entry point: ``python -m failsafe <command> [args]``.

Thin dispatcher over the existing per-module entry points - each command
maps to one pipeline stage's run/main function, preserving their CLIs
(e.g. ``agent --inject-failure ...``, ``checkout-abandonment [N]``).

Every module used to require ``cd src && python agent.py`` to make its
bare imports resolve; with the package layout, this dispatcher is the one
documented way in.
"""

import argparse
import asyncio


def _int_or_none(value: str | None) -> int | None:
    return int(value) if value is not None else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m failsafe",
        description="FailSafe - gated, audited Razorpay revenue recovery.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("generate-data", help="Synthesize the halted-subscriptions dataset.")
    p.add_argument("--force", action="store_true", help="Regenerate even if the file exists.")

    p = sub.add_parser(
        "generate-data-onetime", help="Synthesize the failed one-time payments dataset."
    )
    p = sub.add_parser(
        "generate-checkout-abandonment",
        help="Synthesize the abandoned-checkouts dataset.",
    )
    p = sub.add_parser("generate-receivables", help="Synthesize the overdue-invoices dataset.")
    p = sub.add_parser("generate-detection-pool", help="Synthesize the detection pool dataset.")

    p = sub.add_parser("agent", help="Flagship: halted subscriptions, full pipeline.")
    p.add_argument(
        "--inject-failure",
        choices=[
            "llm_parse_failure",
            "llm_invalid_action",
            "unknown_decline_code",
            "repeat_attempts",
            "diagnosis_parse_failure",
        ],
        default=None,
    )

    sub.add_parser("agent-onetime", help="Failed one-time payments pipeline.")
    p = sub.add_parser("checkout-abandonment", help="Checkout abandonment pipeline.")
    p.add_argument("n", nargs="?", type=int, default=None, help="Max records (default: all).")
    p = sub.add_parser("receivables", help="Overdue receivables pipeline.")
    p.add_argument("n", nargs="?", type=int, default=None, help="Max records (default: all).")
    p = sub.add_parser("integrated", help="All four domains in one dispatch loop.")
    p.add_argument("n", nargs="?", type=int, default=15, help="Records per domain (default: 15).")

    sub.add_parser("detect-demo", help="Live detection-stage demo.")
    sub.add_parser("diagnose-demo", help="Live diagnosis-stage demo.")
    sub.add_parser("route-demo", help="Route split demo.")
    p = sub.add_parser("real-mcp-demo", help="Real Razorpay MCP server demo.")
    p.add_argument("n", nargs="?", type=int, default=5, help="Records (default: 5).")

    sub.add_parser("report", help="Build REPORT.html from the flagship audit log.")
    sub.add_parser("dashboard", help="Build POLICY_DASHBOARD.html from the policy tables.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cmd = args.command

    if cmd == "generate-data":
        from failsafe.generate_data import main as _main

        _main(force=args.force)
    elif cmd == "generate-data-onetime":
        from failsafe.generate_data_onetime import main

        main()
    elif cmd == "generate-checkout-abandonment":
        from failsafe.generate_checkout_abandonment_data import main

        main()
    elif cmd == "generate-receivables":
        from failsafe.generate_receivables_data import main

        main()
    elif cmd == "generate-detection-pool":
        from failsafe.generate_detection_pool import main

        main()
    elif cmd == "agent":
        from failsafe.agent import run

        asyncio.run(run(inject_failure=args.inject_failure))
    elif cmd == "agent-onetime":
        from failsafe.agent_onetime import run

        asyncio.run(run())
    elif cmd == "checkout-abandonment":
        from failsafe.checkout_abandonment_agent import run

        asyncio.run(run(_int_or_none(getattr(args, "n", None))))
    elif cmd == "receivables":
        from failsafe.receivables_agent import run

        asyncio.run(run(_int_or_none(getattr(args, "n", None))))
    elif cmd == "integrated":
        from failsafe.integrated_pipeline import run

        asyncio.run(run(args.n))
    elif cmd == "detect-demo":
        from failsafe.detection_live_demo import run

        asyncio.run(run(None))
    elif cmd == "diagnose-demo":
        from failsafe.diagnosis_live_demo import run

        asyncio.run(run(30))
    elif cmd == "route-demo":
        from failsafe.route_demo import run

        asyncio.run(run())
    elif cmd == "real-mcp-demo":
        from failsafe.real_mcp_demo import run

        asyncio.run(run(args.n))
    elif cmd == "report":
        from failsafe.generate_report import build_report

        build_report()
    elif cmd == "dashboard":
        from failsafe.generate_policy_dashboard import build_dashboard

        build_dashboard()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

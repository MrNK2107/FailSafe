"""
The MCP server: wraps Razorpay actions as MCP tools. This is a real
MCPServer instance (mcp==2.x SDK) - the agent talks to it over the actual
MCP protocol (in-process transport for the main demo; see agent.py's
comment on swapping to a stdio subprocess for a "real" deployment).

Deliberately thin: the decline-code POLICY lookup (which action is even
allowed for a given decline code) lives only in gate.py, not here - these
tools never receive a decline_code, so they have no ground truth to check
that against, and neither does Razorpay's own official MCP server's
create_order/create_payment_link tools this file wraps in real-key mode.

It is NOT true, though, that no check lives here at all. The three
money-moving tools each call _enforce_tool_level_cap() first - an
independent second copy of the gate's spending-cap and per-run
duplicate-call refusal (not the policy check), enforced with its own
state, not the Gate instance's. It exists specifically so that a caller
that skips the gate entirely (a bug in a future second caller, not
anything agent.py does today) still can't overspend or double-act through
this file - see gate.py's module docstring and README.md §6 for why this
was worth adding. This state is process-global: agent.run() and agent_onetime.run() reset it
at the start of every run (reset_tool_level_guard) and then restore it from
the run's checkpoint (seed_tool_level_guard), the same way
Gate.seed_from_checkpoint restores the gate, so nothing leaks from one
in-process run into the next and a resumed run still refuses to repeat
an already-executed money-moving call.

Money-moving tools (create_payment_link, create_retry_order) route through
Razorpay's OWN OFFICIAL MCP server (razorpay_mcp_client.py -> the real
razorpay/razorpay-mcp-server, not a shim) whenever real rzp_test_ keys are
set. With no keys set, they fall back to razorpay_client.py's in-process
simulate mode - the official server has no simulate mode of its own, so
this is the only way the pipeline runs before you've created a Razorpay
account.
"""

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from failsafe import razorpay_client as _razorpay_client
from failsafe.gate import MAX_ACTION_AMOUNT_PAISE, MAX_RUN_TOTAL_PAISE
from failsafe.paths import DATA_DIR
from failsafe.razorpay_client import RazorpayClient, force_simulate
from failsafe.razorpay_mcp_client import call_official_tool

DATA_PATH = DATA_DIR / "halted_subscriptions.json"

# Route stretch goal: requires a real onboarded Linked Account, which is a
# manual Razorpay dashboard step outside this codebase's control. Defaults
# to a labeled fake ID (forces simulate mode for this tool specifically)
# unless a real one is provided.
PARTNER_LINKED_ACCOUNT_ID = os.getenv("RAZORPAY_PARTNER_LINKED_ACCOUNT_ID", "acc_sim_partner001")

server = MCPServer(name="failsafe-recovery-agent")
_rp = RazorpayClient()


@contextmanager
def simulate_mode(enabled: bool = True) -> Iterator[None]:
    """Explicitly set simulate mode for the money-moving tools, then restore it.

    A thin alias for failsafe.razorpay_client.force_simulate(): there is ONE
    dynamic switch (razorpay_client.simulate_mode()), read on every call by
    these tools, by `_rp` and by every health check, so nothing here can hold
    a stale copy. simulate_mode(False) lifts any outer force; with no real
    keys configured the tools still simulate (there is nothing real to call).
    """
    with force_simulate(enabled):
        yield


def is_simulated() -> bool:
    """Current answer of the single switch (same as razorpay_client.simulate_mode())."""
    return _razorpay_client.simulate_mode()


class ToolLevelCapExceeded(Exception):
    """Raised by a money-moving tool's own independent cap/duplicate check
    (see module docstring) - never expected in the normal pipeline, since
    agent.py always gates first and the gate's own limits are at least as
    strict as this file's, applied earlier. A real hit here means some
    caller reached this tool without going through gate.py."""


_tool_seen: set[tuple[str, str]] = set()
_tool_run_total_paise = 0


def _enforce_tool_level_cap(subscription_id: str, tool_name: str, amount_paise: int) -> None:
    global _tool_run_total_paise
    if amount_paise > MAX_ACTION_AMOUNT_PAISE:
        raise ToolLevelCapExceeded(
            f"{tool_name}: amount {amount_paise / 100:.2f} exceeds per-action cap of "
            f"{MAX_ACTION_AMOUNT_PAISE / 100:.2f} - refused independently of the gate."
        )
    if _tool_run_total_paise + amount_paise > MAX_RUN_TOTAL_PAISE:
        raise ToolLevelCapExceeded(
            f"{tool_name}: run-total spending cap would be exceeded - refused independently of the gate."
        )
    key = (subscription_id, tool_name)
    if key in _tool_seen:
        raise ToolLevelCapExceeded(
            f"{tool_name}: duplicate call for {subscription_id} in this run - "
            f"refused independently of the gate."
        )
    _tool_seen.add(key)
    _tool_run_total_paise += amount_paise


def reset_tool_level_guard() -> None:
    """Clear this module's process-global guard state. Called at the start of
    every agent run so a second run in the same process (tests, a notebook,
    a long-lived host) never inherits the previous run's spend or seen keys."""
    global _tool_run_total_paise
    _tool_seen.clear()
    _tool_run_total_paise = 0


def seed_tool_level_guard(run_total_paise: int, seen_keys: set[tuple[str, str]]) -> None:
    """Restore the guard from a resumed run's checkpoint: `run_total_paise` is
    what that run already spent, `seen_keys` the (record_id, tool_name) pairs
    it already executed. Replaces (does not add to) the current state."""
    global _tool_run_total_paise
    _tool_seen.clear()
    _tool_seen.update(seen_keys)
    _tool_run_total_paise = run_total_paise


def tool_level_guard_state() -> tuple[int, frozenset[tuple[str, str]]]:
    """Read-only snapshot (run total, seen keys) - for tests and diagnostics."""
    return _tool_run_total_paise, frozenset(_tool_seen)


def _reset_tool_level_guard_for_tests() -> None:
    """Kept for existing tests; same as reset_tool_level_guard()."""
    reset_tool_level_guard()


@server.tool()
def list_halted_subscriptions() -> list[dict]:
    """Return all synthetic halted-subscription records to evaluate."""
    if not DATA_PATH.exists():
        return []
    return json.loads(DATA_PATH.read_text(encoding="utf-8"))


@server.tool()
async def create_payment_link(subscription_id: str, amount_paise: int, description: str) -> dict:
    """Create a Razorpay payment link to nudge a customer to fix payment."""
    _enforce_tool_level_cap(subscription_id, "create_payment_link", amount_paise)
    if _razorpay_client.simulate_mode():
        return _rp.create_payment_link(amount_paise, description, subscription_id)
    result = await call_official_tool(
        "create_payment_link",
        {
            "amount": amount_paise,
            "currency": "INR",
            "description": description,
            "notes": {"subscription_id": subscription_id, "source": "failsafe-agent"},
        },
    )
    result["simulated"] = False
    result["via_official_razorpay_mcp_server"] = True
    return result


@server.tool()
async def create_retry_order(subscription_id: str, amount_paise: int) -> dict:
    """Create a Razorpay order representing a retry attempt."""
    _enforce_tool_level_cap(subscription_id, "create_retry_order", amount_paise)
    if _razorpay_client.simulate_mode():
        return _rp.create_retry_order(amount_paise, subscription_id)
    result = await call_official_tool(
        "create_order",
        {
            "amount": amount_paise,
            "currency": "INR",
            "notes": {"subscription_id": subscription_id, "source": "failsafe-agent"},
        },
    )
    result["simulated"] = False
    result["via_official_razorpay_mcp_server"] = True
    return result


@server.tool()
def initiate_route_transfer(
    subscription_id: str, amount_paise: int, partner_share_paise: int
) -> dict:
    """
    Stretch goal: split a recovered payment between the merchant and a
    second party (e.g. a referral partner) via Razorpay Route, instead of
    a single-party checkout. Not available on the official MCP server (it
    has no Route/transfer tools as of this build - checked directly against
    its tool list) so this goes through razorpay_client.py's own Route
    wrapper instead.
    """
    _enforce_tool_level_cap(subscription_id, "initiate_route_transfer", amount_paise)
    return _rp.create_route_split_order(
        amount_paise, subscription_id, PARTNER_LINKED_ACCOUNT_ID, partner_share_paise
    )


@server.tool()
def flag_for_manual_review(subscription_id: str, reason: str) -> dict:
    """Record that a subscription needs a human, not automated action (fraud/unrecoverable)."""
    return {"subscription_id": subscription_id, "flagged": True, "reason": reason}


if __name__ == "__main__":
    server.run(transport="stdio")

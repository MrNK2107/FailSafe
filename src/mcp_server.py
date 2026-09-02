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
was worth adding. Scope is deliberately narrower than Gate: this state is
per-process and never seeded from a checkpoint the way Gate.seed_from_checkpoint
is, so it catches one run reprocessing the same subscription, not a
resumed run's history - the primary gate is still what's authoritative
across a resume.

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
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from gate import MAX_ACTION_AMOUNT_PAISE, MAX_RUN_TOTAL_PAISE
from razorpay_client import RazorpayClient, SIMULATE
from razorpay_mcp_client import call_official_tool

DATA_PATH = Path(__file__).parent.parent / "data" / "halted_subscriptions.json"

# Route stretch goal: requires a real onboarded Linked Account, which is a
# manual Razorpay dashboard step outside this codebase's control. Defaults
# to a labeled fake ID (forces simulate mode for this tool specifically)
# unless a real one is provided.
PARTNER_LINKED_ACCOUNT_ID = os.getenv("RAZORPAY_PARTNER_LINKED_ACCOUNT_ID", "acc_sim_partner001")

server = MCPServer(name="razorpay-recovery-agent")
_rp = RazorpayClient()


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


def _reset_tool_level_guard_for_tests() -> None:
    """Test-only: this module's guard state is process-global by design
    (see docstring), so tests that call the tools directly, more than
    once, must reset it between cases rather than restarting the process."""
    global _tool_run_total_paise
    _tool_seen.clear()
    _tool_run_total_paise = 0


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
    if SIMULATE:
        return _rp.create_payment_link(amount_paise, description, subscription_id)
    result = await call_official_tool("create_payment_link", {
        "amount": amount_paise,
        "currency": "INR",
        "description": description,
        "notes": {"subscription_id": subscription_id, "source": "recovery-agent"},
    })
    result["simulated"] = False
    result["via_official_razorpay_mcp_server"] = True
    return result


@server.tool()
async def create_retry_order(subscription_id: str, amount_paise: int) -> dict:
    """Create a Razorpay order representing a retry attempt."""
    _enforce_tool_level_cap(subscription_id, "create_retry_order", amount_paise)
    if SIMULATE:
        return _rp.create_retry_order(amount_paise, subscription_id)
    result = await call_official_tool("create_order", {
        "amount": amount_paise,
        "currency": "INR",
        "notes": {"subscription_id": subscription_id, "source": "recovery-agent"},
    })
    result["simulated"] = False
    result["via_official_razorpay_mcp_server"] = True
    return result


@server.tool()
def initiate_route_transfer(subscription_id: str, amount_paise: int, partner_share_paise: int) -> dict:
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

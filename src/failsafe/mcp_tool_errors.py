"""
Detecting and handling an MCP tool call that failed.

When a tool raises (e.g. mcp_server's tool-level guard refuses with
ToolLevelCapExceeded, or the Razorpay call itself fails), the MCP server does
not propagate the exception: `Client.call_tool` returns a CallToolResult with
`is_error=True` and the text "Error executing tool <name>". Callers used to
store that string as `tool_result` and still mark the record executed.

`tool_call_failed()` reads the structured flag first (`is_error`, or the
wire-format spelling `isError`) and only falls back to the text prefix when
the result carries no boolean flag at all (fakes, older SDKs).
`route_tool_error_to_review()` logs an `mcp_tool_error` audit event and hands
the record to `flag_for_manual_review`, so a human sees it.
"""

from __future__ import annotations

from typing import Any

from failsafe.audit_log import AuditLogger

TOOL_ERROR_TEXT_PREFIX = "Error executing tool"
MANUAL_REVIEW_TOOL = "flag_for_manual_review"


def _text(call: Any) -> str | None:
    content = getattr(call, "content", None)
    if not content:
        return None
    return getattr(content[0], "text", None)


def tool_call_failed(call: Any) -> bool:
    """True if this CallToolResult reports a tool error."""
    for attr in ("is_error", "isError"):
        flag = getattr(call, attr, None)
        if isinstance(flag, bool):
            return flag
    text = _text(call)
    return isinstance(text, str) and text.startswith(TOOL_ERROR_TEXT_PREFIX)


def log_tool_error(
    audit: AuditLogger, *, tool: str, error: str | None, routed_to: str | None, **id_kwargs: Any
) -> None:
    audit.log("mcp_tool_error", tool=tool, error=error, routed_to=routed_to, **id_kwargs)


async def route_tool_error_to_review(
    client: Any,
    audit: AuditLogger,
    *,
    tool: str,
    error: str | None,
    record_id: str,
    id_kwargs: dict,
) -> str:
    """Audit a failed money-moving tool call and flag the record for a human.

    Returns the manual-review reason. The flag call's own failure is audited
    too (it has nowhere further to go)."""
    reason = (
        f"MCP tool {tool} failed ({error or 'no error text'}) - the gated action was "
        f"NOT executed; needs human review."
    )
    log_tool_error(audit, tool=tool, error=error, routed_to=MANUAL_REVIEW_TOOL, **id_kwargs)
    call = await client.call_tool(
        MANUAL_REVIEW_TOOL, {"subscription_id": record_id, "reason": reason}
    )
    flag_text = _text(call)
    audit.log("mcp_tool_call", tool=MANUAL_REVIEW_TOOL, result=flag_text, **id_kwargs)
    if tool_call_failed(call):
        log_tool_error(audit, tool=MANUAL_REVIEW_TOOL, error=flag_text, routed_to=None, **id_kwargs)
    return reason


def render_tool_error_lines(results: list[dict]) -> list[str]:
    """RESULTS*.md bullet for records whose MCP tool call errored. Empty when
    there were none, so runs without tool errors render exactly as before."""
    n = sum(1 for r in results if r.get("tool_error"))
    if not n:
        return []
    return [f"- MCP tool errors (action NOT executed, routed to manual review): **{n}**"]

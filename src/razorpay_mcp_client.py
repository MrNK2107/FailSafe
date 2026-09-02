"""
Bridges to Razorpay's OWN OFFICIAL MCP server
(github.com/razorpay/razorpay-mcp-server, docker image mcp/razorpay) for
the two money-moving tools this project needs - create_order and
create_payment_link - instead of hand-rolling those calls through the raw
SDK ourselves.

Only active when real rzp_test_ keys are set (see razorpay_client.py).
There's no simulate-mode equivalent here: the official server has no
simulate mode of its own, and calling it with fake keys fails at the
individual tool-call step (confirmed by hand: `tools/list` succeeds with
any key shape, since listing tools doesn't touch the Razorpay API, but an
actual create_order/create_payment_link call needs real auth).

Each call opens a fresh `docker run` subprocess over stdio, does one tool
call, and tears down. Simpler and more robust than keeping one long-lived
subprocess alive across an async batch run - the ~1-2s startup overhead
per call is acceptable here because this path only activates once you add
real keys; it is not the default demo path (see mcp_server.py, which falls
back to razorpay_client.py's in-process simulate mode otherwise).
"""

import json
import os

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from razorpay_client import KEY_ID, KEY_SECRET, SIMULATE

DOCKER_IMAGE = "mcp/razorpay"


def _server_params() -> StdioServerParameters:
    return StdioServerParameters(
        command="docker",
        args=["run", "--rm", "-i", "-e", "RAZORPAY_KEY_ID", "-e", "RAZORPAY_KEY_SECRET", DOCKER_IMAGE],
        env={**os.environ, "RAZORPAY_KEY_ID": KEY_ID, "RAZORPAY_KEY_SECRET": KEY_SECRET},
    )


async def call_official_tool(tool_name: str, arguments: dict) -> dict:
    if SIMULATE:
        raise RuntimeError(
            "call_official_tool() requires real rzp_test_ keys - the official "
            "Razorpay MCP server has no simulate mode. Set RAZORPAY_KEY_ID/"
            "RAZORPAY_KEY_SECRET in .env first (free, no KYC, from "
            "dashboard.razorpay.com)."
        )
    async with Client(_server_params()) as client:
        result = await client.call_tool(tool_name, arguments)
        text = result.content[0].text if result.content else "{}"
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"raw": text}

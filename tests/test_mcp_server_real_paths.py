"""
mcp_server.py's non-SIMULATE branches (real keys -> official Razorpay MCP
server) and Route transfer, with call_official_tool stubbed so no network
call is made. The safety property under test: the tool-level cap/duplicate
guard runs BEFORE any real money-moving call is forwarded, and the payload
forwarded to the official server carries the right amount/currency/notes.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from failsafe import mcp_server
from failsafe.gate import MAX_ACTION_AMOUNT_PAISE


@pytest.fixture
def real_mode():
    """Pretend real rzp_test_ keys are configured; the official MCP call is a stub."""
    official = AsyncMock(return_value={"id": "plink_1"})
    with (
        patch("failsafe.razorpay_client.keys_configured", return_value=True),
        mcp_server.simulate_mode(False),
        patch.object(mcp_server, "call_official_tool", official),
    ):
        yield official


def test_payment_link_forwards_to_official_server_with_notes(real_mode):
    out = asyncio.run(mcp_server.create_payment_link("sub_r1", 29900, "fix your card"))
    real_mode.assert_awaited_once_with(
        "create_payment_link",
        {
            "amount": 29900,
            "currency": "INR",
            "description": "fix your card",
            "notes": {"subscription_id": "sub_r1", "source": "failsafe-agent"},
        },
    )
    assert out == {"id": "plink_1", "simulated": False, "via_official_razorpay_mcp_server": True}


def test_retry_order_forwards_create_order(real_mode):
    out = asyncio.run(mcp_server.create_retry_order("sub_r2", 19900))
    name, payload = real_mode.await_args.args
    assert name == "create_order"
    assert payload["amount"] == 19900 and payload["notes"]["subscription_id"] == "sub_r2"
    assert payload["notes"]["source"] == "failsafe-agent"
    assert out["simulated"] is False


@pytest.mark.parametrize(
    "call",
    [
        lambda: mcp_server.create_payment_link("sub_big", MAX_ACTION_AMOUNT_PAISE + 1, "x"),
        lambda: mcp_server.create_retry_order("sub_big", MAX_ACTION_AMOUNT_PAISE + 1),
    ],
)
def test_over_cap_is_refused_before_any_real_call(real_mode, call):
    with pytest.raises(mcp_server.ToolLevelCapExceeded):
        asyncio.run(call())
    real_mode.assert_not_awaited()


def test_duplicate_real_call_is_refused_before_second_forward(real_mode):
    asyncio.run(mcp_server.create_retry_order("sub_dup", 100))
    with pytest.raises(mcp_server.ToolLevelCapExceeded, match="duplicate"):
        asyncio.run(mcp_server.create_retry_order("sub_dup", 100))
    assert real_mode.await_count == 1


def test_route_transfer_uses_local_client_and_guard():
    split = MagicMock(return_value={"id": "order_route"})
    with patch.object(mcp_server._rp, "create_route_split_order", split):
        out = mcp_server.initiate_route_transfer("sub_route", 10000, 2000)
        assert out == {"id": "order_route"}
        split.assert_called_once_with(
            10000, "sub_route", mcp_server.PARTNER_LINKED_ACCOUNT_ID, 2000
        )
        with pytest.raises(mcp_server.ToolLevelCapExceeded):
            mcp_server.initiate_route_transfer("sub_route", 10000, 2000)
    assert split.call_count == 1


def test_list_halted_subscriptions_handles_missing_file(tmp_path):
    with patch.object(mcp_server, "DATA_PATH", tmp_path / "nope.json"):
        assert mcp_server.list_halted_subscriptions() == []
    data = tmp_path / "subs.json"
    data.write_text('[{"subscription_id": "s"}]', encoding="utf-8")
    with patch.object(mcp_server, "DATA_PATH", data):
        assert mcp_server.list_halted_subscriptions() == [{"subscription_id": "s"}]


def test_flag_for_manual_review_moves_no_money():
    out = mcp_server.flag_for_manual_review("sub_f", "fraud")
    assert out == {"subscription_id": "sub_f", "flagged": True, "reason": "fraud"}
    assert mcp_server._tool_run_total_paise == 0


def test_server_carries_the_failsafe_brand_name():
    assert mcp_server.server.name == "failsafe-recovery-agent"

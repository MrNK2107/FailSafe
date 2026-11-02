import importlib
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _reload_razorpay_client(monkeypatch, key_id, key_secret):
    monkeypatch.setenv("RAZORPAY_KEY_ID", key_id)
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", key_secret)
    from failsafe import razorpay_client

    importlib.reload(razorpay_client)
    return razorpay_client


def test_simulate_true_when_no_keys(monkeypatch):
    rc = _reload_razorpay_client(monkeypatch, "", "")
    assert rc.simulate_mode() is True and rc.keys_configured() is False
    client = rc.RazorpayClient()
    assert client.simulate is True
    result = client.create_payment_link(10000, "desc", "sub_1")
    assert result["simulated"] is True
    assert "plink_sim" in result["id"]


def test_simulate_false_with_test_keys(monkeypatch):
    rc = _reload_razorpay_client(monkeypatch, "rzp_test_abc", "secret123")
    assert rc.simulate_mode() is False and rc.keys_configured() is True
    client = rc.RazorpayClient()
    assert client.simulate is False
    fake_client = MagicMock()
    fake_client.payment_link.create.return_value = {
        "id": "plink_real",
        "short_url": "https://rzp.io/i/real",
    }
    client._sdk = fake_client
    result = client.create_payment_link(10000, "desc", "sub_1")
    assert result["simulated"] is False
    assert result["id"] == "plink_real"


def test_live_key_refused(monkeypatch):
    rc = _reload_razorpay_client(monkeypatch, "rzp_live_abc", "secret")
    with pytest.raises(RuntimeError, match="live-mode"):
        rc.RazorpayClient()


def test_route_split_value_error(monkeypatch):
    rc = _reload_razorpay_client(monkeypatch, "", "")
    client = rc.RazorpayClient()
    with pytest.raises(ValueError, match="partner_share_paise cannot exceed"):
        client.create_route_split_order(10000, "sub_1", "acc_1", 20000)


def test_route_split_simulate(monkeypatch):
    rc = _reload_razorpay_client(monkeypatch, "", "")
    client = rc.RazorpayClient()
    result = client.create_route_split_order(10000, "sub_1", "acc_1", 5000)
    assert result["simulated"] is True
    assert result["transfers"][0]["amount"] == 5000


def test_create_retry_order_simulate(monkeypatch):
    rc = _reload_razorpay_client(monkeypatch, "", "")
    client = rc.RazorpayClient()
    result = client.create_retry_order(5000, "sub_1")
    assert result["simulated"] is True
    assert "order_sim" in result["id"]


def test_create_retry_order_real(monkeypatch):
    rc = _reload_razorpay_client(monkeypatch, "rzp_test_abc", "secret123")
    fake_client = MagicMock()
    fake_client.order.create.return_value = {"id": "order_real", "amount": 5000}
    client = rc.RazorpayClient()
    client._sdk = fake_client
    result = client.create_retry_order(5000, "sub_1")
    assert result["simulated"] is False
    assert result["id"] == "order_real"


def test_razorpay_errors_propagate_or_handled(monkeypatch):
    rc = _reload_razorpay_client(monkeypatch, "rzp_test_abc", "secret123")
    fake_client = MagicMock()
    fake_client.payment_link.create.side_effect = Exception("razorpay error: auth failed")
    client = rc.RazorpayClient()
    client._sdk = fake_client
    with pytest.raises(Exception, match="razorpay error"):
        client.create_payment_link(10000, "desc", "sub_1")


@pytest.mark.asyncio
async def test_call_official_tool_simulate_raises(monkeypatch):
    from failsafe import razorpay_mcp_client

    monkeypatch.setattr(razorpay_mcp_client, "simulate_mode", lambda: True)
    with pytest.raises(RuntimeError, match="requires real rzp_test_"):
        await razorpay_mcp_client.call_official_tool("create_order", {"amount": 100})


@pytest.mark.asyncio
async def test_call_official_tool_success(monkeypatch):
    from failsafe import razorpay_mcp_client

    monkeypatch.setattr(razorpay_mcp_client, "simulate_mode", lambda: False)
    mock_client_instance = AsyncMock()
    mock_result = MagicMock()
    mock_result.content = [MagicMock(text=json.dumps({"id": "order_123"}))]
    mock_client_instance.call_tool.return_value = mock_result
    mock_cm = AsyncMock()
    mock_cm.__aenter__.return_value = mock_client_instance
    mock_cm.__aexit__.return_value = False
    with patch.object(razorpay_mcp_client, "Client", return_value=mock_cm):
        result = await razorpay_mcp_client.call_official_tool("create_order", {"amount": 100})
    assert result["id"] == "order_123"


@pytest.mark.asyncio
async def test_call_official_tool_invalid_json_returns_raw(monkeypatch):
    from failsafe import razorpay_mcp_client

    monkeypatch.setattr(razorpay_mcp_client, "simulate_mode", lambda: False)
    mock_client_instance = AsyncMock()
    mock_result = MagicMock()
    mock_result.content = [MagicMock(text="not json at all")]
    mock_client_instance.call_tool.return_value = mock_result
    mock_cm = AsyncMock()
    mock_cm.__aenter__.return_value = mock_client_instance
    mock_cm.__aexit__.return_value = False
    with patch.object(razorpay_mcp_client, "Client", return_value=mock_cm):
        result = await razorpay_mcp_client.call_official_tool("create_order", {"amount": 100})
    assert "raw" in result
    assert result["raw"] == "not json at all"


@pytest.mark.asyncio
async def test_call_official_tool_empty_content(monkeypatch):
    from failsafe import razorpay_mcp_client

    monkeypatch.setattr(razorpay_mcp_client, "simulate_mode", lambda: False)
    mock_client_instance = AsyncMock()
    mock_result = MagicMock()
    mock_result.content = []
    mock_client_instance.call_tool.return_value = mock_result
    mock_cm = AsyncMock()
    mock_cm.__aenter__.return_value = mock_client_instance
    mock_cm.__aexit__.return_value = False
    with patch.object(razorpay_mcp_client, "Client", return_value=mock_cm):
        result = await razorpay_mcp_client.call_official_tool("create_order", {"amount": 100})
    assert result == {}


def test_route_split_real_sends_transfer_to_linked_account(monkeypatch):
    rc = _reload_razorpay_client(monkeypatch, "rzp_test_abc", "secret123")
    fake_client = MagicMock()
    fake_client.order.create.return_value = {"id": "order_route_real", "amount": 10000}
    client = rc.RazorpayClient()
    client._sdk = fake_client
    result = client.create_route_split_order(10000, "sub_9", "acc_partner", 500)
    assert result == {"id": "order_route_real", "amount": 10000, "simulated": False}
    payload = fake_client.order.create.call_args.args[0]
    assert payload["amount"] == 10000 and payload["receipt"] == "route_sub_9"
    assert payload["transfers"] == [
        {"account": "acc_partner", "amount": 500, "currency": "INR", "on_hold": False}
    ]


@pytest.mark.parametrize(
    ("key_id", "key_secret"),
    [
        ("rzp_test_xxxxxxxxxxxxxx", "xxxxxxxxxxxxxxxxxxxxxxxx"),  # verbatim .env.example
        ("rzp_test_xxxxxxxxxxxxxx", "a_real_looking_secret"),
        ("rzp_test_RealLookingId1", "xxxxxxxxxxxxxxxxxxxxxxxx"),
        ("rzp_test_XXXXXXXXXXXXXX", "XXXXXXXXXXXXXXXX"),
    ],
)
def test_placeholder_keys_count_as_unset(monkeypatch, key_id, key_secret):
    """`cp .env.example .env` must leave SIMULATE on, not send fake credentials."""
    rc = _reload_razorpay_client(monkeypatch, key_id, key_secret)
    assert rc.keys_configured() is False
    assert rc.simulate_mode() is True
    assert rc.RazorpayClient().create_retry_order(100, "sub_p")["simulated"] is True


def test_env_example_ships_no_active_placeholder_keys():
    from pathlib import Path

    example = Path(__file__).resolve().parent.parent / ".env.example"
    active = [
        line
        for line in example.read_text(encoding="utf-8").splitlines()
        if line.startswith(("RAZORPAY_KEY_ID=", "RAZORPAY_KEY_SECRET="))
    ]
    assert active == []


def test_sdk_is_built_lazily_and_notes_carry_failsafe_brand(monkeypatch):
    rc = _reload_razorpay_client(monkeypatch, "rzp_test_abc", "secret123")
    built = MagicMock()
    with patch.object(rc.razorpay, "Client", return_value=built) as ctor:
        client = rc.RazorpayClient()
        ctor.assert_not_called()  # importing/constructing never builds the SDK
        built.order.create.return_value = {"id": "order_x"}
        built.payment_link.create.return_value = {"id": "plink_x"}
        client.create_retry_order(100, "sub_b")
        client.create_payment_link(100, "d", "sub_b")
        client.create_route_split_order(100, "sub_b", "acc_1", 10)
        ctor.assert_called_once_with(auth=("rzp_test_abc", "secret123"))
    assert built.order.create.call_args_list[0].args[0]["notes"]["source"] == "failsafe-agent"
    assert built.payment_link.create.call_args.args[0]["notes"]["source"] == "failsafe-agent"
    route_notes = built.order.create.call_args_list[1].args[0]["notes"]
    assert route_notes["source"] == "failsafe-agent-route-demo"


def test_force_simulate_overrides_real_keys(monkeypatch):
    rc = _reload_razorpay_client(monkeypatch, "rzp_test_abc", "secret123")
    with rc.force_simulate():
        assert rc.simulate_mode() is True
        assert rc.RazorpayClient().create_retry_order(100, "s")["simulated"] is True
    assert rc.simulate_mode() is False

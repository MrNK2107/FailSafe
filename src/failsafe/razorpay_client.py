"""
Thin wrapper over the official Razorpay Python SDK. Test-mode only - this
codebase has no code path that can touch a live key.

If RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET aren't set, everything runs in
SIMULATE mode: same interface, fake-but-realistic responses, so the whole
pipeline is runnable before you've created a Razorpay account. Swap in real
rzp_test_ keys (free, no KYC) any time and nothing else changes.

Design note (WP3 import-hygiene overhaul): this module is deliberately
import-safe. Reading .env and the key strings at import is fine (values
only, no connections), but the SDK client is constructed LAZILY on first
real use - importing this module never touches the network or builds a
razorpay.Client. There is exactly ONE simulate knob: simulate_mode(),
which is dynamic (call it any time - it always reflects current state).
Demo scripts and tests force it with force_simulate(); the old pattern of
patching a module-level SIMULATE flag AND a per-instance simulate
attribute (the "two-patch dance") is gone because there are no shadow
copies left to drift apart.
"""

import os
import uuid
from contextlib import contextmanager

import razorpay
from dotenv import load_dotenv

load_dotenv()

KEY_ID = os.getenv("RAZORPAY_KEY_ID", "")
KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")

# Forced-simulate override. A list so force_simulate() can mutate it without
# any consumer ever holding a stale copy of the value.
_forced = [False]


def keys_configured() -> bool:
    """True when real rzp_test_ keys are present in the environment."""
    return KEY_ID.startswith("rzp_test_") and bool(KEY_SECRET)


def simulate_mode() -> bool:
    """The single source of truth for whether Razorpay calls are simulated.

    Dynamic by design: demo scripts can flip it on for one run via
    force_simulate() and every caller that asks afterwards sees the same
    answer, unlike the old import-time snapshot copies.
    """
    return _forced[0] or not keys_configured()


@contextmanager
def force_simulate():
    """Force simulate mode inside the block, regardless of local .env.

    Used by demo scripts that exist to measure diagnosis/detection behavior
    (not to create real Razorpay objects) and by tests that must never make
    a live call as a side effect. Always restore on exit, even on error.
    """
    _forced[0] = True
    try:
        yield
    finally:
        _forced[0] = False


class RazorpayClient:
    """Test-mode-only client. Raises if anyone ever points it at a live key."""

    def __init__(self):
        if KEY_ID.startswith("rzp_live_"):
            raise RuntimeError(
                "Refusing to run: a live-mode key was detected. This project "
                "is test-mode only, always."
            )
        self._sdk = None  # constructed lazily; never at import time

    @property
    def simulate(self) -> bool:
        """Reads the one shared knob - always current, never a stale copy."""
        return simulate_mode()

    @property
    def sdk(self):
        """The real SDK client, built on first use (never at import)."""
        if self._sdk is None:
            self._sdk = razorpay.Client(auth=(KEY_ID, KEY_SECRET))
        return self._sdk

    def create_payment_link(
        self, amount_paise: int, description: str, subscription_id: str
    ) -> dict:
        if self.simulate:
            return {
                "id": f"plink_sim_{uuid.uuid4().hex[:14]}",
                "short_url": f"https://rzp.io/i/sim_{uuid.uuid4().hex[:8]}",
                "amount": amount_paise,
                "status": "created",
                "simulated": True,
                "subscription_id": subscription_id,
            }
        link = self.sdk.payment_link.create(
            {
                "amount": amount_paise,
                "currency": "INR",
                "description": description,
                "notes": {"subscription_id": subscription_id, "source": "failsafe-agent"},
            }
        )
        link["simulated"] = False
        return link

    def create_route_split_order(
        self,
        amount_paise: int,
        subscription_id: str,
        partner_linked_account_id: str,
        partner_share_paise: int,
    ) -> dict:
        """
        Stretch goal: two-sided commerce via Route. Creates an order for a
        recovered payment where a slice of it is transferred to a second
        party (e.g. a referral partner's Linked Account) instead of all of
        it settling to the merchant alone.

        Requires a real Linked Account ID in live/test mode - Route accounts
        are onboarded via the Razorpay dashboard, which is a manual step
        outside this codebase's control, so this runs in simulate mode by
        default (see mcp_server.py) unless RAZORPAY_PARTNER_LINKED_ACCOUNT_ID
        is actually set to a real onboarded account.
        """
        if partner_share_paise > amount_paise:
            raise ValueError("partner_share_paise cannot exceed amount_paise")

        if self.simulate:
            return {
                "id": f"order_sim_{uuid.uuid4().hex[:14]}",
                "amount": amount_paise,
                "status": "created",
                "simulated": True,
                "subscription_id": subscription_id,
                "transfers": [
                    {
                        "account": partner_linked_account_id,
                        "amount": partner_share_paise,
                        "currency": "INR",
                        "simulated": True,
                    }
                ],
            }
        order = self.sdk.order.create(
            {
                "amount": amount_paise,
                "currency": "INR",
                "receipt": f"route_{subscription_id}",
                "notes": {
                    "subscription_id": subscription_id,
                    "source": "failsafe-agent-route-demo",
                },
                "transfers": [
                    {
                        "account": partner_linked_account_id,
                        "amount": partner_share_paise,
                        "currency": "INR",
                        "on_hold": False,
                    }
                ],
            }
        )
        order["simulated"] = False
        return order

    def create_retry_order(self, amount_paise: int, subscription_id: str) -> dict:
        """Represents an immediate/delayed retry attempt as a fresh test-mode Order."""
        if self.simulate:
            return {
                "id": f"order_sim_{uuid.uuid4().hex[:14]}",
                "amount": amount_paise,
                "status": "created",
                "simulated": True,
                "subscription_id": subscription_id,
            }
        order = self.sdk.order.create(
            {
                "amount": amount_paise,
                "currency": "INR",
                "receipt": f"retry_{subscription_id}",
                "notes": {"subscription_id": subscription_id, "source": "failsafe-agent"},
            }
        )
        order["simulated"] = False
        return order

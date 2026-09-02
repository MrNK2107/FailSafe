"""
Thin wrapper over the official Razorpay Python SDK. Test-mode only - this
codebase has no code path that can touch a live key.

If RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET aren't set, everything runs in
SIMULATE mode: same interface, fake-but-realistic responses, so the whole
pipeline is runnable before you've created a Razorpay account. Swap in real
rzp_test_ keys (free, no KYC) any time and nothing else changes.
"""

import os
import uuid

import razorpay
from dotenv import load_dotenv

load_dotenv()

KEY_ID = os.getenv("RAZORPAY_KEY_ID", "")
KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")
SIMULATE = not (KEY_ID.startswith("rzp_test_") and KEY_SECRET)

if not SIMULATE:
    _client = razorpay.Client(auth=(KEY_ID, KEY_SECRET))
else:
    _client = None


class RazorpayClient:
    """Test-mode-only client. Raises if anyone ever points it at a live key."""

    def __init__(self):
        if KEY_ID.startswith("rzp_live_"):
            raise RuntimeError(
                "Refusing to run: a live-mode key was detected. This project "
                "is test-mode only, always."
            )
        self.simulate = SIMULATE

    def create_payment_link(self, amount_paise: int, description: str, subscription_id: str) -> dict:
        if self.simulate:
            return {
                "id": f"plink_sim_{uuid.uuid4().hex[:14]}",
                "short_url": f"https://rzp.io/i/sim_{uuid.uuid4().hex[:8]}",
                "amount": amount_paise,
                "status": "created",
                "simulated": True,
                "subscription_id": subscription_id,
            }
        link = _client.payment_link.create({
            "amount": amount_paise,
            "currency": "INR",
            "description": description,
            "notes": {"subscription_id": subscription_id, "source": "recovery-agent"},
        })
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
                "transfers": [{
                    "account": partner_linked_account_id,
                    "amount": partner_share_paise,
                    "currency": "INR",
                    "simulated": True,
                }],
            }
        order = _client.order.create({
            "amount": amount_paise,
            "currency": "INR",
            "receipt": f"route_{subscription_id}",
            "notes": {"subscription_id": subscription_id, "source": "recovery-agent-route-demo"},
            "transfers": [{
                "account": partner_linked_account_id,
                "amount": partner_share_paise,
                "currency": "INR",
                "on_hold": False,
            }],
        })
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
        order = _client.order.create({
            "amount": amount_paise,
            "currency": "INR",
            "receipt": f"retry_{subscription_id}",
            "notes": {"subscription_id": subscription_id, "source": "recovery-agent"},
        })
        order["simulated"] = False
        return order

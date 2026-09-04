"""
Synthetic failed-one-time-payment dataset generator - the stretch-goal
sibling of generate_data.py, proving the gate/policy/audit-log pattern
isn't subscription-specific.

Deliberately different from a subscription record, not just a renamed
copy, because the underlying Razorpay behavior is genuinely different:
  - A subscription gets Razorpay's own automatic 3-day/3-attempt retry
    cycle before it ever reaches this agent (BUILD_LOG.md §1). A one-time
    payment has no such cycle - it fails once, at checkout, and Razorpay
    does nothing further automatically. So there's no `halted_days_ago`
    or `previous_retry_count` here; this agent is the FIRST thing to see
    the failure, not the last resort after Razorpay gives up.
  - No time-decay on recoverability either, for the same reason - decay
    in generate_data.py models a subscription going stale while sitting
    halted for days; a one-time payment failure has no "days sitting
    halted" to decay over.

Same decline-code taxonomy and DECLINE_CODES policy table as subscriptions
(config/decline_policy.json) - real Razorpay decline codes don't care
whether the payment was recurring or one-off. The weighting below is a
plausible checkout-failure mix, not a measured one - said explicitly, same
honesty standard as generate_data.py.
"""

import json
import random
import uuid
from pathlib import Path

from decline_codes import DECLINE_CODES

OUTPUT_PATH = Path(__file__).parent.parent / "data" / "failed_onetime_payments.json"

# A one-off checkout skews differently from recurring billing: less
# "insufficient funds on a recurring date" (that's a subscription pattern),
# more input-entry and authentication mistakes typical of a first-time
# card entry at checkout.
CODE_WEIGHTS = {
    "card_declined": 20,
    "authentication_failed": 16,
    "incorrect_cvv": 14,
    "insufficient_funds": 12,
    "card_expired": 10,
    "payment_timed_out": 8,
    "gateway_technical_error": 6,
    "bank_technical_error": 5,
    "card_not_enrolled": 3,
    "transaction_limit_exceeded": 2,
    "payment_cancelled": 2,
    "payment_risk_check_failed": 2,  # fraud - small but non-zero, deliberately
}

ITEMS = [
    ("Wireless Headphones", 249900),
    ("Standing Desk", 1499900),
    ("Running Shoes", 449900),
    ("Espresso Machine", 899900),
    ("Backpack", 189900),
    ("Mechanical Keyboard", 649900),
]

NUM_MERCHANTS = 8


def generate(n: int = 30, seed: int = 7) -> list[dict]:
    rng = random.Random(seed)
    codes = list(CODE_WEIGHTS.keys())
    weights = list(CODE_WEIGHTS.values())

    records = []
    for _ in range(n):
        code = rng.choices(codes, weights=weights, k=1)[0]
        policy = DECLINE_CODES[code]
        merchant_num = rng.randint(1, NUM_MERCHANTS)
        item_name, base_amount = rng.choice(ITEMS)
        # +/- 15% jitter (wider than subscriptions - one-off purchase
        # amounts vary more than fixed recurring plan prices)
        amount_paise = int(base_amount * rng.uniform(0.85, 1.15))

        simulated_customer_response = (
            rng.random() < policy.simulated_success_rate
            if policy.simulated_success_rate > 0
            else False
        )

        records.append({
            "payment_id": f"pay_{uuid.uuid4().hex[:14]}",
            "merchant_id": f"merchant_{merchant_num:03d}",
            "customer_id": f"cust_{uuid.uuid4().hex[:10]}",
            "item": item_name,
            "amount_paise": amount_paise,
            "currency": "INR",
            "decline_code": code,
            "simulated_customer_response": simulated_customer_response,
        })
    return records


def main():
    records = generate()
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(records, indent=2), encoding="utf-8")
    print(f"Wrote {len(records)} synthetic records to {OUTPUT_PATH}")

    fraud = sum(1 for r in records if r["decline_code"] == "payment_risk_check_failed")
    unrecoverable = sum(
        1 for r in records
        if DECLINE_CODES[r["decline_code"]].allowed_action.value == "no_action_unrecoverable"
    )
    print(f"  fraud-flagged: {fraud}, unrecoverable: {unrecoverable}, total: {len(records)}")


if __name__ == "__main__":
    main()

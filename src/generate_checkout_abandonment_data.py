"""
Synthetic checkout-abandonment dataset generator - the CHECKOUT ABANDONMENT
sibling of generate_data.py (halted subscriptions) and
generate_data_onetime.py (failed one-time payments), closing one of the
two previously-undisclosed category-scope gaps
(PS_REQUIREMENTS_DEBATE.md; README.md §6's own disclosure of it).

Structurally different from both siblings by definition, not by choice: a
checkout-abandonment record has no decline_code and no
previous_retry_count, because no payment was ever attempted or declined -
the customer left before ever submitting one. Schema-accurate to a
plausible real checkout funnel (a session id, which STAGE the customer
reached, how long ago they left, device, returning-customer status), but,
like every other synthetic dataset in this project, fabricated - no real
customer data anywhere here.

`abandonment_reason` is generated as ground truth here (kept deliberately
- exactly like generate_data.py keeps decline_code, and
generate_detection_pool.py keeps ground_truth_needs_attention - the only
way to ever measure diagnosis accuracy honestly). It is NEVER exposed to
diagnose_checkout_abandonment.diagnose_abandonment_reason(), which only
ever sees checkout_stage/minutes_since_abandonment/device_type/
is_returning_customer/amount_paise - checkout_abandonment_agent.py reads
it ONLY to score diagnosis_matched_ground_truth in the audit log.

Two DELIBERATE ambiguity clusters, mirroring generate_data.py's own
raw-text-sharing clusters and generate_detection_pool.py's shared
pending/retry-1 slice - real, structural ambiguity in the signal space
the diagnosis stage actually sees, not an artificial difficulty knob:

  - Cluster A: `checkout_stage="otp_entry"` with `minutes_since_abandonment`
    in the 8-15 minute band is shared by BOTH `otp_delay_or_failure` (a
    genuine OTP/SMS delivery delay of that length is realistic) AND
    `distraction_or_multitasking` (started OTP entry, then got pulled
    away and never came back) - nothing in stage+timing alone
    distinguishes them.
  - Cluster B: `checkout_stage="card_details_entry"` with
    `is_returning_customer=False` is shared by BOTH
    `trust_or_security_concern` (a nervous new customer) AND
    `payment_method_unsupported` (a new customer whose only card type
    turns out not to be accepted) - nothing in stage+returning-customer
    alone distinguishes them either.

test_generate_checkout_abandonment_data.py asserts both ground truths
genuinely appear inside each shared signal band, or the test fails - this
is a structural guarantee, not a hope.
"""

import json
import random
import uuid
from pathlib import Path

from checkout_abandonment_policy import ABANDONMENT_POLICIES

OUTPUT_PATH = Path(__file__).parent.parent / "data" / "abandoned_checkouts.json"

CHECKOUT_STAGES = [
    "payment_method_selection",
    "card_details_entry",
    "otp_entry",
    "review_confirm",
]
DEVICE_TYPES = ["mobile_web", "desktop_web", "android_app", "ios_app"]

# Weighted toward the reasons that plausibly dominate real checkout
# abandonment (distraction/multitasking and OTP friction are common;
# outright security refusal is the rarest), not an even split.
REASON_WEIGHTS = {
    "otp_delay_or_failure": 22,
    "payment_method_unsupported": 18,
    "price_shock": 20,
    "distraction_or_multitasking": 25,
    "trust_or_security_concern": 15,
}

# (item name, base amount in paise). One deliberately low-value item
# (below abandonment_gate.MIN_CART_VALUE_FOR_ACTION_PAISE = Rs 149) so the
# low-value stopping rule has something real to fire on in a live run,
# not just in a unit test.
ITEMS = [
    ("Streaming Monthly Plan", 29900),
    ("Wireless Earbuds", 249900),
    ("Desk Lamp", 89900),
    ("Digital Sticker Pack", 9900),
    ("Grocery Delivery Bag", 59900),
    ("Ebook Bundle", 129900),
    ("Phone Case", 69900),
]

NUM_MERCHANTS = 10


def _pick_device(rng: random.Random, new_customer_leaning: bool = False) -> str:
    if new_customer_leaning:
        # Trust/security concerns skew toward mobile web - an unfamiliar
        # checkout on a small screen is a plausible real amplifier of
        # hesitation, not an arbitrary tie-breaker (this is flavor, not a
        # disambiguating signal the diagnosis stage is told to rely on).
        return rng.choices(DEVICE_TYPES, weights=[45, 20, 20, 15], k=1)[0]
    return rng.choice(DEVICE_TYPES)


def generate(n: int = 150, seed: int = 7) -> list[dict]:
    rng = random.Random(seed)
    reasons = list(REASON_WEIGHTS.keys())
    weights = list(REASON_WEIGHTS.values())

    merchant_items = {i: rng.choice(ITEMS) for i in range(1, NUM_MERCHANTS + 1)}

    records = []
    for _ in range(n):
        reason = rng.choices(reasons, weights=weights, k=1)[0]
        merchant_num = rng.randint(1, NUM_MERCHANTS)
        item_name, base_amount = merchant_items[merchant_num]
        amount_paise = int(base_amount * rng.uniform(0.85, 1.15))

        if reason == "otp_delay_or_failure":
            checkout_stage = "otp_entry"
            # 60% land in the deliberately-ambiguous 8-15 minute band
            # shared with distraction_or_multitasking; 40% are a clearly
            # fresh, unambiguous technical hiccup.
            if rng.random() < 0.6:
                minutes_since_abandonment = rng.randint(8, 15)
            else:
                minutes_since_abandonment = rng.randint(1, 5)
            is_returning_customer = rng.random() < 0.6
            device_type = _pick_device(rng)

        elif reason == "distraction_or_multitasking":
            # 30% land in the SAME otp_entry/8-15-minute band as
            # otp_delay_or_failure above - the genuine ambiguity cluster.
            # The remaining 70% span other stages with a clearly LONG gap,
            # the reason's own unambiguous signature.
            if rng.random() < 0.3:
                checkout_stage = "otp_entry"
                minutes_since_abandonment = rng.randint(8, 15)
            else:
                checkout_stage = rng.choice(CHECKOUT_STAGES)
                minutes_since_abandonment = rng.randint(30, 180)
            is_returning_customer = rng.random() < 0.5
            device_type = _pick_device(rng)

        elif reason == "payment_method_unsupported":
            checkout_stage = rng.choice(["payment_method_selection", "card_details_entry"])
            minutes_since_abandonment = rng.randint(1, 10)
            # Half of the card_details_entry slice is also a new customer -
            # the SAME (stage, new-customer) combination
            # trust_or_security_concern uses below - the second ambiguity
            # cluster.
            if checkout_stage == "card_details_entry" and rng.random() < 0.5:
                is_returning_customer = False
            else:
                is_returning_customer = rng.random() < 0.5
            device_type = _pick_device(rng)

        elif reason == "price_shock":
            checkout_stage = "review_confirm"
            minutes_since_abandonment = rng.randint(1, 12)
            is_returning_customer = rng.random() < 0.5
            device_type = _pick_device(rng)
            # Price shock correlates with a genuinely larger cart - jitter
            # upward rather than the default +/-15% band.
            amount_paise = int(base_amount * rng.uniform(1.1, 1.6))

        else:  # trust_or_security_concern
            checkout_stage = "card_details_entry"
            minutes_since_abandonment = rng.randint(2, 15)
            is_returning_customer = False  # always a new customer, by construction
            device_type = _pick_device(rng, new_customer_leaning=True)

        policy = ABANDONMENT_POLICIES[reason]
        # Recoverability decays the longer the cart has sat abandoned -
        # same principle as generate_data.py's halted_days_ago decay, just
        # on a much shorter (minutes, not days) clock, matching how much
        # faster checkout intent cools compared to a subscription retry
        # cadence.
        decay = max(0.3, 1.0 - (minutes_since_abandonment / 240) * 0.6)
        effective_rate = policy.simulated_recovery_rate * decay
        simulated_customer_response = (
            rng.random() < effective_rate if policy.simulated_recovery_rate > 0 else False
        )

        records.append({
            "cart_id": f"cart_{uuid.uuid4().hex[:14]}",
            "merchant_id": f"merchant_{merchant_num:03d}",
            "customer_id": f"cust_{uuid.uuid4().hex[:10]}",
            "item": item_name,
            "amount_paise": amount_paise,
            "currency": "INR",
            "checkout_stage": checkout_stage,
            "minutes_since_abandonment": minutes_since_abandonment,
            "device_type": device_type,
            "is_returning_customer": is_returning_customer,
            "abandonment_reason": reason,
            "simulated_customer_response": simulated_customer_response,
        })
    return records


def main():
    missing = [r for r in REASON_WEIGHTS if r not in ABANDONMENT_POLICIES]
    if missing:
        raise ValueError(f"REASON_WEIGHTS references reasons with no policy entry: {missing}")

    records = generate()
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(records, indent=2), encoding="utf-8")
    print(f"Wrote {len(records)} synthetic checkout-abandonment records to {OUTPUT_PATH}")

    by_reason = {}
    for r in records:
        by_reason[r["abandonment_reason"]] = by_reason.get(r["abandonment_reason"], 0) + 1
    for reason, count in sorted(by_reason.items()):
        print(f"  {reason}: {count}")


if __name__ == "__main__":
    main()

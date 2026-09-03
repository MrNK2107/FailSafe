"""
Synthetic MIXED-POOL dataset for the revenue-at-risk DETECTION stage
(detect.py) - see PS_REQUIREMENTS_DEBATE.md Round 2, finding 4.

Why this is a separate file from generate_data.py, not an extension of it:
generate_data.py's whole premise is `data/halted_subscriptions.json` - a
file whose name and contents guarantee every record is already at-risk
(Razorpay's own T+3 retry cycle already ran and failed). That's the exact
thing the debate flagged: there is no step anywhere that looks at a MIX of
subscriptions - some genuinely healthy, some genuinely at-risk - and
decides which ones need attention at all. This generator produces that
mix; `generate_data.py`'s own 150-record flagship dataset and its already-
verified `RESULTS.md`/`logs/audit_log.jsonl` are untouched by this file
existing.

What a healthy record looks like (genuinely fine, not a relabeled at-risk
one): `previous_retry_count: 0`, no `decline_code`/`raw_decline_message`,
a recent successful charge, and a clean `subscription_status`. What an
at-risk record looks like: real signs of trouble - retries already
happened, a stale gap since the last successful charge, a decline code
assigned (used ONLY as ground truth for scoring detect.py's accuracy,
exactly like generate_data.py keeps a ground-truth decline_code that
diagnose.py is never shown), and a raw gateway/bank response describing
the failure (reusing generate_data.py's own real, Razorpay-taxonomy-
grounded RAW_SIGNAL_TEMPLATES - not a separate invented pool).

The four signals detect.py is actually given - `previous_retry_count`,
`days_since_last_successful_charge`, `most_recent_gateway_response`,
`subscription_status` - are all things a real merchant/Razorpay dashboard
already has without waiting on a real multi-day retry cycle. Deliberately
NOT generated as an input to detect.py: any "is_at_risk"/"needs_attention"
boolean. `ground_truth_needs_attention` below exists ONLY so a run can
measure detect.py's accuracy afterward - the same role `decline_code`
plays for diagnose.py.

Genuine ambiguity, not an artificial difficulty knob: a real single-retry,
few-days-old, "pending" subscription can be EITHER a resolved one-off blip
(healthy) or the earliest sign of real trouble (at-risk) - a merchant's
dashboard often can't tell the two apart from `subscription_status` alone
either, which is exactly why this is a genuine classification task and not
a lookup. `_EARLY_AMBIGUOUS_STATUS` records deliberately span BOTH ground
truths with overlapping retry-count/day ranges and a similarly-worded
`subscription_status`, mirroring how generate_data.py's diagnosis clusters
share an identical raw-text pool across different decline codes.
"""

import json
import random
import uuid
from pathlib import Path

from decline_codes import DECLINE_CODES
from generate_data import PLANS, RAW_SIGNAL_TEMPLATES

OUTPUT_PATH = Path(__file__).parent.parent / "data" / "detection_pool.json"

NUM_MERCHANTS = 8

# Weighted toward genuinely recoverable/common decline modes for the
# at-risk half of the pool, reusing the same real Razorpay decline-code
# taxonomy generate_data.py already validates against - detection doesn't
# need its own separate taxonomy, only its own separate SIGNALS.
AT_RISK_CODE_WEIGHTS = {
    "insufficient_funds": 22,
    "card_expired": 16,
    "card_declined": 10,
    "authentication_failed": 9,
    "gateway_technical_error": 6,
    "bank_technical_error": 5,
    "incorrect_cvv": 4,
    "transaction_limit_exceeded": 4,
    "card_not_enrolled": 3,
    "card_disabled_for_online_payments": 3,
    "payment_timed_out": 3,
    "debit_instrument_inactive": 3,
    "payment_cancelled": 2,
    "debit_instrument_blocked": 2,
    "payment_risk_check_failed": 2,
    "payment_failed": 2,
}

# Benign gateway responses for a record that had exactly one retry but
# recovered on its own - genuinely healthy, not a relabeled decline. Kept
# deliberately generic ("succeeded") so no decline-code vocabulary leaks
# into a healthy record's text.
_RESOLVED_BLIP_RESPONSES = [
    "Retry succeeded on second attempt.",
    "Payment cleared after a brief delay - no further action needed.",
    "Card verification succeeded after an initial timeout.",
]

_CLEAN_SUCCESS_RESPONSES = [
    None,
    "Payment captured successfully.",
    "Last scheduled charge completed without issue.",
]


def _make_healthy(rng: random.Random, subscription_id: str, merchant_num: int, plan_name: str, amount_paise: int) -> dict:
    # ~1/3 of healthy records are the deliberately ambiguous "resolved
    # blip" case: one retry, a few days ago, status still "pending" (not
    # "active") - surface-similar to an early at-risk record, but there
    # was never a decline_code assigned because the retry itself succeeded.
    is_blip = rng.random() < 0.33
    if is_blip:
        return {
            "subscription_id": subscription_id,
            "merchant_id": f"merchant_{merchant_num:03d}",
            "customer_id": f"cust_{uuid.uuid4().hex[:10]}",
            "plan": plan_name,
            "amount_paise": amount_paise,
            "currency": "INR",
            "subscription_status": "pending",
            "previous_retry_count": 1,
            "days_since_last_successful_charge": rng.randint(0, 3),
            "most_recent_gateway_response": rng.choice(_RESOLVED_BLIP_RESPONSES),
            "decline_code": None,
            "raw_decline_message": None,
            "halted_days_ago": None,
            "ground_truth_needs_attention": False,
            "simulated_customer_response": False,
        }
    return {
        "subscription_id": subscription_id,
        "merchant_id": f"merchant_{merchant_num:03d}",
        "customer_id": f"cust_{uuid.uuid4().hex[:10]}",
        "plan": plan_name,
        "amount_paise": amount_paise,
        "currency": "INR",
        "subscription_status": "active",
        "previous_retry_count": 0,
        "days_since_last_successful_charge": rng.randint(0, 3),
        "most_recent_gateway_response": rng.choice(_CLEAN_SUCCESS_RESPONSES),
        "decline_code": None,
        "raw_decline_message": None,
        "halted_days_ago": None,
        "ground_truth_needs_attention": False,
        "simulated_customer_response": False,
    }


def _make_at_risk(rng: random.Random, subscription_id: str, merchant_num: int, plan_name: str, amount_paise: int) -> dict:
    codes = list(AT_RISK_CODE_WEIGHTS.keys())
    weights = list(AT_RISK_CODE_WEIGHTS.values())
    code = rng.choices(codes, weights=weights, k=1)[0]
    policy = DECLINE_CODES[code]
    raw_message = rng.choice(RAW_SIGNAL_TEMPLATES[code])

    # ~half of at-risk records are the "early" case - status still
    # "pending", only 1 retry, only a few days elapsed - deliberately
    # overlapping the healthy blip case's ranges above. The other half are
    # unambiguously further along (2-3 retries, halted, stale).
    is_early = rng.random() < 0.5
    if is_early:
        previous_retry_count = 1
        days_since_last_successful_charge = rng.randint(1, 4)
        subscription_status = "pending"
        halted_days_ago = None
    else:
        previous_retry_count = rng.choice([2, 3])
        days_since_last_successful_charge = rng.randint(8, 30)
        subscription_status = "halted" if previous_retry_count == 3 else "pending"
        halted_days_ago = days_since_last_successful_charge if subscription_status == "halted" else None

    decay = max(0.4, 1.0 - (days_since_last_successful_charge / 30) * 0.5)
    effective_success_rate = policy.simulated_success_rate * decay
    simulated_customer_response = (
        rng.random() < effective_success_rate if policy.simulated_success_rate > 0 else False
    )

    return {
        "subscription_id": subscription_id,
        "merchant_id": f"merchant_{merchant_num:03d}",
        "customer_id": f"cust_{uuid.uuid4().hex[:10]}",
        "plan": plan_name,
        "amount_paise": amount_paise,
        "currency": "INR",
        "subscription_status": subscription_status,
        "previous_retry_count": previous_retry_count,
        "days_since_last_successful_charge": days_since_last_successful_charge,
        "most_recent_gateway_response": raw_message,
        "decline_code": code,
        "raw_decline_message": raw_message,
        "halted_days_ago": halted_days_ago,
        "ground_truth_needs_attention": True,
        "simulated_customer_response": simulated_customer_response,
    }


def generate(n: int = 30, seed: int = 4242, at_risk_fraction: float = 0.45) -> list[dict]:
    """
    Produce a mixed pool of `n` records: roughly `at_risk_fraction`
    genuinely at-risk, the rest genuinely healthy - see module docstring
    for exactly what each category looks like and why the "early"/"blip"
    slices deliberately overlap in surface signals.
    """
    rng = random.Random(seed)
    merchant_plans = {i: rng.choice(PLANS) for i in range(1, NUM_MERCHANTS + 1)}

    records = []
    for _ in range(n):
        merchant_num = rng.randint(1, NUM_MERCHANTS)
        plan_name, base_amount = merchant_plans[merchant_num]
        amount_paise = int(base_amount * rng.uniform(0.9, 1.1))
        subscription_id = f"sub_{uuid.uuid4().hex[:14]}"

        if rng.random() < at_risk_fraction:
            record = _make_at_risk(rng, subscription_id, merchant_num, plan_name, amount_paise)
        else:
            record = _make_healthy(rng, subscription_id, merchant_num, plan_name, amount_paise)
        records.append(record)
    return records


def main():
    records = generate()
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(records, indent=2), encoding="utf-8")
    print(f"Wrote {len(records)} synthetic mixed-pool records to {OUTPUT_PATH}")

    at_risk = sum(1 for r in records if r["ground_truth_needs_attention"])
    healthy = len(records) - at_risk
    print(f"  genuinely at-risk: {at_risk}, genuinely healthy: {healthy}, total: {len(records)}")


if __name__ == "__main__":
    main()

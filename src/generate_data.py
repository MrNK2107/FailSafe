"""
Synthetic halted-subscription dataset generator.

Schema-accurate to real Razorpay fields (decline codes, amounts in paise,
INR), but the records themselves are fabricated - there is no real
customer data anywhere in this project. Says so explicitly in the output
file and in RESULTS.md.

Distribution is deliberately NOT uniform and NOT flattering:
  - weighted toward the decline codes that actually happen most (insufficient
    funds, card expired) rather than an even split across all codes
  - includes a realistic slice of genuinely unrecoverable and fraud-flagged
    cases (~15-20% combined) so the results can't just show a fake 100%
    recovery rate
  - each record also carries a `simulated_customer_response` field: an
    independent ground-truth roll (using DECLINE_CODES[...].simulated_success_rate)
    of whether the customer would actually complete a nudge/retry. This is
    what lets RESULTS.md report an honest "simulated recovered amount"
    instead of an unearned one.

`decline_code` here is still assigned as ground truth (kept deliberately -
it's the only way to ever measure diagnosis accuracy honestly). What
changed: each record now ALSO carries a `raw_decline_message` field - a
raw, human/bank-style decline string that a real diagnosis step
(src/diagnose.py) has to interpret to infer decline_code, rather than
being handed decline_code directly. See RAW_SIGNAL_TEMPLATES below for why
this is genuinely ambiguous in places, not just a reworded restatement of
the code - closes the gap documented in PS_REQUIREMENTS_DEBATE.md Round 2
("root-cause diagnosis does not exist as a capability").
"""

import json
import random
import uuid
from pathlib import Path

from decline_codes import DECLINE_CODES

OUTPUT_PATH = Path(__file__).parent.parent / "data" / "halted_subscriptions.json"

# Raw, human/bank-style decline messages a real diagnosis step has to
# interpret - modeled on how Razorpay's own documented decline-code
# taxonomy (razorpay.com/docs/errors/payments/cards/, same source
# decline_codes.py cites) actually surfaces at the bank/gateway response
# level, which is routinely vaguer than the internal taxonomy derived
# from it. No template below contains the decline_code string itself -
# that would make "diagnosis" a lookup wearing a costume.
#
# Three clusters deliberately share an IDENTICAL raw-text pool across two
# or three decline_codes that map to genuinely DIFFERENT recovery actions
# (config/decline_policy.json) - real, structural ambiguity a raw
# bank/gateway decline reason often carries, not an artificial difficulty
# knob:
#   - card_declined / payment_failed / payment_risk_check_failed: a
#     generic "do not honor" response is exactly how banks routinely
#     surface an undisclosed risk/fraud hold - they deliberately don't
#     reveal fraud-detection logic in decline text, so an ordinary decline
#     (payment_link_nudge) and a fraud decline (no_action_fraud) can look
#     textually identical.
#   - debit_instrument_blocked / debit_instrument_inactive: bank decline
#     text for "blocked" and "inactive" is often interchangeable in
#     practice, but one is permanent (no_action_unrecoverable) and the
#     other is fixable by enabling the card for online use
#     (payment_link_nudge).
#   - payment_cancelled / authentication_failed: a gateway that doesn't
#     distinguish a user-initiated cancel from a failed/abandoned OTP step
#     produces the same "did not complete" text for both, but one is
#     unrecoverable and the other is customer-fixable right now.
# These three clusters are ~34/150 records (~23%) under CODE_WEIGHTS below
# - real ambiguity is present, not the whole dataset.
_GENERIC_BANK_DECLINE_POOL = [
    "Issuer response: do not honor.",
    "Bank declined this transaction with no further reason provided.",
    "Generic decline from issuing bank - no error detail returned.",
]
_BLOCKED_OR_INACTIVE_POOL = [
    "Issuer response: card blocked.",
    "Bank declined: this card cannot currently be used.",
]
_CANCELLED_OR_AUTH_FAILED_POOL = [
    "Transaction not completed - customer exited before verification finished.",
    "Payment did not go through during the authentication step.",
]

RAW_SIGNAL_TEMPLATES: dict[str, list[str]] = {
    "insufficient_funds": [
        "Bank response: insufficient balance in account to complete this transaction.",
        "Decline reason from issuer: low balance at time of debit.",
    ],
    "card_expired": [
        "Card verification failed: card has passed its valid-thru date.",
        "Issuer declined - card expiry date has lapsed.",
    ],
    "card_not_enrolled": [
        "Issuer response: card not enrolled for this payment channel.",
        "Bank declined: card lacks activation for this type of digital transaction.",
    ],
    "card_disabled_for_online_payments": [
        "Issuer response: online/e-commerce transactions are disabled on this card.",
        "Bank declined: card not enabled for card-not-present use.",
    ],
    "incorrect_cvv": [
        "Issuer declined: CVV/CVV2 verification failed.",
        "Bank response: security code entered does not match card on file.",
    ],
    "authentication_failed": _CANCELLED_OR_AUTH_FAILED_POOL + [
        "OTP/3-D Secure verification failed for this transaction.",
    ],
    "debit_instrument_blocked": _BLOCKED_OR_INACTIVE_POOL,
    "debit_instrument_inactive": _BLOCKED_OR_INACTIVE_POOL,
    "transaction_limit_exceeded": [
        "Bank response: transaction exceeds the daily/per-transaction limit set on this card.",
        "Issuer declined: spend limit reached for this account today.",
    ],
    "payment_timed_out": [
        "Gateway response: customer did not complete authentication within the allotted time.",
        "Transaction expired - payment session timed out before completion.",
    ],
    "gateway_technical_error": [
        "Payment gateway reported an internal processing error; no funds were debited.",
        "Acquirer-side technical failure while routing this transaction.",
    ],
    "bank_technical_error": [
        "Issuing bank's system was unreachable/down at the time of this transaction.",
        "Bank-side outage prevented this transaction from being authorized.",
    ],
    "card_declined": _GENERIC_BANK_DECLINE_POOL,
    "payment_risk_check_failed": _GENERIC_BANK_DECLINE_POOL,
    "payment_cancelled": _CANCELLED_OR_AUTH_FAILED_POOL,
    "payment_failed": _GENERIC_BANK_DECLINE_POOL,
}

# Weighted so common real-world failure modes dominate, matching how
# these actually distribute in subscription billing (soft declines are
# the bulk; fraud/blocked cards are a small minority).
CODE_WEIGHTS = {
    "insufficient_funds": 28,
    "card_expired": 18,
    "card_declined": 12,
    "authentication_failed": 10,
    "gateway_technical_error": 8,
    "bank_technical_error": 6,
    "incorrect_cvv": 5,
    "transaction_limit_exceeded": 4,
    "card_not_enrolled": 3,
    "card_disabled_for_online_payments": 3,
    "payment_timed_out": 3,
    "debit_instrument_inactive": 3,
    "payment_cancelled": 3,
    "debit_instrument_blocked": 2,
    "payment_risk_check_failed": 2,   # fraud - small but non-zero, deliberately
    "payment_failed": 2,
}

PLANS = [
    ("Streaming Monthly", 29900),
    ("Streaming Annual", 249900),
    ("SaaS Seat Monthly", 149900),
    ("SaaS Team Quarterly", 899900),
    ("Fitness App Monthly", 49900),
    ("Cloud Storage Monthly", 19900),
]


NUM_MERCHANTS = 12


def generate(n: int = 150, seed: int = 42) -> list[dict]:
    rng = random.Random(seed)
    # A SEPARATE RNG stream, deliberately independent of `rng` above.
    # raw_decline_message was added after the flagship 150-record dataset
    # (and its already-verified RESULTS.md/audit_log.jsonl) already
    # existed - drawing it from the same `rng` stream would shift every
    # subsequent rng.choices()/randint()/uniform() call by one draw,
    # silently changing which decline_code, merchant, amount, and
    # simulated_customer_response every later record gets for the SAME
    # seed. Kept on its own stream so `generate(seed=42)` still reproduces
    # the exact ground-truth fields the flagship run was verified against
    # - only raw_decline_message is new, nothing else silently shifts.
    raw_signal_rng = random.Random(f"raw-signal-{seed}")
    codes = list(CODE_WEIGHTS.keys())
    weights = list(CODE_WEIGHTS.values())

    # Pin each merchant to one plan - a single subscription merchant sells
    # one product, not a random mix of Streaming/SaaS/Fitness plans. Caught
    # in review as a realism tell.
    merchant_plans = {
        i: rng.choice(PLANS) for i in range(1, NUM_MERCHANTS + 1)
    }

    records = []
    for _ in range(n):
        code = rng.choices(codes, weights=weights, k=1)[0]
        policy = DECLINE_CODES[code]
        raw_decline_message = raw_signal_rng.choice(RAW_SIGNAL_TEMPLATES[code])
        merchant_num = rng.randint(1, NUM_MERCHANTS)
        plan_name, base_amount = merchant_plans[merchant_num]
        # +/- 10% jitter so amounts aren't suspiciously uniform
        amount_paise = int(base_amount * rng.uniform(0.9, 1.1))
        halted_days_ago = rng.randint(0, 14)

        # Recoverability decays the longer a subscription has sat halted -
        # a customer whose card expired 13 days ago is less likely to still
        # complete a nudge than one from yesterday. Was previously generated
        # and never used anywhere; now it actually affects the outcome it's
        # supposed to represent.
        decay = max(0.4, 1.0 - (halted_days_ago / 14) * 0.5)
        effective_success_rate = policy.simulated_success_rate * decay

        simulated_customer_response = (
            rng.random() < effective_success_rate
            if policy.simulated_success_rate > 0
            else False
        )

        records.append({
            "subscription_id": f"sub_{uuid.uuid4().hex[:14]}",
            "merchant_id": f"merchant_{merchant_num:03d}",
            "customer_id": f"cust_{uuid.uuid4().hex[:10]}",
            "plan": plan_name,
            "amount_paise": amount_paise,
            "currency": "INR",
            "decline_code": code,
            "raw_decline_message": raw_decline_message,
            "previous_retry_count": 3,  # Razorpay's real T+3 cycle already ran and failed
            "halted_days_ago": halted_days_ago,
            "simulated_customer_response": simulated_customer_response,
        })
    return records


def main():
    missing = [c for c in DECLINE_CODES if c not in RAW_SIGNAL_TEMPLATES]
    if missing:
        raise ValueError(f"RAW_SIGNAL_TEMPLATES is missing entries for: {missing}")

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

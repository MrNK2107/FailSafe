"""
Synthetic overdue-receivables dataset generator - the OVERDUE RECEIVABLES
sibling of generate_data.py (halted subscriptions) and
generate_checkout_abandonment_data.py (abandoned checkouts), closing the
LAST of the three category-scope gaps (PS_REQUIREMENTS_DEBATE.md;
README.md §6's own disclosure of it, which explicitly left this one
category "still not started at all" after checkout abandonment closed the
other one).

Structurally different from both siblings by definition, not by choice: a
B2B receivable has no decline_code (nobody attempted or declined a
payment) and no checkout funnel (nobody started a checkout at all). What
this category is actually about is an AGING CLOCK (`days_overdue`) plus a
business's own payment-behavior history and how much automated
communication has already gone out about this specific invoice.
Schema-accurate to a plausible real B2B accounts-receivable record (an
invoice id, issue/due dates, payment terms, an aging clock, a customer
payment-history signal, and a reminder-communication history), but, like
every other synthetic dataset in this project, fabricated - no real
business/customer data anywhere here.

`case_reason` is generated as ground truth here (kept deliberately -
exactly like decline_code/abandonment_reason are kept in the other two
generators - the only way to ever measure diagnosis accuracy honestly).
It is NEVER exposed to diagnose_receivable.diagnose_receivable(), which
only ever sees days_overdue/payment_terms/customer_payment_history_signal/
reminders_sent_count/last_reminder_response/amount_vs_typical_ratio -
receivables_agent.py reads case_reason ONLY to score
diagnosis_matched_ground_truth in the audit log.

Two DELIBERATE ambiguity clusters, mirroring generate_data.py's raw-text
clusters and generate_checkout_abandonment_data.py's own two clusters -
real, structural ambiguity in the signal space the diagnosis stage
actually sees, not an artificial difficulty knob:

  - Cluster A: `customer_payment_history_signal="first_time_overdue"`,
    `reminders_sent_count=0`, `days_overdue` in [1, 10] is shared by BOTH
    `cash_flow_delay` (a normal-sized invoice, just a timing problem) AND
    `payment_process_friction` (probably stuck in an approval workflow) -
    `amount_vs_typical_ratio` is only a WEAK tie-breaker (the two
    populations' ratio ranges deliberately overlap), not a clean
    separator.
  - Cluster B: `customer_payment_history_signal="disputes_invoices"` with
    `last_reminder_response` in {"no_response", "requested_extension"} is
    shared by BOTH `invoice_dispute_likely` (going quiet while pursuing
    the dispute through another channel) AND `high_risk_non_payment`
    (this time it is not really a dispute, just avoidance) - nothing in
    these two fields alone distinguishes them.

test_generate_receivables_data.py asserts both ground truths genuinely
appear inside each shared signal band, or the test fails - a structural
guarantee, not a hope.

Also deliberately produces some invoices ABOVE receivables_gate.py's
reused MAX_ACTION_AMOUNT_PAISE cap (B2B invoices are realistically larger
than a checkout cart or a subscription charge - see that module's
docstring for why this is an intended, disclosed consequence, not an
oversight), some invoices with `reminders_sent_count` at or past
MAX_REMINDERS_BEFORE_ESCALATION, and some with `days_overdue` at or past
DAYS_OVERDUE_LEGAL_REVIEW_THRESHOLD, so both compliant-escalation
stopping rules have real cases to fire on in a live run, not just in a
unit test.
"""

import datetime
import json
import random
import uuid
from pathlib import Path

from receivables_policy import RECEIVABLE_POLICIES

OUTPUT_PATH = Path(__file__).parent.parent / "data" / "overdue_invoices.json"

TODAY = datetime.date(2026, 9, 2)

PAYMENT_TERMS_DAYS = {"net_15": 15, "net_30": 30, "net_45": 45, "net_60": 60}
PAYMENT_TERMS = list(PAYMENT_TERMS_DAYS)

HISTORY_SIGNALS = [
    "first_time_overdue",
    "always_pays_late_but_pays",
    "disputes_invoices",
    "chronic_non_payer",
]

REASON_WEIGHTS = {
    "cash_flow_delay": 25,
    "payment_process_friction": 15,
    "chronic_late_payer_will_eventually_pay": 25,
    "invoice_dispute_likely": 15,
    "high_risk_non_payment": 20,
}

NUM_MERCHANTS = 8

# (business name, typical order size in paise). Deliberately wide, and
# deliberately including sizes both above and below
# receivables_gate.MAX_ACTION_AMOUNT_PAISE (Rs 50,000) - see module
# docstring.
CUSTOMER_PROFILES = [
    ("Anand Textiles Pvt Ltd", 45_000 * 100),
    ("Bluewave Logistics", 120_000 * 100),
    ("Crestline Stationers", 25_000 * 100),
    ("Deepak Fabrication Works", 300_000 * 100),
    ("Everstone Retail Supplies", 60_000 * 100),
    ("Ferro Components", 15_000 * 100),
    ("Ganges Cold Storage", 80_000 * 100),
    ("Harit Agro Traders", 200_000 * 100),
    ("Indus Packaging Co", 35_000 * 100),
    ("Jyoti Electricals", 500_000 * 100),
    ("Kaveri Furnishings", 55_000 * 100),
    ("Lotus Print Solutions", 18_000 * 100),
]


def _issue_and_due_dates(days_overdue: int, terms_days: int) -> tuple[str, str]:
    due_date = TODAY - datetime.timedelta(days=days_overdue)
    issue_date = due_date - datetime.timedelta(days=terms_days)
    return issue_date.isoformat(), due_date.isoformat()


def generate(n: int = 150, seed: int = 11) -> list[dict]:
    rng = random.Random(seed)
    reasons = list(REASON_WEIGHTS.keys())
    weights = list(REASON_WEIGHTS.values())

    records = []
    for _ in range(n):
        reason = rng.choices(reasons, weights=weights, k=1)[0]
        merchant_num = rng.randint(1, NUM_MERCHANTS)
        business_name, typical_amount = rng.choice(CUSTOMER_PROFILES)
        payment_terms = rng.choice(PAYMENT_TERMS)
        terms_days = PAYMENT_TERMS_DAYS[payment_terms]

        if reason == "cash_flow_delay":
            if rng.random() < 0.7:
                # Cluster A share: normal-sized invoice, early, no reminders.
                history_signal = "first_time_overdue"
                reminders_sent_count = 0
                days_overdue = rng.randint(1, 10)
                last_reminder_response = None
                ratio = rng.uniform(0.7, 1.5)
            else:
                history_signal = rng.choice(["first_time_overdue", "always_pays_late_but_pays"])
                reminders_sent_count = rng.randint(1, 2)
                days_overdue = rng.randint(11, 35)
                last_reminder_response = rng.choice(["no_response", "promised_to_pay"])
                ratio = rng.uniform(0.7, 1.3)

        elif reason == "payment_process_friction":
            if rng.random() < 0.7:
                # Cluster A share: SAME (first_time_overdue, 0 reminders,
                # <=10 days) band as cash_flow_delay above - the genuine
                # ambiguity cluster. Ratio skews larger but deliberately
                # overlaps cash_flow_delay's own range (1.2-1.5), so it is
                # only a weak tie-breaker, not a clean separator.
                history_signal = "first_time_overdue"
                reminders_sent_count = 0
                days_overdue = rng.randint(1, 10)
                last_reminder_response = None
                ratio = rng.uniform(1.2, 4.0)
            else:
                history_signal = "first_time_overdue"
                reminders_sent_count = 0
                days_overdue = rng.randint(1, 7)
                last_reminder_response = None
                ratio = rng.uniform(1.5, 3.0)

        elif reason == "chronic_late_payer_will_eventually_pay":
            history_signal = "always_pays_late_but_pays"
            reminders_sent_count = rng.randint(0, 3)
            days_overdue = rng.randint(5, 60)
            last_reminder_response = (
                None if reminders_sent_count == 0
                else rng.choice(["no_response", "promised_to_pay", "promised_to_pay", "requested_extension"])
            )
            ratio = rng.uniform(0.8, 1.3)

        elif reason == "invoice_dispute_likely":
            if rng.random() < 0.5:
                # Unambiguous: an explicit dispute on a reminder response.
                history_signal = rng.choice(["disputes_invoices", "first_time_overdue", "always_pays_late_but_pays"])
                reminders_sent_count = rng.randint(1, 3)
                days_overdue = rng.randint(10, 60)
                last_reminder_response = "disputed_charge"
                ratio = rng.uniform(0.6, 2.0)
            else:
                # Cluster B share.
                history_signal = "disputes_invoices"
                reminders_sent_count = rng.randint(1, 4)
                days_overdue = rng.randint(15, 80)
                last_reminder_response = rng.choice(["no_response", "requested_extension"])
                ratio = rng.uniform(0.6, 2.0)

        else:  # high_risk_non_payment
            if rng.random() < 0.6:
                history_signal = "chronic_non_payer"
                reminders_sent_count = rng.randint(2, 6)
                days_overdue = rng.randint(30, 150)
                last_reminder_response = rng.choice(["no_response", "no_response", "requested_extension"])
                ratio = rng.uniform(0.5, 2.5)
            else:
                # Cluster B share (same signal band as invoice_dispute_likely's own share above).
                history_signal = "disputes_invoices"
                reminders_sent_count = rng.randint(1, 4)
                days_overdue = rng.randint(15, 80)
                last_reminder_response = rng.choice(["no_response", "requested_extension"])
                ratio = rng.uniform(0.6, 2.0)

        amount_paise = max(1000, int(typical_amount * ratio))
        issue_date, due_date = _issue_and_due_dates(days_overdue, terms_days)

        policy = RECEIVABLE_POLICIES[reason]
        # Recoverability decays the longer the invoice has sat overdue -
        # same principle as generate_data.py's halted_days_ago decay and
        # generate_checkout_abandonment_data.py's minutes-based decay, on
        # this domain's own much slower (months, not days/minutes) clock.
        decay = max(0.3, 1.0 - (days_overdue / 180) * 0.5)
        effective_rate = policy.simulated_recovery_rate * decay
        simulated_customer_response = (
            rng.random() < effective_rate if policy.simulated_recovery_rate > 0 else False
        )

        records.append({
            "invoice_id": f"inv_{uuid.uuid4().hex[:14]}",
            "merchant_id": f"merchant_{merchant_num:03d}",
            "customer_id": f"cust_{uuid.uuid4().hex[:10]}",
            "business_name": business_name,
            "amount_paise": amount_paise,
            "currency": "INR",
            "invoice_issue_date": issue_date,
            "due_date": due_date,
            "payment_terms": payment_terms,
            "days_overdue": days_overdue,
            "customer_payment_history_signal": history_signal,
            "reminders_sent_count": reminders_sent_count,
            "last_reminder_response": last_reminder_response,
            "typical_order_amount_paise": typical_amount,
            "amount_vs_typical_ratio": round(amount_paise / typical_amount, 3),
            "case_reason": reason,
            "simulated_customer_response": simulated_customer_response,
        })
    return records


def main():
    missing = [r for r in REASON_WEIGHTS if r not in RECEIVABLE_POLICIES]
    if missing:
        raise ValueError(f"REASON_WEIGHTS references reasons with no policy entry: {missing}")

    records = generate()
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(records, indent=2), encoding="utf-8")
    print(f"Wrote {len(records)} synthetic overdue-receivable records to {OUTPUT_PATH}")

    by_reason = {}
    for r in records:
        by_reason[r["case_reason"]] = by_reason.get(r["case_reason"], 0) + 1
    for reason, count in sorted(by_reason.items()):
        print(f"  {reason}: {count}")


if __name__ == "__main__":
    main()

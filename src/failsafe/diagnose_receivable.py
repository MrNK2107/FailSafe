import time

import requests

from failsafe.llm_client import MAX_RETRIES, OLLAMA_HOST, OLLAMA_MODEL, OllamaToolCaller

_REASON_DESCRIPTIONS = {
    "cash_flow_delay": (
        "the business likely has a temporary cash-flow crunch but intends to pay - a "
        "normal-sized invoice, early in the overdue window, no reminders sent yet"
    ),
    "payment_process_friction": (
        "the delay is probably administrative (invoice stuck in an approval workflow, "
        "sent to the wrong contact) rather than the business's ability or intent to pay "
        "- early in the overdue window, no reminders sent yet"
    ),
    "chronic_late_payer_will_eventually_pay": (
        "this business has a track record of paying late but reliably paying eventually "
        "- treat as low-risk despite being overdue"
    ),
    "invoice_dispute_likely": (
        "signals suggest the business disputes something about this invoice (amount, "
        "delivery, service) rather than being unable or unwilling to pay outright"
    ),
    "high_risk_non_payment": (
        "signals suggest a real risk of non-payment - a track record of chronic "
        "non-payment, or continued silence after prior reminders, that warrants "
        "escalation rather than a routine nudge"
    ),
}
_SORTED_REASONS = sorted(_REASON_DESCRIPTIONS.items())

DIAGNOSE_RECEIVABLE_TOOL = {
    "type": "function",
    "function": {
        "name": "record_receivable_diagnosis",
        "description": (
            "Record your diagnosis of WHY this B2B invoice is overdue and what kind of "
            "case it is. This is diagnosis ONLY - a separate, deterministic policy table "
            "(not this call) decides what collections action to take based on your "
            "diagnosis."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "case_reason": {
                    "type": "string",
                    "enum": [reason for reason, _ in _SORTED_REASONS],
                    "description": (
                        "The single reason that best explains why this invoice is overdue. "
                        "Known reasons and what each means:\n"
                        + "\n".join(f"- {reason}: {desc}" for reason, desc in _SORTED_REASONS)
                        + "\n\nApply these rules IN ORDER (check rule 1 first; only move to the "
                        "next rule if the current one does not match):\n"
                        "1. last_reminder_response is 'disputed_charge' -> "
                        "'invoice_dispute_likely'. An explicit dispute on a reminder is the "
                        "strongest possible signal for this category - this OVERRIDES rule 2's "
                        "history-based signal below even if customer_payment_history_signal is "
                        "'always_pays_late_but_pays'. Do not skip past this rule just because a "
                        "later rule also looks like it matches.\n"
                        "2. customer_payment_history_signal is 'always_pays_late_but_pays' -> "
                        "'chronic_late_payer_will_eventually_pay'. A known-reliable-but-slow "
                        "payer's current overdue invoice is business as usual for them, not a "
                        "new risk signal (unless rule 1 already matched above).\n"
                        "3. customer_payment_history_signal is 'disputes_invoices' AND "
                        "last_reminder_response is 'no_response' or 'requested_extension' -> "
                        "genuinely ambiguous between 'invoice_dispute_likely' (going quiet while "
                        "building/pursuing their dispute through another channel) and "
                        "'high_risk_non_payment' (this time it is not really a dispute, just "
                        "avoidance) - weigh reminders_sent_count as a weak tie-breaker (more "
                        "reminders met with continued silence leans toward "
                        "'high_risk_non_payment'), but expect to sometimes be wrong here; that "
                        "is fine.\n"
                        "4. customer_payment_history_signal is 'chronic_non_payer' -> "
                        "'high_risk_non_payment'. A track record of chronic non-payment is a "
                        "strong prior regardless of how this specific invoice looks.\n"
                        "5. customer_payment_history_signal is 'first_time_overdue' AND "
                        "reminders_sent_count is 0 AND days_overdue is small (roughly 10 or "
                        "fewer) -> decide between 'cash_flow_delay' (a normal-sized invoice, just "
                        "a timing problem) and 'payment_process_friction' (probably stuck in an "
                        "approval workflow, nothing to do with ability to pay) using this CONCRETE "
                        "rule on amount_vs_typical_ratio, not a vague impression of it: if the "
                        "ratio is ABOVE 1.5x the customer's typical order size, pick "
                        "'payment_process_friction' - an invoice that much larger than normal is "
                        "the kind of thing that realistically triggers an extra approval step, "
                        "and you should pick 'payment_process_friction' even if you can also "
                        "imagine a cash-flow story, because size alone is the deciding factor "
                        "here. If the ratio is AT OR BELOW 1.5x, pick 'cash_flow_delay'. This is a "
                        "real, sometimes-wrong-either-way call in the 1.2x-1.5x band specifically "
                        "(both patterns genuinely occur there) - that is fine - but ABOVE 1.5x, "
                        "'payment_process_friction' is the intended answer, do not default to "
                        "'cash_flow_delay' out of habit just because it is the more common label "
                        "overall.\n"
                        "6. Otherwise (no stronger signal above clearly applies) -> "
                        "'cash_flow_delay' as the general default for an occasional overdue "
                        "invoice with no other distinguishing signal.\n\n"
                        "Worked examples: always_pays_late_but_pays, 55 days overdue, 1 reminder, "
                        "disputed_charge -> invoice_dispute_likely (rule 1 - the explicit dispute "
                        "overrides the payer's own history, even a normally-reliable one). "
                        "always_pays_late_but_pays, 20 days overdue, 1 reminder, no_response -> "
                        "chronic_late_payer_will_eventually_pay (rule 2 - history explains plain "
                        "silence, just not an explicit dispute). first_time_overdue, 3 days "
                        "overdue, 0 reminders, ratio near 1.0 -> cash_flow_delay (rule 5, at or "
                        "below the 1.5x line). first_time_overdue, 5 days overdue, 0 reminders, "
                        "ratio 2.5x typical -> payment_process_friction (rule 5, clearly above the "
                        "1.5x line - pick payment_process_friction even though a cash-flow story "
                        "is also imaginable). first_time_overdue, 8 days overdue, 0 reminders, "
                        "ratio 3.9x typical -> payment_process_friction (same rule - a ratio this "
                        "large is a strong, not a weak, signal; do not talk yourself back into "
                        "cash_flow_delay). chronic_non_payer, 70 days overdue, 3 reminders, "
                        "no_response -> high_risk_non_payment (rule 4). Pick the single most "
                        "likely reason given only the signals below and nothing else. You will "
                        "not always be right on a genuinely ambiguous case (rule 3, and rule 5's "
                        "1.2x-1.5x band), and that is expected."
                    ),
                },
                "reasoning": {
                    "type": "string",
                    "description": "One or two sentences on why this reason best fits these signals.",
                },
            },
            "required": ["case_reason", "reasoning"],
        },
    },
}

_caller = OllamaToolCaller(
    host=OLLAMA_HOST, model=OLLAMA_MODEL, timeout=120, retries=MAX_RETRIES, keep_alive="30m"
)


def diagnose_receivable(
    days_overdue: int,
    payment_terms: str,
    customer_payment_history_signal: str,
    reminders_sent_count: int,
    last_reminder_response: str | None,
    amount_vs_typical_ratio: float | None = None,
) -> dict:
    context = (
        f"\nInvoice amount vs this customer's typical order size: {amount_vs_typical_ratio:.2f}x"
        if amount_vs_typical_ratio is not None
        else ""
    )
    response_line = (
        f"Last reminder response: {last_reminder_response}"
        if last_reminder_response is not None
        else "Last reminder response: (no reminder sent yet)"
    )
    prompt = (
        "A B2B invoice has gone unpaid past its due date - no payment was ever "
        "attempted or declined, the invoice simply aged past its due date. Below "
        "are the only signals available about this account and this invoice. You "
        "have NOT been told why it is actually overdue.\n\n"
        f"Days overdue: {days_overdue}\n"
        f"Payment terms: {payment_terms}\n"
        f"Customer payment history signal: {customer_payment_history_signal}\n"
        f"Reminders already sent: {reminders_sent_count}\n"
        f"{response_line}"
        f"{context}\n\n"
        "Call record_receivable_diagnosis with the single reason that best "
        "explains why this invoice is overdue and a short reasoning."
    )
    messages = [{"role": "user", "content": prompt}]
    result = _caller.call_tool_sync(
        messages, [DIAGNOSE_RECEIVABLE_TOOL], fallback_key="case_reason", fallback_value=None
    )
    if "case_reason" not in result:
        return {
            "case_reason": None,
            "reasoning": result.get("reasoning", "Model returned no tool call."),
        }
    return {"case_reason": result.get("case_reason"), "reasoning": result.get("reasoning", "")}

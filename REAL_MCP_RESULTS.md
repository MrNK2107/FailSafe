# REAL_MCP_RESULTS — verifiable test-mode Razorpay objects

On the run recorded in `logs/real_mcp_server_run.jsonl`, FailSafe created
**5 real objects** in a Razorpay **test-mode** account via Razorpay's official
MCP server (`mcp/razorpay` over stdio). These are not simulations: each was an
authenticated API round-trip through the real server, verifiable by logging
into that account's dashboard.

| Subscription | Decline code | Action | Tool | Razorpay object ID | Amount |
|---|---|---|---|---|---|
| `sub_9acc7c13998447` | `payment_timed_out` | immediate_retry | create_retry_order | `order_TVya2xkz293ced` | ₹304.41 |
| `sub_6290a50fdba748` | `gateway_technical_error` | immediate_retry | create_retry_order | `order_TVya4XdHthFVVR` | ₹195.79 |
| `sub_cf5a67f7bcf846` | `insufficient_funds` | delayed_retry | create_retry_order | `order_TVya8ntYFOCcb7` | ₹203.06 |
| `sub_f41bfcfd51474d` | `card_expired` | payment_link_nudge | create_payment_link | `plink_TVyaB1NfbPJerN` | ₹281.97 |
| `sub_140d9d8513774b` | `authentication_failed` | payment_link_nudge | create_payment_link | `plink_TVyaCtXyLz50Rk` | ₹192.00 |

## How to reproduce

1. Put real `rzp_test_` keys in `.env` (free, no KYC, at
   dashboard.razorpay.com — test mode never touches real money).
2. Run:

   ```bash
   python -m failsafe real-mcp-demo 5
   ```

3. The run log lands in `logs/real_mcp_server_run.jsonl` (append-only), and
   the objects above appear under Payments → Orders / Payment Links in the
   test dashboard.

## Notes

- The gate still runs on every record before any tool call; the demo is the
  full pipeline with the official MCP server as executor, not a bypass.
- Test-mode object IDs are stable but dashboards can be reset; the raw JSON
  responses in the run log are the durable evidence.
- This project is test-mode only: `failsafe/razorpay_client.py` refuses to run
  against a `rzp_live_` key by construction.

# Screenshots

All captures come from an isolated headless Chrome (`--headless=new` with a throwaway `--user-data-dir`), at 100% zoom, run against the real generated pages.

| File | Source | Capture |
|---|---|---|
| `hero.png` | `docs/brand/hero.html` | `chrome --headless=new --window-size=1280,560 --screenshot=<abs>/hero.png file:///<repo>/docs/brand/hero.html` |
| `trace.png` | `python -m failsafe.decision_trace` | `--window-size=1440,1000` on `TRACE.html?theme=light#sub_c02f15ccf66b4e` |
| `trace-dark.png` | same | `TRACE.html?theme=dark#sub_bd31c471dfd343` |
| `trace-whatif.png` | `python -m failsafe.decision_trace --policy examples/policy_whatif_retry_low_balance.json --out WHATIF.html` | `WHATIF.html?theme=light&filter=changed#sub_56a63b4669ee4e` |
| `terminal-demo.svg` | `python scripts/make_terminal_svg.py --max-lines 28 --out docs/screenshots/terminal-demo.svg "python scripts/offline_demo.py --n 12"` | real output rendered as SVG text |
| `terminal-whatif.svg` | `python scripts/make_terminal_svg.py --title "failsafe - policy what-if" --max-lines 40 --out docs/screenshots/terminal-whatif.svg "python -m failsafe.replay --policy examples/policy_whatif_retry_low_balance.json"` | same |
| `offline-demo.gif` | `python scripts/make_demo_gif.py --python .venv/Scripts/python.exe` | needs Pillow + Chrome |
| `report.png`, `policy-dashboard.png` | `make report` | `--window-size=1280,1500` on `REPORT.html` / `POLICY_DASHBOARD.html` |

Trace page URL parameters: `?theme=light|dark`, `?filter=executed|escalated|blocked|overridden|changed`, `#<record_id>` or `#<record_id>@<run>`.

Chrome writes `--screenshot` relative to its own working directory, so pass an absolute path.
| `policy-editor.png` | `python -m failsafe.console` (PORT=8765) | `--virtual-time-budget=8000 --window-size=1440,1100` on `/policy/edit?theme=light&edit=insufficient_funds:immediate_retry,card_declined:no_action_unrecoverable` |

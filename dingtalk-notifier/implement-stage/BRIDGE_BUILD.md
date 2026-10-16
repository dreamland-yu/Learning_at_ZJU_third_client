# LAZY DingTalk bridge build

Base commit: 53f6cf2c02b53fe79ff12c11da90bb31d91a899f. ASK=never, balanced.

| Rung | Feature | Acceptance command | Tier | State |
|---|---|---|---|---|
| F0 | real entry point → explicitly marked smoke artifact | `python3 dingtalk-notifier/bridge.py --smoke` | MUST | passed, exit 0 |
| F1 | SQLite persistence, threshold/rollcall deduplication | `python3 -m unittest discover -s dingtalk-notifier/tests -p test_logic.py` | MUST | passed, 13 tests |
| F2 | predefined commands and manual tasks | `python3 -m unittest discover -s dingtalk-notifier/tests -p test_commands.py` | MUST | passed, 24 tests |
| F3 | server cache, submission checks, DWS adapters | `.venv/bin/python -m unittest discover -s dingtalk-notifier/tests` | MUST | passed; 76 bridge tests plus 14 legacy sender tests |
| F4 | real local server, DING probe, interactive service and launchd | `.venv/bin/python dingtalk-notifier/bridge.py --status` | MUST | passed; real data/DING/user-confirmed reply, running/stream ready after restart |

## Run record
2026-10-15 F0: real entry point emitted .runtime/spine-smoke.json, sent=false, live_adapters=false. Exit 0 before feature implementation started.
F1/F2 and Server foundation: accumulated 48 tests passed; final smoke runs the real SQLite/command/reminder modules with explicitly offline fixtures, no surviving live-path stub.
Official app DING sent once with fixed UUID; platform accepted and user explicitly confirmed receipt. DING is enabled for rollcalls.
`bridge.py --check` started the real local Server, but macOS denied new Python access to CLI Keychain (`Keychain Access Denied`). That credential path was removed; login_server.py lets the operator authorize Server independently without sending passwords through chat.
GitHub fork created/verified: https://github.com/dreamland-yu/Learning_at_ZJU_third_client ; origin points to fork, upstream preserved. All final commits target only codex/dingtalk-reminders in the fork; commit identity is recorded by Git history.
2026-10-16: operator confirmed independent Server login. Real --check returned 7 todos, 0 current rollcalls, default minima 30/60s, and homework submission_probe status=ok/submitted=false.
LaunchAgent installed and reached running=true/stream_ready=true. User explicitly confirmed automatic private-chat replies. LaunchAgent ProgramArguments runs Python bridge.py --run; it does not launch the DingTalk desktop app.
Final review fixes: filter held reminders before delivery LIMIT; preserve unknown submission checks in health state; retire ended queued rollcalls; show local manual tasks during school outage; avoid second-precision history gaps. Regression checks included.
Post-review lifecycle adjustment: wait for the prior LaunchAgent process to stop before reloading; actual local restart passed, same 3 accepted replies remained recorded (no replay), settings and manual state preserved. This small lifecycle fix was executor-verified after the last model review.
Final restart stayed healthy across multiple 30/60s cycles: running=true, stream_ready=true, errors={}, accepted count unchanged. Total deterministic suite: 76 bridge/Server checks plus 14 legacy sender checks, all passed. Staged source audit found no configured profile, recipient ID, private conversation ID, credential state, SQLite, binaries or logs.
## Deferred
Automatic rollcall answer and assignment submission.
## Blockers
None. No active rollcall was available during deployment; rollcall first-seen/ended/restart rules and 24/12/3 transitions were verified with controlled offline clocks, not real timed course events.
## Sweep
Round 1 recorded 3 undeclared decisions (2 semantic) and 3 concrete defects. Ledger corrected; fixes and regression checks applied. Round 2 found 0 undeclared decisions and 0 remaining demonstrated defects. Full replies in SILENT_ASSUMPTION_SWEEP.json. Same-family/provisional only; not a cross-family or end-to-end acquittal. See post-review lifecycle adjustment above.

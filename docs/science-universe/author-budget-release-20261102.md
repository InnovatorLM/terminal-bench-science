# Author iteration budget and historical handoff release

Updated 2026-11-02 UTC. The active author iteration budget is 48 hours
(172800 seconds); ACS execution and recorded pauses remain excluded.

## Historical versions released

The operator explicitly admitted the two completed self-tests below with
`legacy_budget_unknown`. This preserves unknown historical duration rather
than claiming reconstructed timing or resetting the idea clock. Admission
applies only to the recorded job, author, idea and frozen task version.

| Author | Task | Commit | Self-test job | Assigned reviewer |
| --- | --- | --- | --- | --- |
| author-08 | seaice-record-audit | 069a35218dca1b065a003f88dcc13e29a7b29f53 | 466a4ebb15394c9d9452143ff666a5f2 | evaluator-02 |
| author-09 | sunspot-station-audit | e2cea4bf82876546afaadfd6023aa5a1ea29eedd | bba2c3aaf1374d8881c9caa4f2513a3a | evaluator-03 |

Both handoffs are `ready_for_independent_review`; dispatcher entries are
`reviewing`, with reviewer activity confirmed. Both author schedule records
say `waiting for the assigned evaluator review`, and their exact assignment
feedback has `author_must_pause=true`.

Reviewer updates report completed initial checks: seaice reference checks
29/29 and ten rejected shortcut mutations; sunspot reference checks 7/7.
Each reviewer submitted five independent model evaluation requests. At the
inspection time these requests were queued behind existing evaluations;
there were no new rollout results yet. Neither task is scientifically
accepted merely because its historical timing exception was approved.

## Runtime correction

- Install the common `iteration_budget.py` dependency together with both
  `author-iteration-clock` and its historical `role-iteration` command name.
- Use the same installation routine during initial role provisioning and
  runtime client refresh; replace files atomically to preserve running work.
- Verify both installed commands as each of the ten role UIDs inside its
  container root; all ten imported the 172800-second policy successfully.
- Restart only the command watchdog and self-test handoff consumer to unload
  their cached five-hour policy. Existing self-test and evaluation queue
  parents, running cloud jobs and the controller were preserved.
- Bind hot-restarted consumers to the existing launcher identity, including
  process start time, so they stop if that launcher exits or its PID is reused.
- The budget watchdog requests one reset when independently observed active
  iteration exceeds 48 hours. It retains task files and original clock, and
  directs the author to a new idea. Recorded waits do not trigger this reset.

## Validation and evidence

76 distinct targeted Python tests passed across the budget, lifecycle,
submission, handoff, supplement, watchdog and installed-client checks. These
are local tests, including synthesized cloud receipts; they are not new live
LLM/ACS evaluation results. Live checks covered the ten installed author
clients, both restarted consumers, both review assignments and continued
queue/controller process liveness.

Runtime evidence is under the active universe's
`.state/gewu-multiverse/author-budget-policy-update/`:

- `installed-clients.json`: per-role installed command and budget checks.
- `service-restarts.json`: old/new process IDs and launcher ownership.

Each listed self-test directory preserves `legacy-budget-admission.json`,
the original timing evidence snapshots and `handoff.json`. The dispatcher
ledger and author feedback remain the source of current review progress.

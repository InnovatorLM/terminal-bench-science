---
name: acs-task-pipeline
description: Standard contract for preparing, freezing, executing, diagnosing, and handing off Harbor tasks on ACS.
---

# ACS task pipeline

Use this skill for every task that may be submitted to or evaluated on ACS. The
pipeline is evidence based: a local or mock run is useful for debugging, but it
is never an ACS acceptance receipt.

## Required task contract

This contract applies to implemented tasks before execution, not to an initial
research proposal. Before execution, verify and record the task snapshot: `instruction.md`,
`task.toml`, Dockerfile/build context, `tests/test.sh`, `solution/solve.sh`, dataset/task name, and harness.
Run a schema/path check and an author self-test in a real ACS sandbox. Confirm
the test produces an explicit verifier result and reward. Missing verifier
output, `rewards=null`, a bare `ValueError`, or an infrastructure exception is
an unscored failure requiring diagnosis (task/verifier or platform), never a solver score.
Check the verifier's own build context before requesting ACS: `tests/test.sh`
and every local COPY/ADD source must exist in the frozen version. An explicitly
declared independent verifier environment must have its configured Dockerfile
or usable image. Shared/inherited verifier environments may legitimately reuse
the task image; do not add a redundant `tests/Dockerfile` merely to satisfy a
filename checklist. Read the actual builder receipt to confirm which was used.

## Immutable registration

Create a receipt before handing off: PR commit or task archive hash, task
snapshot hash, image digest (never a mutable tag), harness/runtime version,
self-test result, ACS cluster/placement policy, resource request, and artifact
locations. Evaluators may accept only the exact registered version. A changed
commit, Dockerfile, verifier, or image requires a new receipt. The old result
remains evidence for its original frozen version and does not transfer to the
new version. For an already reviewed task with a different current PR, read
`/review-feedback.json` and the version handoff reference in
`/skills/acs-task-troubleshooting/references/version-handoff.md` before more tests.

## ACS execution

Use the approved ACS Harbor environment and the versioned `universe_ack` runtime.
Pin the image by digest and use the configured pull secret. Default resources
are 4 CPU/8 GiB; retry with at most 8 CPU/16 GiB only when evidence shows a
resource failure. Keep the approved placement policy and do not schedule on
`cn-beijing-f`. The current ACK adapter supports the public network default,
but does not advertise network isolation, allowlists, or policy changes inside
a running sandbox. The worker checks Harbor's resolved task/step network plans
before building; an incompatible source task receives
`task_acs_network_incompatible` with the offending phase and baseline.

Shared/separate verifier mode comes from **task.toml**: `[verifier]
environment_mode="separate"`, or an explicit `[verifier.environment]`, selects
a separate environment; without either, shared mode is valid. Job YAML/JSON
`verifier.environment_mode` and `verifier.environment` are ignored by this
Harbor version and do not fix network mismatches. For separate mode, declare
the solver outputs that must cross environments in `artifacts` (or write them
under `/logs/artifacts`) and verify the transfer with oracle/nop. Do not assume
the verifier sees changes to the solver's whole filesystem. Its startup
baseline and phase policy must match and both must be supported; separate mode
does not provide unsupported static isolation. Update the source task and
freeze a new commit if its contract needs changing; never rewrite a frozen
snapshot silently. Collect job result,
verifier stdout/reward, pod events, and the agent trajectory, then clean all
temporary ACS resources.

## Five-run quality evaluation

Run five independent DeepSeek `deepseek-v4.1-flash-1017` trials in parallel
when capacity allows. Preserve each trajectory and verifier evidence. Separate
model failures from ACS/Harbor/image/network/resource failures. At the four-hour deadline, first check for incomplete task/environment or
model/tool infrastructure; if those are excluded and the task is genuinely difficult, count it as a model failure
and treat the difficulty as positive evidence. A pass rate at or below 60% is
acceptable under the current policy; at or above 80% is a strict rejection.

## Handoff and security

Authors must self-check, run through ACS, inspect results, and iterate before
submission. Do not submit basic format, image, verifier, or dependency defects.
Attach the receipt and a concise handoff; evaluators must be assigned the exact
version. Do not print or commit API keys, ACR credentials, kubeconfigs, private
endpoints, or raw private trajectories. Redact logs before publishing.

## Failure taxonomy

Classify failures as: task/package defect; image build or pull/auth defect;
verifier defect; ACS scheduling/resource/storage defect; network-policy defect;
model/agent failure; or timeout. Include the observed evidence and the next
action. Never mark a task accepted solely because a process exited zero.

For concrete errors and waiting states, use `/skills/acs-task-troubleshooting/SKILL.md`.

---
name: acs-task-pipeline
description: Standard contract for preparing, freezing, executing, diagnosing, and handing off Harbor tasks on ACS.
---

# ACS task pipeline

Use this skill for every task that may be submitted to or evaluated on ACS. The
pipeline is evidence based: a local or mock run is useful for debugging, but it
is never an ACS acceptance receipt.

## Required task contract

Before execution, verify and record the task snapshot: `instruction.md`,
`task.toml`, Dockerfile/build context, `tests/test.sh` (or the declared
verifier), solution/solve script where required, dataset/task name, and harness.
Run a schema/path check and an author self-test in a real ACS sandbox. Confirm
the test produces an explicit verifier result and reward. Missing verifier
output, `rewards=null`, a bare `ValueError`, or an infrastructure exception is
an infrastructure failure, never a solver score.

## Immutable registration

Create a receipt before handing off: PR commit or task archive hash, task
snapshot hash, image digest (never a mutable tag), harness/runtime version,
self-test result, ACS cluster/placement policy, resource request, and artifact
locations. Evaluators may accept only the exact registered version. A changed
commit, Dockerfile, verifier, or image requires a new receipt and invalidates
the old result.

## ACS execution

Use the approved ACS Harbor environment and the versioned `universe_ack` runtime.
Pin the image by digest and use the configured pull secret. Default resources
are 4 CPU/8 GiB; retry with at most 8 CPU/16 GiB only when evidence shows a
resource failure. Keep the approved placement policy and do not schedule on
`cn-beijing-f`. Network policy must be fixed when the verifier starts; ACS does
not support switching policy inside one running sandbox. Collect job result,
verifier stdout/reward, pod events, and the agent trajectory, then clean all
temporary ACS resources.

## Five-run quality evaluation

Run five independent DeepSeek `deepseek-v4.1-flash-1017` trials in parallel
when capacity allows. Preserve each trajectory and verifier evidence. Separate
model failures from ACS/Harbor/image/network/resource failures. A timeout over
four hours is first classified as incomplete environment/task infrastructure;
if the task is complete and genuinely difficult, count it as a model failure
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

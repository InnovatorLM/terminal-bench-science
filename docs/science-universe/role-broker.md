# Science role command broker

The science deployment explicitly selects `GEWU_HARNESS=native` and
`GEWU_ROLE_BROKER_CONFIG` pointing to a root-owned JSON configuration. Generic
multiverse installations keep their existing behavior. A configured broker
failure refuses execution; it never falls back to a host shell or host Git.

The trusted controller runs as UID65534. Provider/admin credentials remain in
its private configuration. Role shell commands and workspace Git operations
run through an authenticated UNIX socket into separate chroots as UID21001,
UID21002 and UID21003. Roots contain system tools, a Python standard library,
the role's own instruction, workspace and scoped Git token. No host proc,
provider key, admin token or other role workspace is mounted.

The workspace FUSE filesystem does not support socket inodes, so this deployment
uses a Linux abstract UNIX socket named after a workspace-local path.
SO_PEERCRED restricts requests to root and the configured controller UID.
The root-owned broker configuration must remain outside role roots. The
controller is trusted; role UIDs must be dedicated exclusively to this broker.

Ordinary shell commands have a maximum120second wall timeout; workspace Git
preparation allows1800seconds for the large repository on FUSE. Commands have
120CPU seconds and64process
bounds. Output is bounded to1MiB. Repository file sizes are not capped: the old
64MiB limit killed Git index-pack with SIGXFSZ on real repository packs. Background processes are
removed after each command, including detached children; dedicated-UID pidfd
checks verify cleanup. A lost controller connection triggers cleanup. Role
commands retain public networking as requested. This is filesystem/UID
separation, not a network or kernel namespace boundary.

Run preparation and regression scripts as operator/root, with workspace-local
TMPDIR and PYTHONDONTWRITEBYTECODE=1:

1. `deploy/science/prepare_review_chroot.py`
2. `deploy/science/prepare_role_runtime.py`
3. `deploy/science/test_role_exec.py`
4. `deploy/science/test_multiverse_role_runtime.py`

The last script uses a mock model by default. Explicit `--live` requires the
designated DawnFM environment and uses deepseek-v4.1-flash-1017; no provider
fallback exists. The real supervisor completed two live requests and a real
tool call with Git connectivity. Mock tests separately passed for all three
roles. Negative checks cover host-path/env access, traversal, untrusted socket
UID, timeout, disconnect and detached-child cleanup. Rust regression tests
cover cross-role working-directory symlinks; existing harness/confinement
tests passed24 with one helper ignored.

Production start script and matching release are installed. Full UI startup
showed both organisation repositories and all3stopped roles; normal exit
returned0and stopped its broker/Forgejo. All3real SessionSpawner preparation
and subsequent fetch checks passed without model calls. Post-start ACL inspection
confirmed no shared-agent-team members, evaluator read-only task access and
author/curator inability to read the private evaluation repository.
Do not mark production isolation complete from the single-run tests alone.
Proposal roles require their own startup preflight; full scientific admission
gates apply before accepting a task.

Actual author startup exposed a preparation gap not covered by the earlier
single-repository SessionSpawner smoke: the admin controller lists
`team/workspace`, but production role tokens cannot read it (HTTP404). Passing
the admin list directly to the role made preparation fail before any model plan.
RealSpawner now checks each requested repository through the role's own Forgejo
token before cloning and generating context. Inaccessible repositories are
omitted; unknown access/API failures abort rather than using admin credentials.
The role's permission is unchanged. The real preparation helper now requests
both repositories and asserts that only the readable science repository appears
in forgejo.json. All three roles passed that check with zero model calls.

The initial temporary UI automation also had a faulty startup-failure stop
condition and repeated preparation attempts; it was stopped and corrected.
Those attempts produced empty run directories, no plans and no model calls.
They are retained as failed preparation history, not successful sessions.
Author proposal startup and first-task admission have separate gates; missing
scientific acceptance does not itself prevent proposal work.

`deploy/science/start-universe` is installed as the universe's start script. It starts a broker owned by root, verifies that socket's
peer PID is the process it started, then runs the controller as UID65534. It
stops its broker on exit. Its native harness setting is explicit.

The engine refuses broker-mode starts without a daily session budget of1..10,
or with more than24model turns. The launcher selects3sessions per role per UTC
day. Every prepared plan counts, including errors/cancellations; durable plan
timestamps preserve consumption across board restarts. Normal per-response
output is explicitly configured to8192tokens in this deployment; generic
installations keep the2048default. `GEWU_RESPONSE_TOKENS` accepts256..16384;
invalid values fail before any model request. This is a session/turn/output bound,
not a currency ceiling or a claim of exact token billing. Independent ACS
evaluation remains a separately guarded one-trial/no-retry workflow.

The first real proposal session made10model requests and16shell tool calls, then
received an empty response with finish_reason=length at2048completion tokens.
No proposal was delivered. Its historical outcome was warned because a prior
shell command also lacked tail; the independently retained assessment marks the
session incomplete, not successful authoring. Runtime handling now fails on
length before executing any returned tools and fails on an empty final response.
The chroot provisioner includes tail and common text/path utilities, and all
three role UIDs passed actual tail/cut checks. The updated native library passed
337nonroot tests, including truncated-tool refusal and empty-final handling.
The response allowance change is explicit in the science launcher; no provider
or model fallback was introduced. A subsequent live session is still required
to demonstrate actual proposal delivery.

The second proposal session correctly failed on length at8192tokens, again
without a proposal. The final response contained reasoning_content but no visible
content. The science launcher now explicitly selects
`GEWU_CHAT_THINKING=disabled`, which sends `thinking.type=disabled` to the designated
DawnFM endpoint; generic chat clients omit that provider extension. Invalid modes
fail before sending requests. The setting is recorded in actual request bodies;
it changes neither the account nor the model and is not difficulty evidence.

Startup also needs actual work assignment: the product system prompt permits
Forgejo work only on assigned issues and the role's own PRs. Author instruction
alone does not create that assignment. `ensure_proposal_issue.py --apply --output
<fresh-private-path>` creates the documented local proposal task once, assigns
science-author and verifies read-back using the author's token. Existing matching
tasks must have the exact documented body and operator identity. It posts only to
the fixed local repository and grants no scientific approval. See
[startup task body](proposal-startup-task.md). A live proposal session must still
demonstrate delivery before authoring effectiveness is considered established.

The local startup task is now issue1, assigned to science-author and read back
using that role's token. Controller credentials are intentionally owned by the
trusted controller UID65534, not root. A separate credential reader checks their
owner,0600file and0700parent, refusing symlinks and nonoperator callers;
evaluator root_file guards remain unchanged. The acknowledgment checker uses the
same reader. Credential-guard tests and acknowledgment refusal tests passed;
no real scientific approval has been recorded.

With assigned issue1 and thinking explicitly disabled, the third author session
reached its24turn bound after24model requests and36shell results. It delivered
no proposal/commit/PR. The controller exited0, but the run correctly failed;
usage was550935input tokens and4448output tokens, not a scientific difficulty
measurement. All four dedicated UIDs were subsequently observed with0live
processes. The day's author allowance is3/3used and was not reset.

Eleven public-source research responses/documents were recovered into a private
operator archive and the author's own readonly research cache, with byte hashes.
The actual author UID verified all cache hashes and could read the handoff.
These are research inputs with licensing/query-provenance gaps, not publication-
approved task data. No evaluator-private material was copied. The next author
instruction points to its handoff and requests early persistence of findings.

Native requests now include the current turn, remaining responses and approximate
remaining time in a refreshed system note. During the final four allowed
responses the note prioritizes artifact/handoff persistence over new long network
investigations. Earlier remaining counts do not accumulate. The end-to-end mock
test verifies the actual request bodies;339nonroot library tests passed. This is
budget visibility, not proof that a future live author will deliver a proposal.

Library fixtures select the native mock transport explicitly at compile time
to prevent an installed Codex from being launched accidentally during unit
tests. Black-box Codex integration tests still use their explicit fake CLI.
Permission-sensitive tests must run as a nonroot account; root bypasses the
denials those fixtures need to exercise. Stop timing is measured from the stop
request, and asynchronous session cleanup is awaited through its durable ending.

The full nonroot Rust library regression passed332 tests, with no failures.
The evaluator regression separately passed156 tests. These do not establish
scientific candidate acceptance or target-model difficulty.

Broker-mode clones use a sibling `<destination>.gewu-clone-pending` directory
and publish it by rename only after Git succeeds. Interrupted clones therefore
cannot be mistaken for complete repositories by a later session. Existing pending
directories are refused without deleting data; the operator must inspect/recover
or remove that specific pending checkout before retrying.

Latest full nonroot library rerun:332passed in22.93seconds after Git fixes.
Workspace Git tests separately passed6; broker routing/budget tests passed2.
Final ACS audit used the Kubernetes Python client: two empty snapshots of four
resource kinds and404for all8known trial Pods. A broken local kubectl binary
is being replaced separately; its crash was not counted as a successful query.

The installed launcher uses the controller-owned universe `.tmp` directory.
Using the operator's workspace `.tmp` allowed UI startup but made Forgejo
repository creation fail with permission denied; real private-repository creation
caught this, and the launcher was corrected.

Pilot main protection now requires two whitelisted approvals (`science-evaluator`
and `owner`), applies to admins, clears stale approvals and permits merges only
by `science-curator`. `configure_review_protection.py` applies and reads back this
policy without approving or merging a task. `test_curator_merge_gate.py --live` uses a disposable private local repository.
It verified explicit denial for zero/one approvals, owner bypass and stale
approvals, followed by successful curator merge after two current official
approvals. The synthetic repository was deleted and checked404. No real task
was approved or merged. Async "Please try again later" responses are retried
and are not counted as proof of an approval-policy denial.

The merge took21.04seconds on FUSE. The science launcher explicitly selects
`GEWU_FORGEJO_TIMEOUT_SECS=120`; generic clients keep their caller-selected
timeout unless this deployment override is present. Invalid overrides fail.
The local kubectl was repaired tov1.34.1 using the official Debian package;
its binary matches the official standalone SHA256 and a real ACS query passed.

The registered solver queue now passes real evaluator submission, DawnFM ACS
execution, aggregate result reading, real-Pod cancellation cleanup and installed
launcher start/stop. See [evaluation-queue.md](evaluation-queue.md). Independent
quality/semantic dispatch and arbitrary new-task image registration remain open.


## Health checks and recovery

The controller and broker must be upgraded together. The authenticated Unix socket accepts `{"op":"health"}` for service readiness and `{"op":"health","role":"author-01","cwd":"/workspace"}` for a role. These requests validate the registered role boundary without taking its execution lock or running a shell. They return protocol `role-health-v1`. Actual commands still enter the dedicated nonroot chroot and require verified descendant cleanup. Concurrent commands for the same role are refused immediately.

A role preparation failure is local to its session. It cannot downgrade every role's isolation mode. Service recovery checks run in the background even while other roles are active; they never require the entire universe to become idle.

Native model requests retry HTTP 5xx/408, 429, transport failures and timeouts with exponential delay and jitter, up to 12 attempts within 20 minutes and the remaining session deadline. Authentication and request-configuration errors fail directly. Retries resend the same model context and never repeat completed tools. If a request still fails, the controller starts another session using its existing backoff.

New sessions load a bounded excerpt of their own previous conversation, the latest working-memory checkpoint, and recent tool evidence. Startup-only failures are skipped. The current instruction remains authoritative; historical system prompts and raw request bodies are excluded. `continuation.txt`, `model-retry.json`, and the immutable original transcripts provide operator evidence. Model credentials are redacted. A manual reset preserves files and receives the same continuation on its next session.

# 已审核版本与当前 PR 的交接

作者读取 `/review-feedback.json` 的当前 commit、`prior_reviews` 和 `next_action`。curator 读取 `/review-version-handoffs.json`。历史审核完成只适用于对应 commit/task/snapshot；当前 PR 的资格和正式合并仍需分别核对。历史证据缺项应明确保留，不能补造实际 Pod 观测，也不能为更新回执 schema 重跑五次。

evaluator 的作者自测证明位于 `/evaluation/evidence/<evaluation_id>/selftest-status.json`，同目录的 `selftest-manifest.json` 绑定 commit/task/snapshot、原自测 job 和各文件摘要；oracle/nop、构建及题包检查是技术完整性证据，不是科学质量或模型正确率。文件缺失时请平台补交已有回执，不让作者为“评审没收到文件”重新运行 ACS。若另有真实题意或 verifier 缺陷，应单独列明，补交文件不会抹去这些缺陷。

重复自测同一 author/commit/task/snapshot 后，已有的具体修改意见或拒绝结论仍然适用。平台反馈的 `reused_review_feedback=true` 表示把该版本的原审核意见交给当前 job，`reviewed_job_id` 保留原评审所用的自测。作者应按意见修改源码或换思路，不再为同一份源码申请一轮预算证明或重复审核。不同源码、作者或快照不能复用；自测通过也不能推翻原有科学问题和难度拒绝。

1. 列明已审核 commit、当前 PR HEAD、task 路径、已有 evaluation ID，以及旧结论与证据缺项。
2. 用 `git diff OLD NEW -- "$TASK"` 和 `git show "$COMMIT:$TASK/task.toml"` 只看该题内容。不要导出整个仓库、全盘搜索平台代码，或逐个尝试大量历史 commit。
3. 原作者说明保留旧版本还是采用新版本，附科学契约、Dockerfile、verifier 的实际变化；curator 协调具体选择。新版本必须有自己的资格回执，旧版本的完成记录保留。未明确选择前不重复提交自测。
4. 如果确有必要导出旧题，使用新的、空的题目 staging 目录，并保持管道失败可见：

```bash
set -o pipefail
mkdir -p "$STAGING"
git -C "$REPOSITORY" archive "$COMMIT:$TASK" | tar -x -C "$STAGING"
```

`STAGING` 使用角色可写的题包外目录，不能覆盖已有成果。上例得到题目本身，提交时 `--task "$STAGING" --source-repository "$REPOSITORY" --source-task-path "$TASK" --commit "$COMMIT"` 才能对应；`--commit` 不会自行切换上传内容。平台必须先验证字节与执行位，再用该内容诊断网络或构建。旧 worker 对错版本上传给出的网络报错不能作为目标 commit 的缺陷证据。

交接写清下一步负责人后等待新的决定或反馈。不手改 generation、科学接收状态、历史预算和冻结快照。

自测提交后，job ID、镜像 digest、回执及交接说明保存在题包外的工作记录中，并明确引用被测 commit。不要为了补写运行证据而改动冻结题包里的 `authoring/completeness-review.json`、README 或其他文件；这些文件也参与快照摘要，修改后就是另一个版本，旧通过结果不能自动覆盖它。如果已经产生纯证据变更，向 curator 列出被测版本、当前 PR 版本和仅该题的 diff，明确要求交接原被测版本，不靠反复重测追赶自己写入的回执。

已通过的 job 如果提示 `review_request_required`，先核对原 `idea_id` 和更早自测的真实迭代链，再运行：

```bash
role-acs-selftest request-review "$JOB" --previous-job "$PRIOR_JOB" \
  --iteration-note '根据前次自测的具体结果，说明本次已提交的实际修改'
```

它只补充审核意图及迭代关联，不构建镜像、不调用 ACS 或模型、不修改原 request/status/build、commit 和快照。查询返回的 `review-...` 请求 ID 或原 job 的 `review_handoff`，等待派单；不要再调用 `submit` 追赶交接状态。旧 job 必须本来就绑定 idea，前次 job 必须是同一作者、同一题目的已完成容器自测。原记录缺 idea 时不能追溯补造，构建失败也不能冒充已完成容器测试。

交接成功不等于科学接收。`completed` 是流程状态，`review_recommendation` 是 evaluator 建议，`formal_decision` 是正式接收记录；缺失建议的旧回执显示 `unrecorded`，不能根据默认的 `scientific_acceptance=false` 推断拒绝。预算未知、历史轨迹或实际 Pod 信息缺失等限制应保留；补交审核不能替代真实迭代、oracle/nop 和固定版本证据。

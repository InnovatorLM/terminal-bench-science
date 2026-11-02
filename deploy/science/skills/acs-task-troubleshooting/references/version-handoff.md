# 已审核版本与当前 PR 的交接

作者读取 `/review-feedback.json` 的当前 commit、`prior_reviews` 和 `next_action`。curator 读取 `/review-version-handoffs.json`。历史审核完成只适用于对应 commit/task/snapshot；当前 PR 的资格和正式合并仍需分别核对。历史证据缺项应明确保留，不能补造实际 Pod 观测，也不能为更新回执 schema 重跑五次。

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

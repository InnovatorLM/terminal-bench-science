# 旧评测证据回执补全（2026-11-03）

旧结果可能已经保存了原始轨迹、脱敏评审包和执行绑定，却缺少新版索引要求的发布 manifest 哈希等字段。`deploy/science/refresh_evidence_receipt.py` 用于核验这些已有证据并补回执；它不重新执行题目，不重新打分，也不调用模型或 ACS。

## 操作

默认仅预览，不更新结果回执；`--apply` 才写回。需要 operator/root 身份，输入已有队列配置及一个已存在的 job ID。

```bash
PYTHONDONTWRITEBYTECODE=1 \
TMPDIR=/mnt/data/zhanglinfeng/workspace/.tmp \
/mnt/data/zhanglinfeng/workspace/acs-harbor-test/.venv/bin/python \
  /mnt/data/zhanglinfeng/workspace/gewu-multiverse/deploy/science/refresh_evidence_receipt.py \
  --queue-config /absolute/workspace/path/queue-config.json \
  --job-id <32-character-job-id>
```

确认输出为 `would_refresh` 后，在相同命令末尾加 `--apply`。成功返回 `refreshed`，重复执行返回 `unchanged`。多角色队列必须唯一定位到一份公开结果，存在歧义时拒绝。

常见结果：

- `skipped_active`：原 worker 仍存在，或 controller PID 仍存在（包括不能排除 PID 重用的情况）。
- `skipped_mock`：不是实际执行记录。
- `skipped_historical_delivery`：已有 `historical_evidence_delivery`，由历史索引路径展示其缺项，不进行升级。
- `refused`：证据、版本、内容哈希或回执发生冲突。不会覆盖冲突，也不会启动补跑。

预览及应用均与证据 publication recovery 共用 job 级锁；预览可能创建该锁文件，但不创建补全备份、改写结果或原始证据。

## 核验与写入边界

写入前检查：

1. job、claim、private/public 结果身份一致，两份结果内容一致；任务和 operator 文件均位于工作区且拒绝符号链接。
2. 注册 source snapshot 的内容哈希、冻结模板哈希一致，原始执行配置对应注册模型、镜像和数据集。
3. 沿用旧 `evidence-manifest.json`，核对其原有 `evidence_manifest_sha256`；不调用新版 `inspect()` / `aggregate()` 重建旧证据或更改摘要。
4. canonical run ID、report/config/execution-binding 身份、原始证据摘要及 cleanup 状态相互一致。
5. 原始 evidence manifest 中的所有文件逐一通过 `evidence_files.digest_file` 流式校验。
6. 已发布 manifest 中的每个文件核对发布哈希和字节数；`source_sha256` 对应同一个原 trial 的同路径原文件。已绑定的 published manifest 或 execution binding 哈希不匹配时拒绝覆盖。

只补正以下字段：

```text
run_id
raw_trajectory_retained
native_session_retained
sanitized_native_session_exported
published_evidence_manifest_sha256
execution_binding_sha256
raw_trajectory_exported = false
```

分数、status、错误分类、missing_evidence、cleanup、source_version_bound、scientific decision，以及原始和已发布文件均保持不变。失败或超时任务也可以补全可验证的局部证据索引：例如只有 native session 而没有规范化轨迹／verifier 时，保留对应缺项与原状态，不升级为完整评测。

原始 private/public 回执字节和预定新回执保存在：

```text
<job>/evidence-receipt-refresh/
  private.before.json
  public.before.json
  result.after.json
  journal.json
```

目录为 0700，文件为 0600。两份结果分别原子替换并保留各自的 mode/gid；如果中途退出，下次只允许从该事务明确记录的旧／新字节恢复，遇到新评分或其他外部改动立即拒绝。成功后再次运行不会生成重复备份。

## 验证

`test_refresh_evidence_receipt` 覆盖正常补全、预览和幂等、旧字节备份、原始证据不变、partial timeout、active/historical 跳过、既有哈希冲突、原始和发布文件篡改、source_sha 关联、源快照变化、符号链接、两份写入中断后恢复、冲突评分保护、流式读取和多队列歧义。

12 项测试全部通过，43.738 秒；Python AST 与 `git diff --check` 通过。

```bash
# 工作目录：gewu-multiverse/deploy/science
PYTHONDONTWRITEBYTECODE=1 \
TMPDIR=/mnt/data/zhanglinfeng/workspace/.tmp \
/mnt/data/zhanglinfeng/workspace/acs-harbor-test/.venv/bin/python \
  -m unittest -v test_refresh_evidence_receipt
```

此外，对 `.state/evidence-acceptance-4e838c1a95/queue-config.json` 下 job `62275c95ea204b83833e9694c7af1710` 调用纯只读核验函数 `verified_fields()`：原始及发布证据哈希均通过，可补全 raw/native 留存和 sanitized native 导出字段。此次核验写入为 0，没有调用应用入口或迁移现场结果。

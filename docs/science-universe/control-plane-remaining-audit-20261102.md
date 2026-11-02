# 控制面剩余事项审计（2026-11-02 08:05–08:10 UTC）

宇宙已运行，self-test 已配置 10 槽；主要阻塞已经从单 worker 排队转为依赖下载、版本交接和历史计时门检。当前不能称为所有问题已解决，也不应为了补历史证明重复运行已通过的题目。

本次读取 [问题清单](control-plane-bug-audit-20261102.md)、[逐项验收](control-plane-completion-20261102.md) 并抽查真实运行状态；没有调用 ACS/模型、修改角色或历史回执。以下是观察时点的快照，不能替代后续实时状态。

08:42 UTC 后续：下文建议的完成时刻截止、独立送审补充和有限历史 unknown 审计已实现并通过针对性测试，正在暂停部署；历史原始记录保持不变。详见[维护记录](selftest-handoff-maintenance-20261102.md)。

路径缩写：`R=/mnt/data/zhanglinfeng/workspace/.tmp/gewu-terminal-bench-science-runtime-live`，`S=R/.state/gewu-multiverse`，`J=S/author-selftest`。该工作区实际位于 CPFS，目录名称 `.tmp` 不代表本地磁盘。

## 已完成与未完成的边界

| 范围 | 本次核验及剩余边界 |
| --- | --- |
| 服务与并发 | 本地 Forgejo 3000、网页 8765 均 HTTP 200；控制器 PID 存活，生产 worker 参数为 `--workers 10`。observer 进程参数为 3600 秒。既有十路 ACS 压测及新证据链路验收见 completion，本次未重跑。 |
| 角色与评审 | author-03/07/10 的 schedule-state 为 running，存在新会话。`review-requests` 无请求，review ledger 仅有 author-10 的旧版本 completed；没有新的审核任务已正式进入 evaluator 的证据。 |
| 轨迹与备份 | 既有 55.11 GB 独立归档及抽样恢复已验收；本轮周期仍是 partial。08:09 的状态为 verified=63,105、remaining=22,187、priority_remaining=419，`last_success_at=null`。这是全量覆盖未完成，不等同新轨迹全部没备份。当前两条 priority 警告分别是消失的 current-tool 标记和作者临时源码文件。 |
| 软件修复 | 源版本先验、verifier 缺 reward/真实传输错误分类、证据发布、暂停/reset 和新派单消费者已有针对性测试；软件验证不能替代尚未自然发生的生产暂停/恢复行为。此次检查时 Git HEAD 为 `326cf51`，工作树仍有修改，最终备份、提交与远端同步由集成人员完成。 |

## 当前真正阻塞

### author-06：新的作业仍在构建阶段下载超时

新作业 `4733c59cffd44a2bb532c06ccb9290fe`，commit `fdbaa3565984845b5c30570c5ee8c459a2d14a1d`，snapshot `189097035560b5795ca274846ebf3680c3bd8a4d3e078f1f707119f8f03f7f7a`。源校验及网络预检通过，但 `J/<job>/build/docker-05.log` 显示：

```text
RUN pip install --no-cache-dir --retries 15 --timeout 180 numpy==1.26.4
Downloading ... cp312 ... x86_64.whl (18.0 MB)
7.3/18.0 MB 20.6 kB/s
ReadTimeoutError: HTTPSConnectionPool(host='files.pythonhosted.org', port=443)
... returned a non-zero code: 2
```

这是新日志，区别于旧作业在 0.5 MB 处失败。增加 timeout/retries 后仍失败，不能继续只让作者调重试次数。平台下一步应验证构建容器的依赖交付路径，提供固定版本、目标 ABI 匹配且实际验证过的 wheel 缓存/完整 wheelhouse。仅凭日志不能定位到远端 CDN 或本机出口，也不能归因 ACS、NumPy 兼容性或 CPU/内存。

### author-07：通过自测后，真实预算缺口与后续上传错误叠加

`93d0b726f7f34bb58bf9d7c6263ebdbb` 的 `2ad9bb57ab6737fe64e72cabb6f7e2434fe0de0b` 已 selftest_passed，并明确 request_review、关联前次 `89e10ff281544ca598974a9275a83baa`、说明实际修订。`handoff.json` 为 `waiting_platform / iteration_observation_history_gap`：first_observed_at 晚于同 idea 最早自测，尚无真实历史观察补齐它。

随后作业 `05ad3210730b46a8a76dea81fe6acf88` 是上传内容与声明 commit 不匹配，不能用它覆盖前一版已经通过的事实。角色根 `review-feedback.json` 在此最新上传发生前，已被 PR #49 的泛化 `waiting_author / author_container_preflight` 覆盖。这使作者同时收到“等平台、无需重跑”和“还要补自测”两种反馈，是需要统一的控制面问题。

下一步：按 job/commit 保留具体平台等待的优先级，将 PR 泛化提示列为另一个版本的状态；先处理已通过版本的历史预算审计，不要求作者重命名 idea 或再次跑 ACS。

### author-03：多个版本的状态不能合并

- `95589e219dff43e6badf567d293ea25a` / `7bb7a227...` 已通过且要求送审；角色回执明确 `iteration_original_start_conflict`，当前记录起点比旧同 idea 自测晚约 10 小时。作业目录内 `handoff.json` 仍是泛化 ValueError，而角色投影更具体，应避免不同视图互相覆盖。
- `f775651c394549fc88e844f8e98c4743` / `94777228...` 已通过，但未 request_review，故是 `review_request_required`。
- `1470dd6b...` 是源版本不匹配；最新 `75c5f356382140d49975a5802ff41651` / `1c000b25...` 已经通过源校验，但新 task.toml 的 verifier `no-network` 不受当前 ACK 支持。不能把这个真实的新配置错误套到之前通过的版本。

下一步：先选定要审核的已通过快照，再分别处理正式申请与历史预算审计；新增源码的网络策略问题按该版本修复。不能为消除平台历史计时缺口继续改题。

### author-10：审核完成的是旧版本，当前新版本还没有合法交接

ledger 的 completed 仍绑定 `5ead0fe40dc608e0978550b1b576cbbdb32940fc` 和 snapshot `4fbe6be6...`，历史五次结果 3/5 不变。PR #42 仍由 dispatcher 记录为 `898743be...` 的 version_reconciliation，不能直接继承旧结论。

新 commit `5c9277c8fcdb8d052487d1f86ac36e0d72356066` 在 `73c220ec47444a2ab5e4e3f5e9c93815` 已 selftest_passed，但没有 request_review。随后 `7606ae51a3e24c399f0add8b2bc910cd` 申请审核时，把镜像构建失败的 `162ad830...` 当成前次已完成容器试跑，被 `selftest_iteration.validate` 拒绝。因此目前既不是 evaluator 仍在审核，也不是新版本科学质量已被接受。

下一步由作者/curator 明确选择旧审核版本或新版；若选择新版，提交真实符合条件的迭代关联与版本差异，不转用旧分数。平台可提供“对既有 passed job 追加独立、不可变送审请求”的入口，避免仅为补 request_review 再构建和跑同一快照；原始 request 和执行回执保持不变。

## 五小时规则是否被过度实现

用户要求是：题目迭代超过五小时（ACS 时间不算）仍未完成，或无法保证难度，就换思路。它是对继续投入的约束，没有明确要求已完成的历史题必须证明完整的五小时内时间账才允许审核。

目前代码把它变成硬准入：`selftest_handoff.qualify` 在 `selftest_budget.verify` 的保守上界不合格时抛出 `BudgetEvidenceRequired`；`author_submission_gate.validate_receipt` 同时要求 `0 <= active_iteration_seconds <= 18000` 和 `budget.within_budget is True`。`verify` 用交接时刻减起点及已证明的 ACS/维护区间，而非已完成题的独立完成时刻。这样完成后等待平台也会继续增大上界，历史观察缺失则永远无法通过。这两点超出了“不要无限迭代”的实际目的。

建议有限的历史处理（方案，尚未实现）：

1. 将“题包具备正式审核条件”与“历史迭代用时是否完整可审计”分开。对迁移前的指定 role/idea/job/commit，由 operator 记录 `legacy_budget_unknown`、缺口类型、证据摘要及适用版本。`within_budget=null`、实际用时未知；不得填 0、伪造起点或把 unknown 写成通过。
2. 其他条件继续硬校验：源字节、真实作者解题、oracle/nop、结果驱动的已提交修改及复跑、不可变镜像/快照登记。预算未知只随审核包明确呈现，不自动等于题目接收，也不能使缺少这些证据的题包通过。
3. 不能给作者自助绕过；限定已存在的迁移缺口和精确版本。已明确 abandoned 或独立证据证明超时仍继续迭代的情况，不能通过 unknown 洗掉。
4. 新 idea 保持前瞻五小时干预；把完成与等待审核状态可靠记录，完成后不再计为作者迭代。未知数据应提示观测缺口，不能推定作者已超时或要求反复重跑。

实施涉及 `selftest_budget.py` 的结构化审计状态、`selftest_handoff.py` 的预算与技术资格拆分、`author_submission_gate.py` 的显式新回执模式；dispatcher 仍验证原有版本与执行证明。此次仅分析，没有更改这些代码或现有时钟。

## 不能补造的历史证据及收尾顺序

已删除历史 Pod 的实际资源/imageID 不可从请求值补造；旧 schema-2 中缺少的迭代证明不能改几个 Boolean 升级；03/07 不匹配或从未观察的事件前缀不能拼接成真实观察。保留历史文件，允许新增有来源的诊断或范围明确的迁移审计记录。

建议先解除“历史预算 unknown 导致已完成题永久排队”和反馈互相覆盖，再完成 author-10 版本选择；并行修复依赖下载及更新构建失败分类。备份继续追平并抽查恢复，最后完成当前修改的测试、代码备份、提交和远端同步。上述软件修复无需为了验收重新跑五次模型评测；科学审核仍由 evaluator 对合法冻结版本执行。

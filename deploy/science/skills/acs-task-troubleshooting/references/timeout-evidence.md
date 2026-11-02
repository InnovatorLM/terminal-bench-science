# 四小时超时的证据审查

先区分模型的解题时间与 Pod/控制器的总寿命。Pod 在准备环境时已开始计时；若它在模型预算结束前被删除，模型退出码 137 不是“题目难”的证明。137 也不能单独证明 OOM，需要 Pod 原因和资源证据。

对每个 job 独立记录：冻结 commit/snapshot、run ID、镜像 digest/imageID、实际资源和节点、开始/终止时刻、原始 session/日志、verifier 是否运行、清理回执。平台每分钟保存运行中日志，退出前再收集；`acs-collection.json` 记录最近一次收集状态。检查原始 `sessions/*.jsonl` 与 `codex.txt`，ATIF 缺失不代表从未运行。末行 JSONL 未写完应保留前面已完成事件，不能因此丢弃整条轨迹。

分类必须引用具体日志或轨迹位置：

| 分类 | 处理 |
| --- | --- |
| 题包、依赖、评分器或题意缺陷 | 交作者修订；原版本此轮不作为难度成绩 |
| 容器生命周期、网络、模型接口、工具适配器或收集故障 | `waiting_platform`；修复后由平台明确安排补跑，不反复 submit 原批次 |
| 缺关键轨迹，无法排除上述原因 | 证据不足；不猜测模型能力，不以 reward=null 计算 0 分 |
| 题目完整，运行和工具健康，轨迹显示持续有效解题直到四小时仍未完成 | 记录为难度导致的模型失败；保留原始 reward/异常，另记判定及依据，不能伪造 verifier reward |

五次运行必须分别判断，不能用其中一次的结论覆盖全部。难度超时可以按模型错误纳入五次正确率，但仍须审核科学正确性和题意/评分一致性。补跑保留被替代作业及原因；不能只挑成功结果或把缺证据的历史结果写成完整。

## 保存可审计的分类结果

evaluator 在工作区保存判断 JSON，然后调用：

```sh
role-evaluation-batch review-timeout --job <本批次job-id> --assessment /workspace/timeout-assessment.json
role-evaluation-batch status
```

JSON 包含 `classification`（difficulty / task_bug / platform_error / insufficient_evidence）、`reason` 和 `evidence`。每个 evidence 项是 `{ "path": "agent/sessions/...jsonl", "sha256": "实际文件SHA256", "finding": "引用的事件/行号及其说明" }`，路径相对该 job 的 `/evaluation/evidence/<job>/`，先实际阅读再填写。`difficulty` 还需 `observations`，用文字分别解释 `task_complete`、`environment_healthy`、`model_tools_healthy`、`sustained_problem_solving`；不能只填 true 或复制“运行正常”。

平台核对实际 agent 四小时运行、原始异常、模型、冻结版本、Pod/清理证据及所引轨迹摘要。控制器/Pod 提前退出、短于四小时、缺实际资源或缺关键轨迹的运行不能用 difficulty 放行。观察判断仍由 evaluator 对实际轨迹负责；字段齐全本身不证明科学难度。

原始 result/reward 保留，判断写入 `/evaluation/timeout-reviews/<job>.json`。重复相同判断幂等；改变判断需平台保留原记录后纠正，不能覆盖历史。`platform_error` 标明需要有理由的补跑，不自动生成新任务；`task_bug` 交作者，`insufficient_evidence` 等补证据，三者都不计模型错。历史结果没有新证据字段时先请求平台核对现存原始材料，不为补字段自动重跑。

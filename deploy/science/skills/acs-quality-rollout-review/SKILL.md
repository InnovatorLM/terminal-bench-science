---
name: acs-quality-rollout-review
description: Independently evaluate a frozen Harbor task with five ACS rollouts and inspect trajectories and verifier evidence.
---
# 五次独立 ACS 质量评测

仅 evaluator 使用，先阅读 /skills/harbor-acs-evaluation/SKILL.md。依据 /home/role/review-assignment.json 中的具体任务和 commit 工作；无派单时等待，不选择任意旧题试跑。

使用 /evaluation/catalog.json 中对应本次版本的已注册项；向 /evaluation/inbox 写请求，通过 /evaluation/results 读取结果。请求 schema 为 job_id、evaluation_id、snapshot_sha256，job_id 为32位小写十六进制。注册目录没有本次版本时报告 waiting_platform，请求注册，不猜测 ID，不伪造结果。

已部署 `role-evaluation-batch` 时，完成完整性检查后用 `role-evaluation-batch submit` 一次提交本次派单的五个独立并行job，再用 `role-evaluation-batch status` 查看汇总。重复submit沿用相同批次，不生成额外作业；不得手改generation以绕过失败。只有五份有效、版本一致且轨迹、verifier、运行与资源证据及清理确认完整的结果才能形成正式结论；缺证据标记 incomplete_evidence，不因已有 reward 就跳过。无效结果先诊断并请求平台补跑；汇总阈值不等于科学质量接收。旧部署缺少命令时报告waiting_platform，不自行安装凭证或猜测执行ID。

先做完整性与 runtime/工具调用 smoke。之后用指定 dawnfm / deepseek-v4.1-flash-1017 独立运行五次，默认4c/8Gi、资源不足最高8c/16Gi、不限解题步数、每次4小时。检查实际返回配置，不能只因本文件写了就认为生效。

保存每次 result、agent轨迹、verifier输出、镜像digest、run ID及清理证据。基础设施/适配器失败或无verifier结果不能算模型答错；先修复补跑。四小时超时先查题目缺失与环境缺陷；排除后确属难题才计模型失败。有效通过率<=60%可接收，但必须结合科学正确性、题意/测试一致性、捷径检查和轨迹审查，不能用五次全错自动证明高质量。

作者自测不等于独立评测。只对所派不可变版本输出结论；用 role-review-status 记录状态（先看 --help），平台问题与作者问题分开交接。

## 题目难度与现实来源（作者和审核者共同遵守）

- 难度反馈统一使用 `deepseek-v4.1-flash-1017` 的五次独立有效评测，可以并行。有效正确率大于等于 80%（五次中至少四次通过）时严格拒绝，不能以其它优点抵消此难度门槛。有效正确率小于等于 60% 才满足当前难度接收条件，仍须通过完整性、科学正确性和质量审核。基础设施错误不算模型答错；四小时超时先排查题目或环境缺陷，排除后确属科学难度才计作模型错误。
- 题目必须有符合真实世界的案例；每一道题都应尽可能从真实论文、数据、代码、软件或实际科研工作流中取得 source data 并构建。说明原始来源、版本、实际输入及加工过程，提供可核验的 URL、DOI、commit 或数据标识。不能只加一个现实背景故事而让核心案例、输入和问题完全架空；缺来源时先补证据，不捏造来源。
- 真实问题驱动的模拟、合成、裁剪或重采样可以使用，但必须明确标注，并说明与现实问题和真实来源的联系，不能冒充真实观测。完全虚构的题目原则上不能接收；只有其它方面确有特别突出的价值时才可作为例外，作者须明确申明，审核者须在 review 中记录具体理由和证据，不能默认放行。例外不豁免难度及基本质量要求。
- 作者在提交前自行检查现实来源与难度设计，并完成实际自解、容器自测和基于结果的迭代；审核者独立核对来源与案例关联，在五次有效结果齐备后执行上述难度判定。平台启动验收使用已有简单题目，不以该验收题是否满足新题难度门槛阻塞启动。

异常处理与等待去重见 `/skills/acs-task-troubleshooting/SKILL.md`。不要把缺证据当作 ACS 不支持轨迹，也不要通过重复 submit 修改批次组成。

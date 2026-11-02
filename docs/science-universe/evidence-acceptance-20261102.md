# ACS 证据交接验收（2026-11-02）

修复后的真实模型→独立 verifier→evaluator 证据交接已通过。验收目录：工作区 `.state/evidence-acceptance-4e838c1a95/`，完整核验记录为 `acceptance-verification.json`。

本轮共调用两次指定 DawnFM / deepseek-v4.1-flash-1017 模型，均使用已有 OpenSSL 简单题。第一次暴露独立 verifier 启动前的平台缺陷；修复后经授权追加一次，完整链路通过。为覆盖独立 verifier，将该题运维验收副本声明为 separate，并显式传输 `/app/ssl` 与 `/app/check_cert.py`；题面和原始测试逻辑未改。任务镜像沿用上海 ACR immutable digest，北京 ACS i 区执行。首次失败和不调用模型的 oracle 复核证据保留在 `.state/evidence-acceptance-5b3c147128/`，未将多次运行拼接成一次成功。

## 完整真实验收

- Job：`62275c95ea204b83833e9694c7af1710`；run：`tbs-universe-linfeng-queue-62275c95ea204b83833e9694c7af1710`。
- Codex 0.164.0、指定 DawnFM 模型；`solver_complete`、真实 verifier reward=1、n_errors=0、evidence_complete=true。
- 原生 Codex JSONL session、12 步 ATIF、verifier reward 和输出均落地。队列自动发布含 10 个被哈希文件的 evaluator packet，没有人工补发布。
- 快照 SHA-256、run ID、配置与证据 manifest 的绑定均通过；源快照运行前后相同。
- runtime 与 separate verifier 两个实际 Pod 均记录了 imageID、镜像引用、UID、4 vCPU/8 GiB requests/limits、30 GiB 临时存储和 `virtual-kubelet-cn-beijing-i` 节点；未拿配置申请值冒充 Pod 回显。
- guard 清理确认通过；事后再次查询本轮标签为空，两个已知 Pod 均返回 404。
- 使用真实 UID 验证 evaluator 21012 可读取全部 packet，author 21010 无法读取；原始运行目录为 0700，packet 为 evaluator 专属 0750/0640；已知凭证值扫描通过。
- 23 项实际核验全部通过；19 项证据、队列、五次汇总及 verifier 传输单元测试通过，适配器两份源码一致、Python 语法和 diff 格式检查通过。

这是一个已有简单题的执行与交接验收，不代表新题科学质量通过，也没有启动生产宇宙、作者积压队列或五次模型批量评测。主线仍需完成合并和部署。

## 已发现并修复

- 轮询可能漏掉短暂 verifier：`UniverseACK` 在启动后、删除前同步采样 Kubernetes 实际 Pod，记录 allowlist 字段到 root 私有 journal；guard 合并并按 UID 做清理确认。采集失败不阻止删除，也不伪造完整证据。
- 真实模型执行完成、原始轨迹保存后，独立 verifier 因 ACK 要求本地 `tests/Dockerfile` 而在创建 Pod 前失败。执行已发布 digest 镜像不需要该文件。适配器现在只要求环境目录存在及经构造器验证的 immutable image。原失败保留为 infrastructure_failed，无 verifier reward，不计模型失败。
- 显式区分 shared/separate：shared verifier 使用运行容器，不能要求观测一个未启动的 verifier Pod；separate 则必须实际观测两个角色。
- mini-swe 私有轨迹使用身份、镜像、run ID 和 SHA-256 验证后，桥接到同样 root 私有的 trial 目录，避免保留了轨迹却始终报缺失。
- evaluator packet 同时发布脱敏 native Codex 日志/session 和规范 ATIF；不再仅发布归一化轨迹。
- 队列不再把 `AgentTimeoutError` / `AgentExecutionTimeoutError` 自动归为基础设施失败。返回 `agent_timeout_requires_cause_review`、reward=null；即使统计夹带旧 reward 也不计分，五次汇总保持未完成，等待明确原因判定。

## 历史结果恢复

`historical-packets/` 已发布 iod-enso-attribution 五次历史脱敏 packet，原 reward 为 1、0、0、1、1，保留 3/5。五次都有真实轨迹（20、19、29、20、27 步）及 verifier 输出、cleanup confirmation。旧 run 没有 Pod 实测，明确标为 `historical_incomplete_pod_observations`，没有补造 imageID/node/resources。

权限以真实 UID 验证：evaluator UID 21012 可读 packet，author UID 21010 不可读。结果见 `packet-permission-check.json`。

## 验收边界

第一次模型评测触发平台缺陷，其状态保留 `infrastructure_failed`、reward=null。其后的 `oracle-report.json` 只复核 Pod 实测和独立 verifier。完整链路成功来自本报告首节的第二次真实模型运行，没有将 oracle 与失败模型的证据拼接。


## 修复后的实际复核结果

首轮目录的 `oracle-report.json`：标准解 reward=1，Harbor exit=0、无 trial exception，runtime 和 separate verifier 都捕获了实际 imageID、4c/8Gi requests/limits 和 `virtual-kubelet-cn-beijing-i` 节点。guard 对两个已知 Pod 身份均确认 404，所有本次标签资源为空。oracle 自身未调用 LLM。

原模型失败的真实轨迹（含 Codex native session）已另外发布 diagnostic packet：`role/evaluation/evidence/26c086f3b7f14e2f89459cf84dee0e55/`，manifest 明确 trajectory_available=true、verifier_available=false；生产队列现在也发布这种不完整 diagnostic packet，便于 evaluator 排错。

一个需要主线注意的接口事实：当前 Harbor 解析 verifier 的 shared/separate 来自 task.toml；单纯设置 job 配置 `verifier.environment_mode` 不会改变任务模式。之前十路探针原 task 没有 separate，因此只观测到执行 Pod，不代表 separate verifier 已被验证。本次运维副本显式在 task.toml 声明 separate 后才覆盖到该路径。

进一步核对发现，job 的 `verifier.environment` 同样不属于当前 Harbor 运行配置 schema，会被忽略。self-test worker 已移除这两项无效覆盖，并在构建前使用真实 Harbor schema/resolver 与实际 UniverseACK capabilities 检查任务/step 的模式及 phase/baseline 网络计划；不修改冻结任务。默认 public/shared 和源任务声明的合法 separate 均可通过。当前适配器未支持的静态断网/白名单或动态切换会返回 `task_acs_network_incompatible`、具体字段及处理建议，且 `cloud_execution_started=false`；不能仅靠 separate 声称已提供隔离。

网络预检的 18 项针对性测试通过，包含真实上传路径在进入 builder 前拒绝不兼容配置、冻结内容哈希不变、默认 shared、源配置显式/隐式 separate、legacy flag、step 继承和现有 worker 恢复测试。两份 pipeline/troubleshooting skill 已更新并通过格式校验。本项未增加云端或模型调用。

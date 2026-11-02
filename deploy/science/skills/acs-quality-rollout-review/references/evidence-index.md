# 逐次 ACS 证据索引

在已提交批次的 `role-evaluation-batch status` 中读取 `evidence_index`。
每个条目对应一个固定 job；未提交或等待中的项也保留，不能用另一批次补位。

| 字段 | 判断用途 |
| --- | --- |
| `job_id`、`run_id`、`snapshot_sha256` | 与本次派单、冻结题包和执行相对应。 |
| `evidence_packet` | 本角色内 `/evaluation/evidence/<job_id>`，包含脱敏后的完整证据；不读取宿主私有路径。 |
| `raw_trajectory_retained`、`native_session_retained` | 平台原始 ATIF 轨迹和 Codex 原生会话的留存回执。 |
| `trajectory_available`、`sanitized_trajectory_exported`、`sanitized_native_session_exported` | 对应脱敏文件已交付，可在 packet 中阅读；日志存在不等于有效 ATIF 轨迹。 |
| `runtime_image`、`runtime_digest`、`verifier_image`、`verifier_digest` | 执行身份中的不可变镜像；结合实际 `observed_containers` 检查，不能只看配置。 |
| `observed_containers` | 实际观察到的 Pod UID、node、imageID、requests/limits。shared verifier 使用 runtime Pod，不要求虚构第二个 Pod。 |
| `verifier_available`、`pod_evidence_complete`、`cleanup_confirmed` | 分别检查评分输出、实际容器回显和精确清理。 |
| `missing_evidence`、`evidence_errors` | 原始收集缺项，及 packet 的缺文件、版本或索引校验问题。 |

`packet_index_verified` 只证明发布清单/执行绑定匹配、文件存在且大小一致。
状态查询不会每次重读数小时的轨迹；正式接受阶段还会逐文件核验原始与发布副本的哈希。
索引不是科学审核结论，也不能代替阅读轨迹和 verifier 输出。

若 `historical_packet=true`，旧批次可能只有补回的轨迹和 verifier。
`execution_identity_verified=false` 或缺实际 Pod 记录时如实说明缺口；不能将
镜像配置当作实际资源回显，不能编造已删除 Pod 的数据。已由运维明确接受的历史
版本保留原结论，新版本仍按当前证据门检；不要为了补字段重跑或撤销旧版本。

缺证据先 `waiting_platform`，提供具体 job 与缺项，请求补收集现存文件。
已完整保存的证据无需再跑模型；确实无法恢复时由平台决定该版本的补测方式。
超时仍按 troubleshooting 中的 timeout-evidence 逐次分类，原始 reward 保持不变。

---
name: acs-task-troubleshooting
description: Diagnose a blocked Harbor task build, self-test, review handoff, or ACS rollout from its version-bound receipt; choose author repair or platform wait without duplicate jobs.
---

# ACS 题目故障处理

作者、evaluator、curator 在提交失败、等待超时或结果异常时使用。先读 `/capabilities.json`，再读取当前 job 的回执与脱敏诊断；安装了 skill 不等于服务在线。作者用 `role-acs-selftest status <job_id>`，并读取其 `diagnostics` 指向的 `/selftest/results/<job_id>.diagnostics.txt`；evaluator 用 `role-evaluation-batch status` 和本次派单的私有证据。不要打印凭证或读取别的角色工作区。

遇到本地演练的 `/app`、`/tests` 或 `/opt` 路径问题、pytest/CTRF 插件、上传内容与 commit 不一致时，读 [本地演练与题包预检](references/package-preflight.md)。这些问题需要区分角色环境和实际题目镜像，不应通过反复换 Dockerfile 猜测。

## 先确定是哪一段失败

| 证据/状态 | 谁处理，下一步 |
| --- | --- |
| 缺 `instruction.md`、`task.toml`、`environment/Dockerfile`、`tests/test.sh` 或 `solution/solve.sh`，schema/路径/执行权限错误 | 作者补齐已准备试跑的题包，检查 shell 语法、输入输出和引用文件后提交新版本。尚在研究的提案不要求已有完整题包。 |
| `source_git_tree_verified` 未通过、上传与 commit 不同 | 作者确认完整 commit、仓库相对路径、文件内容和执行位一致。不要拿另一个 task 的登记绕过。 |
| `waiting_builder`、`building`、`running_oracle`、`running_nop` | 当前 job 正常等待或运行。保留 job ID，按状态变化检查；不要反复 submit、重建相同快照或清空状态。等待不证明死循环。 |
| Docker 构建日志中的包缺失、编译失败、无效 Dockerfile | 作者看最早失败步骤，修复该依赖或构建上下文，先做小规模验证再提交。不能只看最后一条命令的退出码。 |
| `Require fresh workspace runtime context`，或平台拒绝自己的输出路径 | 平台存储路径/构建器问题。附 job、commit、具体错误，记录一次 `waiting_platform`；作者不应靠重命名题目或同版本重试规避。 |
| `manifest unknown`、digest 不存在、基础镜像解析失败 | 核对哪个镜像及哪个阶段失败；作者修复自己写错的镜像引用，平台修复冻结/发布/镜像解析失败。成功回执绑定 digest，不能用可变 tag 猜测替换。 |
| `unauthorized`、`ImagePullBackOff`、DNS/TLS/连接超时 | 平台核查 Pod events、pull secret、registry 和实际网络路径。公共 registry 地址本身不是失败证据；不能仅凭报错断言 ACS 或 ACR 故障。 |
| `task_acs_network_incompatible`，stage=`network_preflight` | 构建前已发现源 `task.toml` 与当前 ACK 适配器不兼容，尚未启动构建/ACS。读取 `network_preflight.issues` 的具体字段和 phase/baseline。默认公网 shared 可用；当前适配器不支持运行中切换，也不支持静态断网/白名单。`network_mode="no-network"` 与 `allow_internet=false` 都表达断网，互换字段不会解决问题；独立 verifier 的 `[verifier.environment]` 也一样。题意允许公网时在源任务的对应环境明确选择 `network_mode="public"` 并去除冲突的 false 声明，提交新版本；确需隔离时交平台实现支持，不能谎称已隔离。 |
| `platform_dynamic_network_policy_mismatch` | 实际运行到了 Harbor 不支持的阶段策略切换。核对源任务的 `[environment]`、`[agent]`、`[verifier]` 及 step 配置；若新的预检未拦住，同步向平台提供 job/version 和失败阶段。独立 verifier 只能避免共享容器的阶段切换，不能提供适配器未支持的静态策略。 |
| `rewards=null`、裸 `ValueError`、verifier 没输出 | 尚无有效分数。读异常类型和最后失败命令：可能是题包/verifier缺陷，也可能是平台；不能一律归 ACS，也不能算模型答错。 |
| oracle=0 或 nop=1，且运行无基础设施异常 | 作者检查参考解、artifact传递、判题标准/容差和泄漏。nop通过意味着负对照异常；不是题目“太难”。 |
| OOMKilled 或明确资源耗尽 | 由平台核实后从 4c/8Gi 升到最多 8c/16Gi；一般计算慢或错误答案不构成扩容证据。磁盘不足单独检查镜像/解包与临时存储，不能用加CPU冒充修复。 |
| `selftest_passed` 但 `review_handoff` 是 `waiting_author` | 按 stage/reason 补真实作者自解、结果驱动修改及新 commit 复跑；首次自测不证明已完成迭代。 |
| `selftest_passed` 但 `review_handoff` 是 `waiting_platform` | 保留通过的原 job，提交登记/证据缺项一次，等待平台修复。不要为补交接重复跑相同自测。 |
| 五次 reward 已有但缺 trajectory/imageID/资源回显 | 是证据未闭环，先请求补收集/登记现存文件。配置请求不等于实际Pod回显，历史已删除Pod的信息不能编造。 |

## 等待与交接

当前 Harbor 的 shared/separate 来自题包 **task.toml**。`[verifier] environment_mode="separate"` 或显式 `[verifier.environment]` 才能选择独立环境；job YAML/JSON 的 `verifier.environment_mode`、`verifier.environment` 会被忽略。默认 shared/public 不需要改。选择 separate 时，题目输出必须通过 `artifacts` 或 `/logs/artifacts` 传递，再用 oracle/nop 验证；不要假设两个容器共享整个文件系统。修改模式或网络声明必须在源任务中形成新 commit，不能静默篡改已冻结版本，也不能靠删测试改变题意来绕过。

记录 task 路径、完整 commit、job/run ID、状态、首次失败步骤、已尝试的具体修复，以及下一步由谁处理。避免整段日志贴到公开 issue。curator 汇总同根因平台故障，不替代作者修题或 evaluator 独立评测。evaluator 可用 `role-review-status waiting_platform --stage <阶段> --reason '<具体证据缺项>'`，作者问题使用 `waiting_author`。终态回执受版本/代次约束，后续恢复由 dispatcher 派新代次，不能手改 generation。

没有新结果或反馈就结束当前会话并等待，不重复发表评论、反复探测安装或重新派单。作者收到当前版本进入审核的反馈后暂停出题，等审核结果；不要把正常评测等待当作卡死。平台故障修复后由明确恢复事件继续。

先核对当前 commit/job 和已完成审核的版本，再使用旧诊断。旧自测失败不会推翻后来同一固定版本的正式评审；旧版回执缺新 schema 字段也不表示应重跑五次。保留已完成结论和具体证据缺项，新增版本才按当前门检提交。

## 评分与时间边界

作者 self-test 是 oracle/nop 对照，不调用解题模型，也不能作为五次正确率。正式评测由唯一获派 evaluator 对同一冻结版本运行五个独立 `deepseek-v4.1-flash-1017` job，可并行，默认 4c/8Gi、不限步数，每次 4 小时。有效通过率 <=60% 才满足难度门槛，>=80% 严格拒绝；质量还要核查科学正确性及现实来源。

满 4 小时没有完成时先排查缺文件、环境/模型接口/工具故障、verifier缺陷。排除这些原因后确属题目困难的超时，记录依据并按模型错误计分，可作为难度的正面证据；不要伪造原始 verifier reward。未经分类的 timeout 或缺轨迹只能保留未完成证据，不能直接算错或宣称高质量。

作者同一思路主动迭代超过 5 小时（扣除有平台记录的 ACS 时间），或已无法保证难度，应记录失败经验并换思路。不得重建 idea ID、清空历史或重复提交同版本来重置计时。

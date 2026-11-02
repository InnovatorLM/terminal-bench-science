# 恢复后 self-test 处理与 OpenSSL 对照诊断

本次只统计当前启动 `1793597319` 之后的新请求；平台验收继续使用已有的 `openssl-selfsigned-cert`，不创造新题，不修改冻结题目或难度，不重复五次模型评测。

## 已确认的处理情况

首次采样发现五个新请求均被处理：四个在本地网络能力门检拒绝，一个已完成 official/runtime 镜像发布后在 verifier 镜像构建失败。不是 worker 没有启动或所有请求仍在排队。

- author-03 的 `4bd0be56231c422491df29f0fc87a1be` 声明独立 verifier，`[verifier.environment] network_mode="no-network"`。后续 `179fca1be2ad4a71b8cc09ff1ffe7006` 改成 `allow_internet=false`，解析后仍是同一种隔离要求，不能绕过能力门检。
- author-04 的 `7e936772669d4a9e84ee7236e2528784` 和 author-07 的 `d998a8f3853b4001b3d4260bd434055b` 也显式声明独立 verifier 的 `no-network`。这四次 agent 环境均为 public；不兼容的是 verifier 启动策略。
- 四份诊断均已出现在对应角色的 `selftest/results/`，权限 0640、角色 GID 正确；最近会话轨迹有包含各 job ID 的工具返回及后续模型回复，确认作者已经收到信息。
- author-07 的 `e399a1c472e54d9faf2af630b4353904` 已通过网络门检及 official/runtime 镜像构建发布。失败发生于原 `tests/Dockerfile` 安装 `pytest==8.4.1 pytest-json-ctrf==0.3.5`，pip 报 `pluggy<2,>=1.5` 的候选版本为空。完整日志没有更早的 pip 重试、索引地址或 TLS 错误，因此不能仅凭最后一行断言网络失败或依赖不存在；需要进一步检查构建环境的源配置。

## OpenSSL 原验收的两个平台缺陷

原 job `7ed555071fb4437995f421c41ac25639` 的 oracle reward 为 1，nop 没有 reward，并报 `VerifierTimeoutError`。冻结 `task.toml` 声明 verifier 900 秒，但工作器继承模板的 `override_timeout_sec=600`，实际在 600 秒取消。

oracle 的真实 stdout 显示 apt 索引下载 259 秒、软件包下载 98 秒，而六个 pytest 测试总计 0.35 秒。主要时间用于运行时安装依赖。原 nop 的 stdout 未留存，所以不能证明它具体卡在哪一次下载。

日志缺失的原因是 Harbor 只在 verifier 正常返回后下载 `/logs/verifier`；超时取消绕过了下载步骤。之前的“缺 reward 时保留已有日志”只覆盖已经调用下载函数的场景，不能覆盖这个取消分支。

## 修复与验证

`author_selftest_worker.py` 现在移除通用模板的 verifier override/max 时间限制，并使用 verifier multiplier=1，按冻结任务自身时限执行；不改任务源文件。回归通过实际 Harbor 的时间计算验证 300、900、7200 秒不会被模板的 600 秒覆盖。

`UniverseACK.stop()` 在删除 Pod 前补收尚未下载的 verifier 日志。取证总等待不超过 40 秒，远端 exec 30 秒；缺 reward 或取证失败均继续资源清理。已下载的原证据不覆盖；取证不补造 reward，不把超时改成成功。两个版本控制 adapter 保持相同内容。

17 个针对性单测通过，包括取消后真实 stop 调用顺序、缺 reward 时日志保存、取证失败仍删除 Pod、原输出保持、完整证据安全校验和实际 Harbor 时间解析。

同 digest 的 nop 精准复跑已完成，run ID 为 `tbs-universe-linfeng-nop-recovery-20261102`。配置、机械检查及原始版本绑定位于 `.state/harbor-eval/openssl-nop-recovery-20261102/`；复用原 snapshot 和 official/verifier digest，verifier 恢复题目声明的 900 秒。

实际结果：nop reward=0.0、exception=null、Harbor 退出 0、n_errors=0。CTRF 和完整 stdout 证明六个测试均真实执行，并因缺少解答产物失败，测试本体仅 0.12 秒，排除了依赖安装失败误写零分的情况。此次 apt 索引下载耗 11 分 30 秒（13.6 kB/s），包下载另耗 81 秒；与原 oracle 对比可确认下载吞吐波动显著，不能归因于模型或 CPU 核数。

guard 已确认 owned Pod 404 且该 run 的六类资源均无残留；实际 Pod 资源为 4 CPU/8 GiB/30 GiB，节点 `virtual-kubelet-cn-beijing-i`，Pod 版本与资源证据检查完整。恢复后对照组合为“原 oracle=1 + 同版本重跑 nop=0”，未新增模型调用。`recovery-summary.json` 记录该组合及范围，`evidence-manifest.json` 对原 oracle 和新 nop 的原始证据做 SHA-256 登记；历史 selftest_failed 回执没有被改写，不把本次平台验收冒充作者科学质量验收。

## iod-enso-attribution 的基础镜像解析错误

author-10 的 `b88184a6e39c4ee4813de948546c404b` 对应已有提交 `5ead0fe40dc608e0978550b1b576cbbdb32940fc`。原始 `docker-01.log` 证明 `public.ecr.aws/docker/library/python:3.12-slim` 成功 pull，本次摘要是 `sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159`；随后 inspect 的同一镜像配置附带两个同仓库 digest：`1148db…` 与 `aa943ce…`。旧解析器要求整个 `RepoDigests` 长度恰为一，错误拒绝了这份有效结果。这是平台解析缺陷，不是作者 Dockerfile 或 ACR 登录失败。

修复按请求仓库及本次 pull 的明确 Digest 共同选择，并要求 inspect 包含相同引用；Docker Hub 简写和带端口的仓库名会正确规范化。显式 digest 必须与请求、pull、inspect 一致；缺少明确 pull 证据且同仓库仍有多个候选时继续拒绝，不能猜选第一条。原始日志只读重放已精确解析出 `aa943ce…`，没有重建镜像或重跑已有题目。

解析错误现在带 `platform_base_image_resolution`、`failure_owner=platform`、`retry_requires_task_change=false` 的结构化回执；进入交接时归入 `waiting_platform`。已有 b881 的两份失败回执只追加平台诊断，仍保留 failed、原错误、时间和版本；原始字节另存 `.state/deployment-recovery-20261102/iod-b881-classification-before.json`。解析验证回执为同目录的 `base-image-resolution-regression.json`。20 项构建、Dockerfile 绑定及交接回归通过。

已核对运行中的 worker 主进程 PID 3468357：它是十槽调度器，每单通过新的 Python 进程执行同一源文件的 `--single-request`，不是在 daemon 内直接调用缓存的 `process()`；镜像构建也另起 Python 子进程。因此新请求会加载当前修复，无需重启 controller 或 worker 主进程。已启动的请求子进程仍可能保留启动时的工作器函数，不能声称它们全部热替换；本次没有打断或重启这些正在执行的任务。

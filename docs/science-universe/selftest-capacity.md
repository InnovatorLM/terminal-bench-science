# Self-test 并发与资源

2026-11-02：当前配置为 10 个按需 job 槽位，覆盖现有 10 个作者；同一作者同时只运行一个 self-test。启动器显式传入 `--workers 10`，可通过 `GEWU_SELFTEST_WORKERS` 覆盖。槽位上限不是预热容器数；没有请求时不会创建 job 子进程或 ACS Pod。

## 本机和云端开销

空队列实测：10 槽位的主 worker RSS 为 28.2 MiB，4 秒采样的 CPU 时间增量为 0，子进程数为 0。该采样只能说明空闲控制进程很轻，不能代表构建或运行任务时的开销。

任务的 oracle、nop 按顺序运行，不调用 LLM。ACS 每个 Pod 默认请求并限制为 4 vCPU、8 GiB 内存、30 GiB 临时存储。十个这样的 Pod 合计为 40 vCPU、80 GiB 内存和 300 GiB 临时存储请求。这些资源在 ACS 上分配，不从开发机的 8 CPU/32 GiB 内存中分配。独立 verifier 若与执行 Pod 重叠，需要按实际 Pod 数另计。

并发提高同时使用的云资源。相同任务若运行时长、资源规格和重试次数相同，总资源时长通常相近；不能据此保证账单完全相同。当前没有区域单价和完整账单数据，不给出金额估算。

镜像构建仍使用一个共享槽位，避免多个构建同时争用 Docker 服务。等待构建锁时状态为 `waiting_builder`，不消耗 1 小时构建超时。一个作者的构建完成后，其他作者可以构建，同时前者在 ACS 执行。所有入口共用同一个构建锁。

## 并发和清理验收

- 本地 19 项测试通过，包括十个真实子进程重叠、总槽位上限、同作者互斥、构建锁继承、中断回执和退出清理。
- 先前五路已有简单题 ACS 探针约 146.6 秒完成，三个 oracle 均为 1、两个 nop 均为 0，五个 Pod 同时存在，五次清理确认。
- 本次十路已有简单题 ACS 探针约 134.3 秒完成，五个 oracle 均为 1、五个 nop 均为 0，无异常，十次清理均已确认。观察到最多十个 Pod 同时存在，均调度到北京 i 区，单 Pod 请求 4 vCPU/8 GiB/30 GiB。本机控制进程合计峰值 RSS 2687.3 MiB（约 2.62 GiB，含共享页的重复计数），CPU 总时间 57.45 秒，平均约 0.428 核。此探针复用既有镜像，实际采用 shared verifier，不覆盖独立 verifier 重叠、新镜像构建或重型题目的负载。
- 十路结果和本机开销记录位于工作区 `.state/selftest-capacity-ten-probe-20261102T032953Z-baae13/`，其中 `summary.json` 保存逐次结果，`observations.json` 保存 Pod 采样，`idle-worker.json` 保存空闲主进程采样。探针仅验收并发基础设施，不是作者自主提交或题目难度评测。

宇宙现已恢复，已核对生产 worker 的真实参数为 `--workers 10`。10 个作者及 curator 均有新的模型回复和工具结果。上述短测是独立验收，不能替代每个作者新题的完整 self-test 结果。

## 部署集成记录

十路槽位配置已进入源码；2026-11-02 的控制面离线回归 358 项通过，Rust 库回归 362 项通过、1 项忽略。重建的控制器已安装到工作区 `.local/libexec/gewu-multiverse`。真实单题 DawnFM/Codex → 独立 verifier → 自动私有证据包验收通过，两个 Pod 的实际 imageID、资源、节点和清理均有证据；该单题验收不等于十路独立 verifier 已压测。

恢复启动器同时修复了两类进度丢失风险：同一路径的 workspace 不再被改成自引用链接；已恢复的当前凭证、注册表、调度状态和角色指令不再被旧软件 donor 覆盖。正式运行仍以恢复后的 worker 进程及队列状态为准。

另用原始 OpenSSL benchmark 通过真实 author self-test worker 验证从 Git 冻结上传、构建 Codex runtime、上海 ACR 发布、北京 ACS 执行的路径。三种镜像发布和 manifest/config 关联验证通过，Codex 0.164.0、rg 15.2.0；oracle=1，随后 nop 的 verifier 超时且 reward=null，worker 正确记录 selftest_failed，两次云资源清理均确认。该回执只能证明构建路径已打通，不能作为完整 self-test 通过。证据：`.state/real-worker-acceptance-adbe05c65c/`。原题测试入口运行时下载/安装依赖，失败诊断收集另有修复；未因该结果修改原始题包或伪造负对照分数。

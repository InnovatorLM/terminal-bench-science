# 作者无进展监控的工作区签名核查

2026-11-02，只读核查；未调用 reset、模型或 ACS。

最终结果：修改扫描算法后，正式 CPFS runtime 的 author-02 在 **22.249 秒**内成功生成签名，仍使用 30 秒总预算。下文保留早期失败测量以解释根因，最新实现和验收见末节。

旧实现递归扫描整个工作区，对每个普通文件 stat；上限为 6000 文件、2000 目录、5 秒。真实 author-02 工作区包含主仓库、`wt-main`、`wt-tcr` 等重复基线，0.112 秒就触发文件数上限，结果恒为 `inspection_required`，无法形成可用于监控的签名。此例的直接原因是重复计数，不是单次扫描时间过长。

## 修复

- 对每个 Git 仓库/worktree，记录 HEAD 和暂存区目录摘要，以一次目录遍历观察跟踪文件的 mode/size/mtime/ctime，且识别未跟踪、ignored 源码。不调用会重复核对基线内容的 `git status`，也不让 Git 再做一遍全量 untracked 遍历。
- 各 worktree 独立检查，不能因共享对象库而漏掉另一个 worktree 的改动；只读翻译角色内 `/workspace/...` 的 gitdir 路径，不修改 `.git`。
- 继续观察 Git 之外的 scratch、原型文件及非缓存的 ignored 源码；小型非 Git 文件增加内容哈希，大型下载/数据和跟踪文件使用元数据，避免读取全部基线内容。保留缓存排除；非 Git 文件/目录上限仍为 6000/2000，索引已列举的跟踪文件/目录另有 50000/10000 上限，且全部共享 30 秒总预算，每个 Git 子进程只获得剩余时间。
- Git 观察只使用不会调用 clean/process filters 的只读 plumbing，禁用 fsmonitor、hooks、可选 index 写入、交互认证及 lazy fetch；记录只包含路径摘要，不导出文件内容或 Git 凭证。测试验证了不会执行恶意 filter 或改写作者 index。
- Git 错误、超时、隐藏修改的 assume-unchanged/skip-worktree 标志、越界或超限仍为 `inspection_required`。未知区间清空稳定签名并重启一小时观察窗口，避免把无法观察的时间算为“无进展”。等待排除、六小时冷却、同一签名最多尝试一次 reset 均保留。

## 真实旧 source 工作区结果

路径为 `/tmp/gewu-terminal-bench-science-runtime-live-20261101/.config/gewu-multiverse/agents/<role>/workspace`。

| 作者 | 旧实现 | 新实现 | 新实现逐文件观察 | Git 仓库/worktree 数 |
| --- | --- | --- | ---: | ---: |
| author-01 | 0.106 秒，成功 | 0.024 秒，成功 | 3 | 1 |
| author-02 | 0.112 秒，超 6000 文件上限 | 1.685 秒，成功 | 2762 | 5 |
| author-04 | 0.104 秒，成功 | 0.022 秒，成功 | 11 | 1 |

author-02 的非 Git 文件主要来自现有 nodesrc 工具发行包，仍被保守观察，没有将这些目录擅自视为不可变。上述时延来自旧 `/tmp` source，不能等同于迁移后 CPFS 目标性能。

目标恢复后的实测：`W/.tmp/gewu-terminal-bench-science-runtime-live/.config/gewu-multiverse/agents/author-02/workspace` 在 5.023 秒达到 Git 观察预算，返回 `inspection_required`。该次没有 reset，也没有把未知状态记为无进展，不阻塞其他角色启动。重复基线计数问题已修复，但目标 CPFS 的签名时延仍是监控可用性的限制；不能据旧本地盘结果宣称新目录已能稳定自动检测。

随后按主线要求仅做一次有依据的预算调整：默认上限改为 30 秒，重新只读检查该正式目标。结果在 **30.036 秒** 再次触发 Git 观察预算，仍为 `inspection_required`，没有 reset。因此预算已修正但目标 CPFS 上的 author-02 自动检测尚未获得可用签名，不能称为行为验收通过。没有继续增大参数或反复试测；一小时连续无进展、六小时冷却和不确定不自动 reset 的条件未改。

16 项局部测试通过，覆盖真实 Git/worktree、修改/未跟踪/ignored 源码、scratch、不会执行过滤器或改写 index、隐藏修改标志、超时、未知区间重启计时，以及已有等待/冷却/reset 防重复规则。

30 秒预算改动后另有 5 项针对性测试通过，覆盖默认及显式剩余预算传给 Git 子进程、超时仍按未知处理、未知后重启观察，以及一小时/等待/防重复 reset 规则。

原始只读测量记录位于工作区 `.state/evidence-acceptance-4e838c1a95/watchdog-signature-before.json` 和 `watchdog-signature-after.json`。

目标 30 秒实测记录为 `.state/evidence-acceptance-4e838c1a95/watchdog-signature-cpfs-30s.json`。

## CPFS 根因与最终修复验收

逐命令只读计时确定了两个实际瓶颈：

1. 主仓库的 `git status --porcelain=v1` 即使使用 `--untracked-files=no` 仍超过 20 秒；原完整 status 耗尽约 29 秒剩余时间。`git diff-files --raw` 3.328 秒返回 5407 个 stat-dirty 条目，说明迁移后的 index 文件元数据缓存与 CPFS 实际文件失配，status 正在重新核对大量基线内容；监控不允许改作者 index，因此每次可能重复。
2. 去掉 status 后，串行 Python metadata 扫描在约 5045 文件/1402 目录时仍耗满 30 秒。目录中额外两次 `.git` 不存在探测和重复 stat 导致大量 CPFS 往返。改为从单次目录列表判断 `.git` 后，观察量提升到 8047 文件，但多个 worktree 的串行扫描仍不够快。

最终版本只用 HEAD/index 清单与一次目录扫描，跟踪文件无需内容哈希；四个独立顶层子树并行进行 CPFS 元数据读取，结束时核对目录身份/时间未变化。所有线程仍共享原 30 秒 deadline 和数量上限，未跳过任何基线副本，也未用陈旧缓存代替证据。

正式目标 `W/.tmp/gewu-terminal-bench-science-runtime-live/.config/gewu-multiverse/agents/author-02/workspace` 的最终只读测量成功：

| 项目 | 结果 |
| --- | --- |
| 耗时 / 总预算 | 22.249 秒 / 30 秒 |
| 跟踪文件 / 非 Git 文件 | 10815 / 22 |
| 跟踪目录 / 其他目录 | 3010 / 15 |
| 仓库 / Git 只读命令 | 3 / 9 |
| 签名 | 成功生成 |
| reset / 作者文件或 index 写入 / 云端调用 | 均无 |

21 项不同测试通过（19 项完整测试，加并行覆盖及原因分类测试），覆盖每份 worktree、相同 size/mtime 但 ctime 改变的真实编辑、非 Git/ignored 源码、拒绝隐藏修改和路径逃逸、四子树真实并发且不漏源码，以及既有等待/一小时/六小时/防重复规则。

世界恢复实际运行后未继续性能探测或停止作者。今后作者恰在观察期间修改源文件、目录或 HEAD/index 时，监控保守返回 `inspection_required`，其 `observation_reason=source_changed_during_observation`；预算不足则为 `observation_budget_exceeded`，避免把活跃快照变化和性能超时混为一谈。任一未知区间都不会累计无进展时间或触发 reset。

最终原始测量为 `.state/evidence-acceptance-4e838c1a95/watchdog-signature-cpfs-parallel-metadata.json`；逐命令根因记录为 `watchdog-signature-command-timing.json`。

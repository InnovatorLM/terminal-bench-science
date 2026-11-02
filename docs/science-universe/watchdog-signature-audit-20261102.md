# 作者无进展监控的工作区签名核查

2026-11-02，只读核查；未调用 reset、模型或 ACS。

旧实现递归扫描整个工作区，对每个普通文件 stat；上限为 6000 文件、2000 目录、5 秒。真实 author-02 工作区包含主仓库、`wt-main`、`wt-tcr` 等重复基线，0.112 秒就触发文件数上限，结果恒为 `inspection_required`，无法形成可用于监控的签名。此例的直接原因是重复计数，不是单次扫描时间过长。

## 修复

- 对每个 Git 仓库/worktree，记录 HEAD、暂存区目录摘要、受控 dirty/untracked/ignored 列表，仅逐文件观察实际变更；不逐文件扫描已跟踪且未修改的基线。
- 各 worktree 独立检查，不能因共享对象库而漏掉另一个 worktree 的改动；只读翻译角色内 `/workspace/...` 的 gitdir 路径，不修改 `.git`。
- 继续观察 Git 之外的 scratch、原型文件及非缓存的 ignored 源码；小文件增加内容哈希，大型下载/数据记录大小、mtime、ctime，避免读取其全部内容。保留原有缓存排除和观察上限。
- Git 观察禁用 fsmonitor、hooks、配置的 clean/process filters、可选 index 写入、交互认证及 lazy fetch；记录只包含路径摘要，不导出文件内容或 Git 凭证。
- Git 错误、超时、隐藏修改的 assume-unchanged/skip-worktree 标志、越界或超限仍为 `inspection_required`。未知区间清空稳定签名并重启一小时观察窗口，避免把无法观察的时间算为“无进展”。等待排除、六小时冷却、同一签名最多尝试一次 reset 均保留。

## 真实旧 source 工作区结果

路径为 `/tmp/gewu-terminal-bench-science-runtime-live-20261101/.config/gewu-multiverse/agents/<role>/workspace`。

| 作者 | 旧实现 | 新实现 | 新实现逐文件观察 | Git 仓库/worktree 数 |
| --- | --- | --- | ---: | ---: |
| author-01 | 0.106 秒，成功 | 0.024 秒，成功 | 3 | 1 |
| author-02 | 0.112 秒，超 6000 文件上限 | 1.685 秒，成功 | 2762 | 5 |
| author-04 | 0.104 秒，成功 | 0.022 秒，成功 | 11 | 1 |

author-02 的非 Git 文件主要来自现有 nodesrc 工具发行包，仍被保守观察，没有将这些目录擅自视为不可变。上述时延来自旧 `/tmp` source，不能等同于迁移后 CPFS 目标性能。目标恢复后还需一次只读采样；若目标超预算，将保持 `inspection_required`，不会据此 reset 作者或阻塞其他角色启动。

16 项局部测试通过，覆盖真实 Git/worktree、修改/未跟踪/ignored 源码、scratch、不会执行过滤器或改写 index、隐藏修改标志、超时、未知区间重启计时，以及已有等待/冷却/reset 防重复规则。

原始只读测量记录位于工作区 `.state/evidence-acceptance-4e838c1a95/watchdog-signature-before.json` 和 `watchdog-signature-after.json`。

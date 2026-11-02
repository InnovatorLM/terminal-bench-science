# 维护后部署恢复（2026-11-02）

目标为 `/mnt/data/zhanglinfeng/workspace/.tmp/gewu-terminal-bench-science-runtime-live`。本恢复不启动生产；最新 `/tmp/gewu-terminal-bench-science-runtime-live-20261101` 始终只读。所有新数据位于工作区。当前工作区实际挂载在 CPFS；目录名 `.tmp` 不表示它是本地 ext4，且容器没有自行 bind mount 所需权限。

## 已实际完成

- 私有检查点位于 `.state/deployment-recovery-20261102`：45 个原始控制文件，267624 字节，逐文件 SHA-256 验证；目录 0700、文件 0600，清单不含凭证正文。
- 14 个角色独立软件环境已从停机的 `universes/my-terminal-bench-science` **可逆搬迁**到新运行目录。使用同文件系统 `rename`，没有重新复制或解包科学环境。旧 donor 的工作区、Git、指令、服务状态保留原处；donor 软件已迁出，因此不能把它当作可直接启动的旧实例。
- 共搬迁 93 个软件目录，耗时 117 秒，包括每角色 `bin`、`usr`、`lib`、`lib64`、`etc`、存在的 `opt` 和 `home/role/.local`。迁移前确认所有角色 UID 没有运行进程；逐目录记录源/目标、inode、UID/GID/mode 和移动状态，支持中断续做、不覆盖冲突目标。清单为 `software-migration.json`。
- 14/14 角色均在自己的 chroot 和 UID 下通过 NumPy/SciPy 导入及计算验证；没有模型或云调用。可写环境没有跨角色共享，也没有使用指向 chroot 外部的环境符号链接。
- 从**最新停机实例**恢复了匹配最新 Forgejo 数据库的 21 个私有文件，包括角色 Git 凭证、controller 凭证、Forgejo 配置与签名密钥；保持 0600 及实际使用者 UID，记录仅包含路径/摘要。没有用较旧 donor 的凭证覆盖最新身份。
- 已安装当前的 10 个 self-test 客户端、3 个 evaluator batch 客户端，以及支持运行时 Forgejo URL 的 14 个角色辅助程序。已复制最新角色指令并安装当前版本的 skills 和题目策略。完整 capability 门检 **14/14 通过**：包括五个 Python 模块、九个基础工具、角色客户端的真实 `--help` 和全部 skill 资源摘要；回执在各角色 `capabilities.json`。

## 正在恢复的当前数据

`runtime_backup.py` 的恢复导入保留源 UID/GID/mode，并逐文件核验内容。大约 55 GB 的初始库存包含大量重复历史 Git/临时克隆。当前优先恢复作者实际工作、未提交文件、所需 Git/worktree、controller 轨迹、Forgejo DB/repositories、队列/评测证据和控制状态；旧 completed self-test 源克隆及 curator 历史 scratch 由清单明确列为后续独立归档。

工作区文件系统不支持 reflink。经过验证的恢复 staging 将合并提升到运行目录，避免再复制一份大 payload；提升后的 staging 会标为已移交，不能继续声称它是独立完整备份。最新历史 `/tmp` 原件继续保留。启动后不能把旧归档再次覆盖到活动工作区。

通用备份刻意过滤凭证，因此运行恢复还需补充私有 Git config、签名密钥及名称中包含 `secret` 的合法审计源码。仅文件路径过滤不能证明保存了全部原始证据。

## 剩余启动门检

1. 完成并验收当前数据导入；核对所有作者工作、Git 远端/HEAD/worktree、最新调度和评测状态，记录延后归档范围。
2. 运行 `prepare_repaired_runtime.py --phase configuration`，仅重绑定 active broker/queue/registry/service descriptors。历史签名模板、轨迹和回执保持原字节；registry 指向恢复后相同摘要的 snapshot。
3. 所有启用角色通过完整 capability 和工作区读写验证；重建 derived evaluation catalog，检查 Forgejo 数据库与代码仓库配套完整。
4. 对停机中的自测/评测任务作生命周期协调，确保旧运行不会被错误重复调度。用已有简单题验证平台链路后，父控制器决定生产启动。

启动器已修复同路径 workspace alias 自引用问题，并保留已恢复凭证和服务配置。目标已有软件、指令和工作时不会再从较旧 donor 覆盖。

## 工具与验证

`prepare_repaired_runtime.py` 提供四阶段：`inventory`、`software`、`private`、`configuration`。`--output` 为当前阶段私有清单或检查点路径；后三阶段要求 `--runtime-root`。所有阶段均不启动生产。

五个隔离测试通过：检查点不冒称可运行、拒绝链接控制文件、软件搬迁可恢复且保留 donor 工作、目标冲突不覆盖，以及服务路径重绑定不改历史证据。完整 capability、数据导入和生产行为验收分别记录，不能用单元测试代替。

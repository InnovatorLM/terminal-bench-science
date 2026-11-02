# 维护后部署恢复（2026-11-02）

目标为 `/mnt/data/zhanglinfeng/workspace/.tmp/gewu-terminal-bench-science-runtime-live`。本恢复不启动生产；最新 `/tmp/gewu-terminal-bench-science-runtime-live-20261101` 始终只读。所有新数据位于工作区。当前工作区实际挂载在 CPFS；目录名 `.tmp` 不表示它是本地 ext4，且容器没有自行 bind mount 所需权限。

## 已实际完成

- 私有检查点位于 `.state/deployment-recovery-20261102`：45 个原始控制文件，267624 字节，逐文件 SHA-256 验证；目录 0700、文件 0600，清单不含凭证正文。
- 14 个角色独立软件环境已从停机的 `universes/my-terminal-bench-science` **可逆搬迁**到新运行目录。使用同文件系统 `rename`，没有重新复制或解包科学环境。旧 donor 的工作区、Git、指令、服务状态保留原处；donor 软件已迁出，因此不能把它当作可直接启动的旧实例。
- 共搬迁 93 个软件目录，耗时 117 秒，包括每角色 `bin`、`usr`、`lib`、`lib64`、`etc`、存在的 `opt` 和 `home/role/.local`。迁移前确认所有角色 UID 没有运行进程；逐目录记录源/目标、inode、UID/GID/mode 和移动状态，支持中断续做、不覆盖冲突目标。清单为 `software-migration.json`。
- 14/14 角色均在自己的 chroot 和 UID 下通过 NumPy/SciPy 导入及计算验证；没有模型或云调用。可写环境没有跨角色共享，也没有使用指向 chroot 外部的环境符号链接。
- 从**最新停机实例**恢复了匹配最新 Forgejo 数据库的 21 个私有文件，包括角色 Git 凭证、controller 凭证、Forgejo 配置与签名密钥；保持 0600 及实际使用者 UID，记录仅包含路径/摘要。没有用较旧 donor 的凭证覆盖最新身份。
- 已安装当前的 10 个 self-test 客户端、3 个 evaluator batch 客户端，以及支持运行时 Forgejo URL 的 14 个角色辅助程序。已复制最新角色指令并安装当前版本的 skills 和题目策略。完整 capability 门检 **14/14 通过**：包括五个 Python 模块、九个基础工具、角色客户端的真实 `--help` 和全部 skill 资源摘要；回执在各角色 `capabilities.json`。

## 当前数据已恢复并交付

`runtime_backup.py` 的恢复导入保留源 UID/GID/mode，并逐文件核验内容。大约 55 GB 的初始库存包含大量重复历史 Git/临时克隆。当前优先恢复作者实际工作、未提交文件、所需 Git/worktree、controller 轨迹、Forgejo DB/repositories、队列/评测证据和控制状态；旧 completed self-test 源克隆及 curator 历史 scratch 由清单明确列为后续独立归档。

核心数据已合并提升到新 runtime，`handoff-core.json` 记录移交；此后不再将历史文件叠加到活动工作。通过 Git 索引对象与真实修改内容检查复用了 54031 个文件，启动范围额外复制 30196 个文件，并补上相关自测快照及依赖锁文件。所有作者最新工作、11 个非空主仓库、14 角色轨迹、Forgejo 数据库和仓库均已恢复；三个 evaluator 的原 workspace 为空，已按身份建立空目录。

工作区文件系统不支持 reflink。经过验证的恢复 staging 将合并提升到运行目录，避免再复制一份大 payload；提升后的 staging 会标为已移交，不能继续声称它是独立完整备份。最新历史 `/tmp` 原件继续保留。启动后不能把旧归档再次覆盖到活动工作区。

通用备份刻意过滤凭证，因此运行恢复还需补充私有 Git config、签名密钥及名称中包含 `secret` 的合法审计源码。仅文件路径过滤不能证明保存了全部原始证据。

## 启动门检结果

1. 数据导入完成，11 个非空主仓库的 HEAD/对象连通性/实际修改与停机源一致；各角色 UID 下再次读取 HEAD 均成功。
2. `--phase configuration` 已仅重绑定 active broker/queue/registry/service descriptors，并通过隔离配置检查。历史签名模板、轨迹和回执保持原字节；registry 指向恢复后相同摘要的 snapshot。
3. 14/14 角色通过实际工作区写入、Git 认证辅助程序和基础环境检查；14 份科学环境能由各自 UID 写入且不共享包文件 inode。控制器 UID 65534 能读取全部恢复会话输入。Forgejo SQLite integrity 和三个 bare repo 对象检查通过。
4. `.state/deployment-recovery-20261102/runtime-ready.json` 已标记 `runnable=true`，交给父控制器执行生产启动及行为观察。本恢复代理没有启动模型或云任务。

## 维护期间的五小时计时

`selftest_budget.py` 现在可读取 root 拥有、绑定当前 deployment 的维护证明，将可信停机区间与实际 ACS 区间取并集扣除；不会信任作者自行申报的等待，也不追补未观察的历史时间。相关六个测试覆盖缺证据、坏权限、重叠、其它角色、abandoned 保持关闭及真实暂停转换。

`record_operator_maintenance.py` 已记录首次确认停机的 `1793592379.738586` 至 `1793595364.9284055`，并在新 runtime 暂停仍 active 的 author-01/author-09 计时。原思路开始时间保留，已 abandoned 的思路不复活；自动暂停只由运行器实际观察到新会话后恢复。生产启动后不能继续扩展这段停机证明。

启动器已修复同路径 workspace alias 自引用问题，并保留已恢复凭证和服务配置。目标已有软件、指令和工作时不会再从较旧 donor 覆盖。

## Forgejo 启动准备补验

真实启动发现，角色 capability 通过并不能证明 Forgejo 已可启动。恢复时 `instance/data`、`instance/custom` 及后者的 `templates/base` 祖先目录有 root 所有权，控制器 UID 65534 在调整数据目录权限或更新内置页面模板时失败，尚未创建 web 进程。现已将这些服务专属目录改为 65534 所有、0700；私钥与模板文件保持 0600。同时将遗留的 `[log].ROOT_PATH` 从另一个历史实例改到当前 `instance/log`。

恢复工具已修正目录所有权和嵌套模板父目录，路径规范化函数也包含日志目录。两项对应回归通过。以真实 UID 65534 验证了 12 个启动所需目录的读写/遍历、模板实际创建删除、只读 `embedded view`，并在确认没有 Forgejo 进程后执行 `migrate`（退出 0，0.51 秒）及 `admin user list`（退出 0，0.49 秒，现有管理员存在）。没有新建账号、修改密码、生成 token 或独立启动 web。回执为 `forgejo-prepare-probe.json`，web 重试由父控制器负责。

## 工具与验证

`prepare_repaired_runtime.py` 提供四阶段：`inventory`、`software`、`private`、`configuration`。`--output` 为当前阶段私有清单或检查点路径；后三阶段要求 `--runtime-root`。所有阶段均不启动生产。

五个恢复测试通过：检查点不冒称可运行、拒绝链接控制文件、软件搬迁可恢复且保留 donor 工作、目标冲突不覆盖，以及服务路径重绑定不改历史证据。另六个维护计时测试通过。完整 capability、数据导入和生产行为验收分别记录，不能用单元测试代替。

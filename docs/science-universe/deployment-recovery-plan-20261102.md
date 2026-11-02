# 维护后部署准备

本次未启动生产，也没有写入历史 `/tmp` 运行目录。最新工作区规则要求所有新增数据位于 `/mnt/data/zhanglinfeng/workspace`；该路径当前是 CPFS，容器没有 `CAP_SYS_ADMIN`，不能自行将本地盘 bind mount 到此路径。

## 已实际完成

`prepare_repaired_runtime.py` 已从停机的 `/tmp/gewu-terminal-bench-science-runtime-live-20261101` 生成工作区内的私有控制文件检查点：

`workspace/.state/deployment-recovery-20261102/`

- 45 个控制文件，267624 字节，逐文件 SHA-256 验证。
- 目录权限 0700，文件 0600；清单不包含凭证内容。
- 包括当前角色指令、可用审核反馈、思路计时、broker/queue/registry、review ledger 等小文件；保留原 UID/GID/mode 元数据，恢复时需要显式应用。
- `migration-plan.json` 明确标记 `runnable=false`、`control_checkpoint_only`。这不是已恢复的生产实例。
- 角色软件只检查存在性，没有复制软件目录，没有修改旧角色工作区。

## 真实存储量与软件复用

14 个当前角色在 `universes/my-terminal-bench-science` 下均已存在角色 Python、独立科学环境 Python、bash、git。`my-terminal-bench-science-v2` 中多数角色缺独立科学环境，所以应优先复用前者的软件，不能盲目选择 v2 或重装十四份环境。存在性不代表所有包已经逐个验收，恢复后仍须运行角色 capabilities。

对最新实例的当前角色 workspace/selftest/evaluation/计时状态、控制器状态及 Forgejo data 做了有时间和文件数边界的只读盘点：约 61.42 GB（57.2 GiB），181925 个文件，不含软件 rootfs、包缓存和历史 `workspace.preserved-*`。因此全量搬迁并非一个小配置复制操作。最大部分是控制器与自测状态约 14.02 GB、curator workspace 约 12.48 GB、author-02 workspace 约 8.22 GB。未开始大规模复制。

工作区源的 Forgejo DB 约 3.67 MB，最新运行实例约 8.34 MB，不能拿旧数据库覆盖最新数据。最新数据库必须和同一实例的 repository objects、签名密钥等一起保留，并在停机快照/SQLite backup 方式下迁移。

## 恢复顺序

1. 停机来源保持只读；确定工作区内目标，并保存现有软件 donor 的旧 workspace 和服务状态，不覆盖。
2. 采用增量备份恢复最新作者 workspace/Git、controller 轨迹、自测/评测证据和配套 Forgejo 数据。源路径里的签名证据不应整体文本替换；重新绑定并核验 hash。
3. 在保持每角色独立 UID 和包环境的前提下复用工作区已有软件，恢复真实当前控制状态；检查路径、权限及注册表。
4. 安装新客户端和完整 skills，构建部署控制器，生成角色能力回执。验证队列未遗留未知 ACS 资源、审核唯一绑定和作者暂停状态。
5. 验证恢复文件摘要与一次简单题平台链路后，再启动生产并观察新会话。

## 启动器须先修复的一处问题

`start-universe-local-runtime` 中“Keep the controller's local layout connected”段，将 `agents/<role>/workspace` 与 `Path(entry['root'])/'workspace'` 设为同一地址后，仍把真实目录重命名为 `workspace.preserved-*`，再建立指向自己的 symlink。随后 `start-universe` 发现 symlink 又以空目录替换。历史角色根目录下已出现多份 `workspace.preserved-*`。恢复前必须在 `link == target` 时直接保留实际目录；不能重复执行该段丢失当前工作入口。

## 验证边界

两个隔离单元测试通过：控制检查点不复制软件、不声称可运行、逐文件摘要及私有权限有效、旧输出不覆盖、符号链接控制文件拒绝。Python AST、`node --check -` 和 `git diff --check` 通过。

另以工作区软件 donor 的真实 author-01（UID 21001）和 evaluator-01（UID 21011）分别运行隔离 chroot 验证：独立环境 NumPy 2.4.6、SciPy 1.17.1 均可导入，`numpy.arange(5).sum()` 均为 10。没有运行模型或云服务。其余十二个角色的完整运行能力、完整数据恢复、生产技能加载和模型进度尚未验证。

# 评测证据、作者生命周期与原帖复盘核验

本轮核验沿用现有宇宙和题目记录。平台启动验收使用已经完成的简单题运行，不以新科学题通过五次审核作为启动前提。新开发文件均位于工作区内；目录名 `.tmp` 本身不证明底层是本地磁盘。

## 作者接受后的经验回复

十位作者的实际 `instruction.md` 均已包含以下要求：指定版本正式接受后，在原 proposal/PR 的评审讨论串追加完整 Markdown 经验帖，随后主动开始下一题。四部分为来源与设计、有效与失败的迭代、评测结果与局限、迁移思路与下一题。

平台保存原帖评论的链接、作者和接受版本，不增加新的评审审批。author-10 的原 PR #42 评论 #2299 已被记录为 completed，且已开始后续思路。此处依据持久化审核账本及此前实际评论核验；暂停期间未声称重新在线读取 Forgejo 原评论。详见 [原帖复盘设计](post-acceptance-retrospectives.md)。

## 评测证据与历史兼容

- evaluator 的 `role-evaluation-batch status` 提供五项版本绑定证据索引：job/run ID、源码快照、镜像 digest、实际 Pod 资源、原始轨迹和 native session 留存、脱敏文件交付、verifier 和清理状态。
- 原始轨迹保留在操作员私有运行目录，evaluator 接收脱敏包；`raw_trajectory_exported=false` 不表示原始文件没有保存。
- 当前运行必须具有独立 run ID，不能把一次运行复制成五个 job。索引核查 manifest/binding 和文件存在性、大小；正式接受仍逐文件核对哈希。
- 在原始文件和发布文件逐项哈希核对后，30 份旧回执补齐新版索引字段。原 private/public 结果先备份，分数、状态、缺项、科学决定和审核账本均保持原样。
- 另有 10 份历史失败结果未迁移：五次旧 bee 运行缺完整 binding 与解题轨迹，五次旧 IOD 修订缺 execution-binding 和原始轨迹。它们只有配置、日志及平台诊断，不得计作模型答错。这是历史证据真实缺失，不能通过补写摘要伪装完整。bee 已另有替代批次；IOD 该失败修订与已接受旧版本分别保留。
- IOD 已接受旧版本的五份历史包可以读取已补交轨迹和 verifier，但实际 Pod 资源未曾保存，明确标为 `historical_actual_pod_observations` 缺失；不据此撤销用户已确认的旧版接受记录。

现场证据保存在工作区 `.tmp/evidence-index-repair-20261102T225539Z/`：`legacy-migration.json`、`legacy-refused-diagnosis.json`、`isolated-cli-status.json`。历史回执原件位于各 job 的 `evidence-receipt-refresh/`。

## 已完成的运行核验

1. 已完成的简单题 `openssl-selfsigned-cert`，job `62275c95ea204b83833e9694c7af1710`，经旧回执迁移后可完整读回原始/脱敏轨迹、native session、独立 verifier、实际 runtime/verifier Pod 的镜像、4c/8Gi 资源、北京 i 区节点及清理证据。没有重新调用模型或 ACS。
2. 全部 14 个角色以真实隔离 UID 通过科学计算模块、工具入口和技能内容检查。三位 evaluator 的实际 `role-evaluation-batch status` 均退出 0，返回五项证据索引。它们当前批次有效 trial 数分别为 4、5、0；工具可用不等于每批已满足科学验收条件。
3. 审核暂停、收到作者反馈/完成后的恢复、48 小时有效迭代预算、重复失败且源码一小时无变化后的 reset，已通过真实本地控制器与 watchdog 进程测试。ACS/平台等待不误计迭代时间，reset 保留工作区、轨迹和思路历史。详见 [生命周期核验](lifecycle-watchdog-validation-20261103.md)。
4. 本轮停止期间，按已有 `run_finished` 和进程观察对作者时钟对账，四位 active 作者转为 session_paused，已有 ACS/platform 等待保持原样。原始时钟已备份；未清零历史用时。
5. self-test 默认 10 个 worker，同作者一个任务；继承存活 worker 锁计入容量。30 秒指标区分排队、构建、ACS 阶段。本机构建仍只有一个槽位，worker 数不能冒充同时构建数或实际 Pod 数。详见 [容量说明](selftest-capacity.md)。
6. verifier 在构建前检查入口和静态输入；shared/继承 separate 的合法复用保留，显式 verifier 镜像按 Harbor 契约优先并固定 digest。详见 [构建前检查](verifier-preflight-20261103.md)。

本轮新增校验均为本地/已有证据核验，没有新增模型或云评测费用。针对各修改的回归测试已分别记录于对应文档；重叠测试套件不累加为独立测试总数。

## 恢复状态

此文首次写入时生产宇宙仍暂停。角色能力与证据交付已通过现场检查；剩余恢复工作是周期备份入口修复、最新关键文件恢复抽检、源码备份及启动后的实际服务观察。完整运行树备份仍是 partial，不能把关键文件恢复成功称为全量环境备份完成。恢复后在此节追加具体进程、队列和轨迹观察结果。

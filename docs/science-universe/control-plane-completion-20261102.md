# 控制面修复逐项验收

本表对应 [原始 buglist](control-plane-bug-audit-20261102.md)，区分软件修复、真实基础设施验收和生产模型行为。工作区记为 `W=/mnt/data/zhanglinfeng/workspace`；当前运行目录为 `W/.tmp/gewu-terminal-bench-science-runtime-live`，实际文件系统仍为 CPFS。

| 项目 | 当前结论 | 权威证据与边界 |
| --- | --- | --- |
| 1. 备份失败／新日志被旧库存挡住 | 连续周期和恢复抽查通过；全量备份仍未完成 | 55.11 GB 独立历史归档已抽样恢复。追加审计验证 3 个自动周期、10 作者最新轨迹摘要/源前缀 10/10、实际恢复 23 文件；追加周期后 bulk pending=42,895、verified=41,771。逐文件增加 source_checked_at，历史库存告警与新 priority/bulk 告警分开。新增 24 个 priority 告警中 22 个是 author-02 在扫描与读取间删除的原型文件，不能称为轨迹丢失，也不能保证短于备份周期的文件都被备份。last_success 仍为空。`W/.state/backups/live/recovery/continuous-priority-audit-20261102.json`。 |
| 2. runtime 构建路径冲突／完整自测 | 构建冲突及 verifier 时间覆盖已修复，真实对照通过 | 原 worker 冻结、三种镜像发布、oracle=1 已通过；同 snapshot/digest 的 nop 精准复跑 reward=0、无异常、Harbor exit=0，六个测试真实执行后因缺解答产物失败。实际 4c/8Gi、北京 i，清理确认。旧失败回执保留；修复模板把 900 秒预算缩成 600 秒及取消后日志丢失。见 [自测恢复报告](selftest-recovery-audit-20261102.md)。 |
| 3. 审核开始停止作者、反馈后恢复 | 软件执行链已验证 | 生产 Engine/RealSpawner 的真实本地子进程测试覆盖 running/starting 边界、key/generation/commit 匹配、旧反馈拒绝、暂停中不重启、反馈后恢复。未伪造生产派单，见 [生命周期验收](author-lifecycle-acceptance-20261102.md)。 |
| 4. 首次审核回执夸大迭代证明 | 已取消绕过并验证 | v3 要求真实前次执行、实际版本修改、作者 solution、复跑和预算证据；缺项不能写 true。历史 iod-enso 的 schema-2 不伪造升级，也不因此翻转已经完成的 3/5 评审。详细历史审计在 `W/.state/deployment-recovery-20261102/iod-enso-legacy-handoff-audit.md`。 |
| 5. self-test 单槽排队 | 已生产启用 10 槽并真实压测 | 空闲主进程约 28.2 MiB，无预热 Pod；真实十路 ACS 5 oracle=1、5 nop=0，十次清理确认。构建另有共享槽位，等待构建不消耗构建预算，见 [容量记录](selftest-capacity.md)。 |
| 6. 重启后跳过中断任务 | 已修复并恢复历史回执 | 两条遗留 validating 已转为明确 interrupted 回执并保留原证据，不重复未知云任务。对应 source snapshot 与恢复依赖保留；worker recovery/生命周期测试通过。 |
| 7. 缺原始轨迹、镜像和实际 Pod 证据 | 新链路已实跑验证，历史缺项明确保留 | 新 DawnFM/Codex→独立 verifier→私有 evidence packet 全链路 reward=1，23 项检查通过，含实际 imageID/resources/node 和清理。历史五轨迹已补交，已删除历史 Pod 的实际观测无法补造，见 [证据验收](evidence-acceptance-20261102.md)。 |
| 8. 发布未脱敏、宽松完整性判断 | 已修复并验证 | 轨迹、verifier 和版本绑定检查；发布前脱敏、路径校验、权限限制和 SHA-256 清单。真实 evaluator UID 可读，10 作者 UID 均不能读其私有包；失败诊断也保留而不伪造 reward。 |
| 9. 死循环观察／自动 reset | 执行链与真实目录性能已验证，已热部署 | author-02 正式 CPFS 工作区 22.249 秒内完成有界观察。真实 watchdog→online_reset→controller ACK→子进程清理→保留工作/轨迹/计时→新 runner 读取指导，隔离集成通过；同源只尝试一次、六小时冷却、未知/等待不累计。生产 LLM 是否按指导改变方案仍需自然事件观察。 |
| 10. null／异常／超时误评分 | 已修复并测试 | 缺失、非有限、布尔、非二元 reward 与异常冲突不计分。四小时超时先诊断，题目缺陷/平台错误不记模型错；排除后才可作模型超时判定。原始 reward 不篡改。 |
| 11. 外部观察者看错实例 | 已修复并核验实际进程 | 当前 observer 绑定当前 runtime，interval=3600，实际进程存活；观察者保持建议模式。`W/.state/universe-observer/process.json`。 |
| 12. 五次评测 skill 与并发不一致 | 已统一 | 当前 skill 与 batch 入口采用五个独立并行 job；每个 job 自身 n_concurrent=1。self-test oracle/nop 不调用模型、不计难度。 |
| 13. 软件、skill 发现与运行限制 | 已生产验证，新增故障指南已分发 | 14 角色 capability 通过；隐藏 skill 清单修复后 10 作者及 curator 均有新 `skills_discovered/model_reply/tool_result`。不限轮，四小时会话预算。新增本地/镜像路径、pytest/CTRF、Git 内容一致性检查参考；`cmp` 工具已补装。 |
| 14. 新送审请求缺消费者 | 已接通并集成测试 | `selftest_review_dispatch.consume` 核验冻结版本与资格，持久独占派单；重复轮询及交付中断可恢复。没有新合格请求时不启动 evaluator 空转。 |
| 15. PR 分页遗漏 | 已修复并回归验证 | 结束条件使用实际页长；12 条跨三页检查通过。 |
| 16. iod-enso 已审核版本与当前 PR 不一致 | 解析缺陷已修复；作者已恢复版本比较，交接尚未完成 | 5ead0fe4 的独占审核已 completed、3/5、建议接收；PR #42 HEAD 898743be 的题目说明、task.toml 和 Dockerfile 有变化，尚未合并，不能套用旧评审。基础镜像解析改为请求仓库、本次 pull digest 与 inspect 交叉核验，原始日志重放通过；失败明确归平台、不要求改题。author-10 单角色 reset 已获 controller ACK，旧进程全部退出，文件/轨迹保留；新会话 run-1793601214920-0061 已实际读取回执并比较 task 路径，没有为此次修复重跑旧评测。`W/.state/deployment-recovery-20261102/author10-version-reconciliation-reset.json`。 |

## 后续轨迹发现与修复

06:43 的版本比较没有完成交接。author-10 随后把当前目录上传内容声明成历史 commit，两个 job `8d11e2a99a45449a9a9974929b1d480f`、`20fe4df14b374976801a8db60fdb5140` 被旧 worker 先做网络检查，错误地把当前版本的 `no-network` 问题归到历史版本。直接解析 bare Git 与冻结快照确认：已审核 5ead0fe4 的 verifier 是 public，当前 898743be 才是 no-network。

- worker 改为先校验 Git 字节及执行位，再做题包/网络预检；不匹配明确报 `author_source_version_mismatch`，不启动云执行。历史错误回执保留，新增事实说明纠正其归因。
- dispatcher 保留已完成旧版本结论，给当前 PR 发布 `version_reconciliation` 与 `prior_reviews`，避免旧直接派单覆盖当前反馈；curator 收到同一条版本交接队列。仅重启受监督的 dispatcher 子进程，已实际产生 PR #42 对应队列。
- author-10 已经 controller ACK 暂停，再带确定的两个版本、有限 task diff 和修订 skill 恢复。解除暂停回执为 `W/.state/deployment-recovery-20261102/author10-reconciliation-release.json`；文件、分支、轨迹、原 idea 时钟均保留。新会话是否完成交接仍需行为观察，不能把恢复当成正式接收。
- author-03 的通过 job `95589e219dff43e6badf567d293ea25a` 存在原始起点冲突；author-07 的 `89e10ff281544ca598974a9275a83baa` 缺迁移前 observation。新增结构化 `waiting_platform`，明确不需改题、重跑或全盘搜平台源码，已更新两个作者的结果反馈。真实时钟和执行记录未修改，历史前缀冲突不能拼接或回填。
- 增加只生成方案的历史 observation 恢复预检及事件变化时历史存档；目前没有应用恢复方案，运行中 watchdog 的新存档逻辑尚待下一次加载。
- 14 个角色收到版本交接、局部 Git 导出、实际 solver 路径、负对照 reward、Python ABI 与 wheelhouse 排障说明。技能文件摘要已验证；capability 的原执行探测时间保留，没有把纯技能更新称为重新运行软件探测。

以上组合回归 85 项通过（171.302 秒），均为本地测试，没有为本次诊断新跑模型或 ACS。宇宙仍运行，evaluator 等待新合格任务，版本交接和历史预算缺口仍属未完成事项。

07:20 UTC 行为观察：author-10 新会话 `run-1793603700351-0064` 已写具体版本选择说明，比较 task 范围，并提交 5c9277c8 的新自测 `73c220ec47444a2ab5e4e3f5e9c93815`。该次已通过源版本校验及网络预检，处于 waiting_builder；这是作者发起的新版自测，不是重跑旧五次模型评测。尚未证明 curator 完成交接决策或当前 PR 已进入审核。10 个作者和 curator 均有运行会话；3 个 evaluator 等待任务。

没有为了验收而重置生产作者，也没有将离线模型脚本称为真实 LLM 行为验证。生命周期软件协议的真实进程验收已完成；自然发生的模型行为继续由轨迹和观察者监控。

本轮已补充的稳定操作方法位于 `deploy/science/skills/acs-task-troubleshooting/`，分发给作者、evaluator 和 curator。当前会话可读取 `/skills/` 更新文件；新会话才重新注入更新后的 skill 正文，既有会话快照不改写。

2026-11-02 06:43 UTC 补充核查：author-10 新会话已明确比较 5ead0fe4 与当前 PR 的说明、网络声明和 Dockerfile 差异；evaluator-01 仍为 completed，没有启动新版审核。这里的 reset 是针对确认的平台故障和重复搜索作出的运维干预，不是为制造验收结果。构建/交接/verifier 的 34 项组合回归通过。新 self-test 使用独立 Python 子进程，因此后续请求加载修复；已在运行的子进程不强制重启。

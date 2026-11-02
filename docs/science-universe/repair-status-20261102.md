# 宇宙闭环修复：2026-11-02

目标保持不变：五次独立评测的轨迹、镜像、运行标识、实际资源及清理证据可追溯；正确区分难题超时与题目/平台故障；自测容量足够；作者等待审核、死循环/48小时预算重置可验证；重复错误形成角色技能。科学题审核不作为平台启动验收前提。

## 本轮证据及修复

1. **确认平台生命周期缺陷。** `bee-restoration-meta` 五次运行的 Pod 总寿命都是 14400 秒，模型预算也为 14400 秒。五个 Pod 都在 Pod 启动后约 14400 秒以 137 退出，随后 session 复制、日志/文件下载失败，控制器最终到时终止。这不是模型五次答错的证据，137 本身也不证明 OOM。
2. **分开解题与基础设施预算。** 模型保留四小时；Pod/控制器/worker 依次包含 setup、模型、verifier 和收尾预算。新模板、正式评测 worker、作者自测 worker 都使用同一计算。旧登记模板的源摘要不变，实际执行配置单独记录调整后的基础设施寿命。
3. **运行中及退出前保存原始轨迹。** 每分钟收集 `/logs/agent` 和尚未被 Codex finally 复制的 session，退出前再收集。下载失败保留上次快照；收集与删除有界，失败不能跳过清理。文件传输改为分块文本传输，避免已终止 Pod 上无界 tar 读取。
4. **失败也交付诊断材料。** 不再要求最终 result/ATIF/verifier 完整后才发布已有日志；原始 session 半行 JSONL 经脱敏后保留，并在清单注明。缺证据仍不可计分。
5. **历史五次证据补交。** 原始结果文件已保留为各 job 的 `result.before-timeout-diagnosis.json`。五份 evaluator 私有包补入现存 trial/config 日志、实际 Pod 资源、镜像、run ID 和生命周期诊断；历史 Pod 已删除，五条缺失解题轨迹不可恢复，没有伪造分数或轨迹。
6. **技能更新。** 增加题意/评分/来源交叉核查与超时证据审查，已分发到 14 个角色的隔离工作区与控制器技能视图，资源摘要已核对。当前会话没有被强制重启，实际采用效果仍需观察。

验收：针对性离线测试覆盖阶段预算、取消回收、文件缺失、传输失败、半行 session、敏感值脱敏和队列恢复。真实 ACS smoke `tbs-universe-linfeng-evidence-84018f5d0b02` 完成运行中 session 保存、2 MiB 二进制哈希校验、命令超时后收集和 Pod 删除确认，模型调用数为 0。这是基础设施验收，不是科学题评分，也不是一次真实四小时模型运行。

运行报告：工作区 `.state/acs-evidence-smoke-20261102T1450/report.json`。历史补交清单：`.state/code-checkpoints/bee-timeout-evidence-recovery-20261102.json`。技能部署回执：`.state/code-checkpoints/timeout-contract-skills-deployment-20261102.json`。

新派发在代码更新期间暂缓，随后已恢复；运行中作业未重跑。恢复后新作者自测 `66d163343e16448a977de9013bdcb10b` 的 oracle/nop 配置已使用 Pod 16920 秒、agent 14400 秒，证明新 worker 已读取寿命修复。原先已经启动的进程不能据此宣称已热替换。

## 尚未完成，不能标记 goal 完成

- 结构化超时诊断与五次汇总已接通：原始 reward 保持原样，另存带证据哈希的判定。只有真实四小时 agent 超时、完整来源/Pod/清理证据及持续解题轨迹，才允许按难度计错。题目缺陷、平台故障、取消运行和证据不足均不自动计错。离线集成测试覆盖完整判定链，但没有用模拟时间声称完成真实四小时验收。
- author-07 的第二轮平台故障替代批次已全部完成并逐文件核验：5/5 正确、无运行异常、完整轨迹/verifier/Pod/清理证据。该题按 >=80% 规则应因难度不足拒绝；仍需确认 evaluator 回执与作者修复/换题交接。
- 已定向放行 author-03/04/05/08 的四个完成版本的历史计时缺口，记录“历史用时未知”，不回写时间、不重跑自测。03/08 已获得实际 evaluator 活动回执；04 得到具体题包反馈。05 的 evaluator 启动/排队状态仍需确认恢复。
- 作者等待审核已有真实行为证据；48小时预算与死循环 reset 已有离线/进程级测试，仍需完整核查当前各角色的监控健康与新会话恢复。历史 error_type 残留的修复已加载到监控进程，保留 reset 历史及冷却时间。
- 仍需代码整合、完整备份周期、历史失败证据补交与后续真实评测验收。旧 Pod 销毁前未保存的原始轨迹无法恢复，不得补造。

## 已有能力的当前核对

- self-test supervisor 为 10 个 worker；构建仍受独立 builder 槽位约束，worker 增加不等于同时启动十个本机构建。
- 正式评测队列上限为作者数乘五，当前为 50。空槽位不代表持有 50 个 ACS 容器。
- IOD `5ead0fe4` 已有正式接受记录；后续 IOD 版本有不同审核结果，不撤销旧冻结版本结论。
- 宇宙仍在运行；当前变化不等于“所有生产闭环已完成”。

## 本轮恢复与原帖复盘验证

- 新控制器已安装并恢复运行；镜像/模型评测派发在修复期间暂停，验证后已解除暂停。当前作者复盘功能已有实际行为证据：author-10 在原 PR #42 发出 Markdown 评论 #2299，调度器自动记为 completed，作者已开启 `oshw-seasonal-threshold-attribution` 新思路。10 位作者的 instruction 均安装同一规则。
- 五个 bee 替代运行持续重连但没有模型输出；在失败 Pod 中确认缺公共 CA，启用正确公共信任根后 HTTPS 可达。已取消这五次空转并确认清理，保留原日志；没有将它们计作模型答错。
- 修正 Harbor agent 选择：已知内置 `name` 优先于 `import_path`，因此必须清空竞争名称才会执行 `UniverseCodex`。新增测试使用真实 Harbor factory 验证类选择；证书准备回执同时写到容器日志，保证最终收集保留。
- 第一次简单题 `5de16bacca714b8db731043daa8eea0b` 虽 reward=1，但绕过了 adapter，不能作为证书修复证据。修正后的简单题 `cf5744673eaa4882bb0d59cc373b2a5e` 才完成完整验证：指定 DawnFM 模型，runtime trust 回执、实际模型轨迹、verifier、实际 Pod 证据和清理全部保留，reward=1、n_errors=0。回执位于工作区 `.state/runtime-trust-repair-20261102/adapter-acceptance.json`。
- 8 项针对性离线测试通过；模型连接与证书排查已写入 troubleshooting skill 并分发至 14 个角色。运行中的会话是否阅读新技能仍需观察。

## 替代评测和调度状态修复

- 五次 CA 故障均有 `platform-diagnosis.json`，绑定原 job、evaluation、snapshot、取消记录及原日志哈希。三个 evaluator 已安装支持取消运行诊断的工具；evaluator-01 的 `/evaluation/platform-diagnoses/` 保存了五次故障与替代运行的对应关系。
- 替代计划 `rollout-replacements/af02182f69425be209f9c280b0c88dc0b0b4c3695a98074599a32b52a0c03f08/0002.json` 保留原五次取消运行，派发五个新 job：`9082a01b…`、`399e2edd…`、`9425ebdd…`、`8f922ce6…`、`07294c91…`。真实 queue worker 均已领取，实际配置为 `name=null`、`universe_codex:UniverseCodex`、指定 DawnFM 模型。
- 同一批无效结果交给 evaluator 诊断后，不再因文件仍存在而反复唤醒；新 job、结果内容或超时判定变化才产生新的唤醒。不会抢占该 evaluator 已在处理的其他题目。
- 旧 PR 的等待反馈不能覆盖当前独占评审的唤醒标记；旧 author preflight 也不能覆盖精确绑定的 five-rollout 等待。已在线确认 evaluator-01 的 seaice 审核标记及 author-07 的五次评测等待状态正确。
- 33 项调度、复盘、替代流程测试通过；新增 CA 替代路径的 8 项测试通过，覆盖 adapter 被内置名称绕过、TLS 未验证、轨迹缺失和有效模型失败不可重跑。已有真实简单题 smoke 是修复证据，不以新科学题评测作为平台启动门槛。
- 五个替代 Pod 均进入 Running，全部位于 `cn-beijing-i`；五份 TLS 回执通过。运行中原始 JSONL 已在本地保存，逐次观察到实际 assistant 回复和工具调用，未再出现早先的反复重连。证据回执：`.state/runtime-trust-repair-20261102/replacement-inflight-evidence.json`。这仍是进行中证据，不是最终五次完成声明。
- author-07 的旧 operator hold 已在控制器确认静止、五个 worker 存活、精确等待绑定一致后解除。真实 `schedule-state.json` 显示 `waiting for five assigned evaluation rollouts`，等待由正常机制接管。回执：`author-07-hold-release.json`。新监控周期已检查全部十位作者，无残留 `error_type`；正常评审和平台等待被识别为等待，不重置。
- 历史 IOD 修订的五次失败运行已补交现存日志、execution/Pod 证据和非计分诊断至 evaluator-02；原 result 保持字节不变。旧 Pod 销毁且没有原始 solver 轨迹，不能恢复或用于难度判断，也未为已知存在题包缺陷的旧修订启动重跑。回执：`historical-iod-evidence-supplement.json`。
- 最终五次替代运行均为 `solver_complete`、reward=1、n_errors=0。重新检查冻结源码摘要、每次 12 个已发布文件的哈希、原始 JSONL/标准轨迹、实际 runtime/verifier Pod 的镜像 digest/imageID/节点/资源及清理。五次有效结果正确率 100%，因此严格拒绝难度验收。完整审计：`.state/runtime-trust-repair-20261102/five-rollout-completion-audit.json`。
- 待提交代码在独立目录运行 161 项测试全部通过；随后去除独立 public-service 的奖励例外，并重新运行受影响的 16 项测试全部通过。代码检查未发现凭据特征，工作区完整源码快照已保存，public Harbor 服务的未完成改动保留在工作区。

原帖经验回复已走通，五次替代评测及其证据已全部完成；完整目标仍保持 active，直到作者后续交接、其余调度问题、监控和备份逐项完成核验。

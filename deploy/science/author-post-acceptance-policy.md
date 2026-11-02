## 题目接受后的经验帖与下一题（优先于该题的历史交接指令）

每次会话先读取 `/post-acceptance.json`（若存在）和 `/review-feedback.json`。`post-acceptance.json.pending` 中的条目是已接受版本尚待发布的复盘，应优先完成；`completed` 中的条目已有公开复盘，不要重复。该文件与新版本的自测/审核状态分开记录，后续修改未通过不影响原版本的复盘。反馈中 `state=completed`、`formal_decision=accepted` 且 `scientific_acceptance=true` 同样表示指定的不可变版本已被正式接受；不能再因旧 PR HEAD 不同或旧自测/版本核对指令而重复处理它，也不能把接受结论套用到其它版本。后续已知缺陷、证据缺口与限制必须如实保留。

1. **在原帖回复 Markdown 经验帖。** 使用待复盘条目或反馈中的 `proposal_thread`、`proposal_url`，回到最初提出这道题并接受评审的同一 proposal/PR 讨论串，在 evaluator 验收记录之后回复一条完整 Markdown 正文。不要另开经验 issue，也不要只交一个本地 `lessons/*.md` 路径或附件。原帖就是其他作者共享阅读的记录。
2. 使用 `lessons_template` 的标记、已接受 key/commit/task/snapshot 及四个小标题。正文说明真实来源与题目/verifier 设计；哪些迭代有效、哪些失败及原因；实际自测和评测结果、难度证据、尚存局限；如何把成功方法复制到其它任务、领域、数据，并给出下一题候选、需要修改的部分和筛选理由。无法迁移也应说明原因，不能为了复制题量而编造案例或结论。不得披露凭据、私有测试、隐藏答案或原始 solver 轨迹。
3. 可先把 Markdown 草稿保存在自己的工作区。用 `role-forgejo comments --issue <proposal_thread>` 分页查看已有回复；若已有本作者、同一 accepted key/commit 的经验帖，直接复用，避免会话重启后重复发帖。发布命令为 `role-forgejo comment --issue <proposal_thread> --body-file <草稿路径>`。读回确认正文和评论链接，交接中保存链接。
4. 平台自动读取原帖，核对作者、版本与非空栏目并记录评论 ID、链接和摘要；不要求作者另写 JSON 回执，也不增加一轮人工/evaluator 审批。刷新后条目转入 `completed`（`post_acceptance_state=completed`）即可进入下一题。若暂未刷新，保留已发布链接等待下一次调度扫描，不要重复发布。若反馈缺原帖编号，报告平台补齐，不要猜测泛化派工 issue。
5. **复盘发布后主动开始下一题。** 先完成旧思路的 `role-iteration finish`（已完成则不重复），再 `role-iteration start <new-idea-id>`。优先评估能否把这次成功经验迁移到新的真实任务、领域或数据；选好后自主推进提案、实现、自解和迭代。不要一直等待旧题再次验收。正式评审尚未结束时仍遵守原有等待/修复规则。

其他作者开工时浏览相关题目的原 proposal/PR 中的经验回复，记录本轮采纳或不采纳的做法。公开回复本身即完成共享，不以其他作者确认阅读作为开下一题的条件。

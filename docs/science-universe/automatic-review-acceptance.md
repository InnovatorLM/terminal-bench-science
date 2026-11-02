# 评审后的自动接收登记

evaluator 对一个冻结版本完成科学审核后，必须明确提交 `accept`、`reject` 或 `revise`。`completed` 本身只表示这一轮评审结束。对于明确的 `accept`，调度器自动登记正式接收，不再要求另一轮人工批准。

登记条件为同一 commit/task/snapshot 的五次独立有效评测，使用指定 DawnFM 模型，正确率不超过 60%。四小时超时只有在 evaluator 引用留存轨迹、排除题包和平台故障并登记 difficulty 判断后，才能计作模型错误。

平台核查原始和公开证据文件的实际摘要、原始会话与标准化轨迹、运行 ID、镜像 digest、Pod 实际资源、清理结果，并核对原始执行与 evaluator 可读证据之间的对应关系。正常完成的 trial 还须保留 verifier 输出。校验不会发起新的 ACS 任务。

调度器先持久化经过身份校验的 evaluator 完成回执，再生成只覆盖该冻结版本的正式接收记录。重启后可以从此记录恢复；新 PR HEAD 不继承旧版本的接收结果。用户已明确批准的历史接收记录继续有效，原有证据限制仍保留。

证据缺失或不一致时，作者收到 `waiting_platform/formal_acceptance_evidence` 和具体 `acceptance_blockers`，不将它误读为科学拒绝，也不重复构建或自测。调度器的 `pending-acceptances.json` 和 curator 的 `/review-acceptance-handoffs.json` 记录平台负责人及待补项；补齐后自动重试登记。

正式接收后接入现有经验帖流程：原作者在原 proposal/PR 回复完整 Markdown 总结，平台记录链接，作者随后开始下一题。验收证据在 `formal-decisions/` 中保留五个 job/run ID 及对应摘要，原始运行文件继续按原位置保存。

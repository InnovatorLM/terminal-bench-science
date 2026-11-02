# 作者暂停与 reset 验收（2026-11-02）

本次使用隔离工作区、真实本地子进程和模拟的审核服务；没有调用模型或 ACS，没有启动生产宇宙。

## 审核暂停

- 控制器以作者公开的 `review-feedback.json` 和 `review-current-assignment.json` 的 key、generation、commit、task 精确匹配为准。只有明确 reviewing 且 `author_must_pause=true` 时暂停。
- dispatcher 仍要求 evaluator 已回报同代次 reviewing 活动；派单文件存在本身不被冒充为已进入审核。
- 修复同一版本旧代次反馈覆盖新代次的问题。旧 completed/waiting 回执不能解除较新 reviewing 的暂停。
- Rust 测试创建真实 sleep 子进程和进程组，分别在正在运行和已创建子进程、尚未交接完成两个边界发布审核暂停；验证进程组消失、轨迹有结束记录、PID/PGID 记录清理，暂停期间不重启。
- waiting_author 或 completed 后，作者的 Desired::Started 保持不变，控制器重新调度；旧工作文件保留。

## 在线 reset

- 已验证控制器 hold/ack 协议：in-flight spawn 不会提前签发 quiescent，已结束并清理后才发 exact nonce/role/controller PID 回执。
- 释放 hold 后下一次准备的运行计划指向更新后的 instruction，保留原策略和 reset 指导。
- 该测试证明控制器和指令入口接通，不等于真实模型已阅读并遵循指导。生产会话的 instruction_read 事件和后续行为仍需恢复后观察。

## 卡死观察修正

- 原来要求三个 session 才可能触发，无法覆盖不限轮数的长会话。现在三个 session 各至少三次共同失败，或当前一个 session 至少九次同类失败，均可成为候选。
- 保留至少一个小时无工作区变化、当前 runner 存活、无审核/ACS/构建/平台等待条件。同一源状态只尝试 reset 一次，变更后的状态仍有六小时冷却。
- Python traceback 用最后一个异常类型和消息归类，避免变化的调用行和 stdout 掩盖相同失败；原始输出不写入监控回执。
- 长于五分钟的本地进程视为计算中，暂不 reset；短诊断命令不会永远清零观察时长。negative control 不当作故障证据。
- 重型观察每五分钟一次，等待或无重复失败时不扫描工作区；扫描有文件数、目录数和时间边界，读取错误不会作为“无变化”证据。损坏的观察 JSON 返回 inspection_required，不使整个 watchdog 退出。

## 验证

- Python：dead_end_watchdog、review_dispatcher、role_reset_online 合计 24 项通过。
- Rust：author_review 3 项（含两个真实子进程测试）、operator_hold 4 项、review_assignment_tests 4 项通过；均通过 workspace-local Cargo 工具链离线编译。
- Python AST、git diff --check 通过；按执行约束运行 node --check（本次没有 JavaScript 变更）。
- 历史 author-02、author-04、author-10 最近三个会话只读抽查：没有满足自动 reset 阈值。不能将历史有报错等同于已证实死循环。

尚需主集成恢复生产后检查真实作者收到审核时停止、等待反馈后恢复，以及发生适当故障时 reset 后模型读取指导；此文不宣称生产行为已经验收。

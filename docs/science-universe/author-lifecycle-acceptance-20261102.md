# 作者暂停与 reset 验收（2026-11-02）

本次使用隔离工作区、真实本地子进程和模拟的审核服务；没有调用模型或 ACS，没有操控正在运行的生产作者或伪造生产派单。

## 审核暂停

- 控制器以作者公开的 `review-feedback.json` 和 `review-current-assignment.json` 的 key、generation、commit、task 精确匹配为准。只有明确 reviewing 且 `author_must_pause=true` 时暂停。
- dispatcher 仍要求 evaluator 已回报同代次 reviewing 活动；派单文件存在本身不被冒充为已进入审核。
- 修复同一版本旧代次反馈覆盖新代次的问题。旧 completed/waiting 回执不能解除较新 reviewing 的暂停。
- Rust 测试创建真实 sleep 子进程和进程组，分别在正在运行和已创建子进程、尚未交接完成两个边界发布审核暂停；验证进程组消失、轨迹有结束记录、PID/PGID 记录清理，暂停期间不重启。
- waiting_author 或 completed 后，作者的 Desired::Started 保持不变，控制器重新调度；旧工作文件保留。

## 在线 reset

- 已验证控制器 hold/ack 协议：in-flight spawn 不会提前签发 quiescent，已结束并清理后才发 exact nonce/role/controller PID 回执。
- 释放 hold 后下一次准备的运行计划指向更新后的 instruction，保留原策略和 reset 指导。
- 该测试证明控制器和指令入口接通，不等于真实模型已阅读并遵循指导。生产会话的 instruction_read 事件和后续行为仍需实际触发后观察。

## 真实控制器集成补验

新增 `deploy/science/test_author_lifecycle_integration.py`，由 Python 启动隔离的 Rust engine fixture。fixture 使用生产 `Engine`、`RealSpawner`、运行计划、进程组清理和 hold/ack 实现；唯一替代的运行环节是无网络的模型脚本。使用单独的临时部署、未被任何现存进程占用的 UID，清理仅针对 fixture。

两项集成测试均通过，耗时 12.843 秒：

1. 真实 `dead_end_watchdog.scan` 读取九次重复失败的轨迹和未变化的工作文件；只前移观察时间，不 mock observer、`online_reset`、进程身份或 controller ACK。第二次观察触发真实在线 reset。
2. Rust controller 实际停止运行中的 runner，轨迹追加唯一 `run_finished=stopped`，清除 PID/PGID；随后签发精确 nonce、角色和 controller PID 的 quiescent ACK。
3. Python reset 实际终止进程组之外、属于独立角色 UID 的 broker 子进程；保留工作文件、旧轨迹、五小时迭代账本，保存原指令备份，并更新隔离环境和控制器的两份 instruction。
4. hold 成功释放后，控制器启动新的真实本地子进程。该离线脚本读取更新后的指令并记录摘要，测试核对摘要和 reset ID。下一次 watchdog 观察为 `already_attempted`，没有重复 reset。
5. 另一项测试实际验证旧 generation 的 reviewing 不暂停当前作者；匹配版本的 reviewing 停止作者且不替换会话；同代 `waiting_author` 反馈后恢复新的 runner。

这补齐了此前“Python reset 与 Rust hold/ack 分开测试”的证据缺口。它证明软件链路已连接，仍不冒充真实 LLM 已按指导换思路，也不声称生产作者已经自然触发过 reset。

复现时先离线编译 Rust lib test，再将生成的 unit-test 可执行文件传给集成测试（所有缓存和 TMPDIR 保持在 workspace）：

```bash
# 工作目录：deploy/science；GEWU_ENGINE_TEST_BINARY 是本次编译的 lib test 可执行文件
PYTHONDONTWRITEBYTECODE=1 \
TMPDIR=/mnt/data/zhanglinfeng/workspace/.tmp \
GEWU_ENGINE_TEST_BINARY=/mnt/data/zhanglinfeng/workspace/.cache/cargo-target/debug/deps/gewu_multiverse-bc49280ff109cffb \
/mnt/data/zhanglinfeng/workspace/acs-harbor-test/.venv/bin/python \
  -m unittest -v test_author_lifecycle_integration
```

没有显式提供 test binary 或没有 operator 权限时，该集成测试明确 skip，不以 mock 结果替代。Rust fixture 默认 ignored，必须由上述隔离 harness 显式驱动。新增 Rust 代码仅存在于 `cfg(test)`，无需替换生产控制器。

## 卡死观察修正

- 原来要求三个 session 才可能触发，无法覆盖不限轮数的长会话。现在三个 session 各至少三次共同失败，或当前一个 session 至少九次同类失败，均可成为候选。
- 保留至少一个小时无工作区变化、当前 runner 存活、无审核/ACS/构建/平台等待条件。同一源状态只尝试 reset 一次，变更后的状态仍有六小时冷却。
- Python traceback 用最后一个异常类型和消息归类，避免变化的调用行和 stdout 掩盖相同失败；原始输出不写入监控回执。
- 长于五分钟的本地进程视为计算中，暂不 reset；短诊断命令不会永远清零观察时长。negative control 不当作故障证据。
- 重型观察每五分钟一次，等待或无重复失败时不扫描工作区；扫描有文件数、目录数和时间边界，读取错误不会作为“无变化”证据。损坏的观察 JSON 返回 inspection_required，不使整个 watchdog 退出。

## 验证

- Python：dead_end_watchdog、review_dispatcher、role_reset、role_reset_online 合计 42 项通过；另外两项真实控制器集成通过。
- Rust：author_review 3 项（含两个真实子进程测试）、operator_hold 4 项、review_assignment_tests 4 项通过；均通过 workspace-local Cargo 工具链离线编译。
- Python AST、git diff --check 通过；按执行约束运行 node --check（本次没有 JavaScript 变更）。
- 历史 author-02、author-04、author-10 最近三个会话只读抽查：没有满足自动 reset 阈值。不能将历史有报错等同于已证实死循环。

尚需观察真实作者自然收到审核时停止、等待反馈后恢复，以及发生适当故障时 reset 后模型读取指导；此文不宣称生产行为已经验收，也不为验收而中断真实作者。

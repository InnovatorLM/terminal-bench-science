# 作者生命周期与预算看门狗复核（2026-11-03）

本轮只使用隔离临时目录、独立 UID 和真实本地子进程；没有启动生产宇宙，没有调用真实模型或 ACS，也没有修改生产角色状态。运行的是现有 Rust `Engine`/`RealSpawner` 测试入口和生产 Python watchdog/reset 实现；模型执行器是无网络的本地脚本，审核消息和经过时间由 fixture 提供。

## 修复

1. **后续题目超时不触发 reset**：作者账本永久保留旧 idea。预算看门狗原来总返回第一条超时记录，再因它已经 reset 而退出，导致同一作者后续超时题目无法触发。现在在寻找超时 idea 时排除已尝试记录，每个 idea 仍只尝试一次。
2. **损坏的 reset 历史使整个 watchdog 退出**：原异常处理再次读取同一个损坏 JSON，异常逃逸到服务主循环。现在保留损坏文件，报告 `inspection_required`，继续检查其他作者；不会用空历史覆盖旧记录或重复 reset。
3. **预算检查异常被静默丢弃**：主循环现在输出结构化 `author_budget_watchdog` 事件，记录 reset 或待人工检查结果，随后继续 dead-end 扫描。

没有重复接入新的 daemon；`start-universe` 仍启动现有 `command_watchdog`，由它调用时钟对账、预算看门狗及 dead-end 看门狗。

## 行为证据

隔离的真实 controller 集成测试验证了五个场景：

| 场景 | 实际断言 |
| --- | --- |
| 精确版本进入审核、要求作者修订 | 旧 generation 不暂停；匹配的 reviewing 停止进程；等待时不创建替代会话；waiting_author 后产生新 PID |
| 正式审核完成 | reviewing 停止作者；completed 后产生新会话；保留原工作文件及 idea clock |
| ACS / 平台等待 | 本地 runner 存活且有九次重复失败，但有实际 fixture 队列等待状态时，跨 48 小时观察仍保持 waiting；有效迭代时间不增加，不 reset |
| 超过 48 小时有效迭代 | `scan_clocks` 把 idea 标记 abandoned；预算看门狗通过真实 hold/ack 协议 reset；旧进程退出、PID/PGID 清除、新会话读取包含换思路要求的指令；账本未重置 |
| 重复失败且一小时没有源文件变化 | dead-end 看门狗触发真实在线 reset；清理独立角色 UID 的命令进程；保留工作区、原轨迹和 idea clock；同一源状态不重复 reset |

每个被停止的旧会话都检查唯一的 `run_finished=stopped` 记录。测试中发现旧断言仅等待 PID 文件删除，偶尔在 PGID 文件尚未删除时误报；现在等待整个进程及 PID/PGID 清理边界。这是测试同步修正，未改变生产清理逻辑。

## 复现与结果

工作目录：`/mnt/data/zhanglinfeng/workspace/gewu-multiverse/deploy/science`。

```bash
PYTHONDONTWRITEBYTECODE=1 \
TMPDIR=/mnt/data/zhanglinfeng/workspace/.tmp \
/mnt/data/zhanglinfeng/workspace/acs-harbor-test/.venv/bin/python \
  -m unittest -v test_author_budget_watchdog test_command_watchdog
```

结果：8 项通过，4.863 秒。覆盖连续两个 idea 超时、损坏历史保留且不影响其他作者、等待保护、身份验证、命令终止以及异常日志。

```bash
PYTHONDONTWRITEBYTECODE=1 \
TMPDIR=/mnt/data/zhanglinfeng/workspace/.tmp \
GEWU_ENGINE_TEST_BINARY=/mnt/data/zhanglinfeng/workspace/.cache/cargo-target/debug/deps/gewu_multiverse-bc49280ff109cffb \
/mnt/data/zhanglinfeng/workspace/acs-harbor-test/.venv/bin/python \
  -m unittest -v test_author_budget_watchdog test_author_lifecycle_integration
```

结果：9 项通过，32.825 秒，其中 5 项是真实 controller 集成，其余 4 项为预算单元测试。该二进制是现有离线编译的 Rust lib-test；没有改 Rust 源码或替换生产控制器。未设置 test binary 或缺少 operator 权限时集成测试会显式 skip，不能作为通过证据。

此前先运行的原有预算、命令 watchdog、role_reset、online_reset、dead-end 和集成测试共 40 项通过；本轮新增 6 项用例。`git diff --check` 通过。

## 结论边界

- 这里证明了暂停、恢复、等待排除和 reset 的软件链路；不冒充真实模型已按指令换思路，也不声称生产作者已自然触发过这些场景。
- dead-end 自动 reset 仍要求重复失败与至少一小时源状态不变同时成立；单纯没有产物、仍在研究或者正常长计算不会自动当作死循环。
- 角色处于 hold/ack 失败、损坏账本或证据不足时，保留工作并报告人工检查，不为消除报错而强行重置。

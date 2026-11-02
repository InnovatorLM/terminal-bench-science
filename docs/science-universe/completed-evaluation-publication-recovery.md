# 完成评测后的证据发布恢复

2026-11-02 的 `420e9b119cf0d741c809be4d80b86de6` 在 Harbor 完成后遇到本地 publisher 的热更新导入错误。模型和 verifier 已结束，reward=1、退出码 0、Pod 清理确认、轨迹和实际资源证据均在；但队列缺少最终 receipt，再次观察时返回了 `interrupted`。这属于平台交付错误，不是 ACS 执行失败，也不是模型错误。

`recover_completed_evaluation.py` 从已有不可变执行中恢复交付，绝不启动模型或容器。它要求原 worker 和 controller 已退出、原请求和注册快照/镜像/模型一致、源码未改变、原始 session 与规范化轨迹及 verifier/Pod 证据完整、清理已确认。任何条件缺失都不能合成分数。旧的部分发布目录和中断回执保留，原始 Harbor result、轨迹、Pod report 不修改；重新生成派生证据包、绑定、私有/角色可见结果，并记录恢复审计。

队列再次处理相同已完成任务时可自动尝试这一恢复；如果原 worker 仍活着，返回 running，不能发布一个终态 interrupted 覆盖它。对于已对角色发布过 interrupted 的历史任务，可由 operator 对精确 job 执行恢复命令，不能为了补交接重跑五次。

```bash
GEWU_UNIVERSE_ROOT=<当前宇宙目录> PYTHONPATH=deploy/science \
python -B deploy/science/recover_completed_evaluation.py \
  --queue-config <当前宇宙目录>/.config/science-evaluation/queue.json \
  --job-id 420e9b119cf0d741c809be4d80b86de6
```

六项离线恢复/队列回归通过，覆盖成功恢复、原始数据不变、幂等、活动 controller 拒绝、清理未知拒绝、源码变化或 native session 缺失拒绝、保留部分发布记录。模拟测试显式禁止启动子进程和读取真实凭证。

真实 job 已恢复为 `solver_complete / reward=1`，新增模型调用和云任务均为 0。恢复审计保存在该 job 的 `publication-recovery.json`；原队列 interrupted 回执保存在 `result-before-recovery-*.json`。该题其余四次结果为 1、1、0、1，完整五次为 4/5=80%，应按既定规则严格拒绝难度门槛，最终科学评审由 evaluator 完成。

## 恢复后更新审核结论

若 evaluator 已因缺失交付返回 `waiting_author`，调度器读取本批次精确 job 的 operator 恢复审计，并在结果内容改变时重新通知原审核者。固定快照、`batch_generation` 和五个 job ID 保持原值；更新仅用于补充审核判断，不重新启动五次模型评测。

原审核的 state、stage、reason 和时间保存在 `reviews_before_publication_recovery`，已经发现的题目缺陷继续保留。审核者忙于另一题时保存待通知记录，不覆盖旧结论；空闲后处理一次，恢复审计摘要用于去重。已正式接受的版本不受这一机制影响。离线回归覆盖忙碌等待、原五次绑定、旧缺陷保留、一次唤醒，以及错误/重复恢复审计的忽略。

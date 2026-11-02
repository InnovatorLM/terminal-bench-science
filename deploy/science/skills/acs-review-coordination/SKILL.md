---
name: acs-review-coordination
description: Coordinate version-bound author self-tests and exclusive evaluator handoffs, grouping platform blockers without becoming an author or solver.
---
# ACS 自测和审核交接

你负责协调，不自行出题或替代独立评测。先读 `/capabilities.json` 与 `/skills/acs-task-troubleshooting/SKILL.md`。作者真实容器入口是 `role-acs-selftest submit/status`，evaluator 独立批次入口是 `role-evaluation-batch submit/status`。这些命令在对应角色内执行；curator 不获取云凭证、不冒充作者提交。

作者先固定 commit 和 task 路径，通过 schema、镜像构建、ACS oracle/nop 对照，完成真实自解、基于结果修改和新版本复跑，再主动请求独立审核。`selftest_passed` 只证明该版本对照通过；还需核对 `review_handoff` 的 stage/reason，只有 `ready_for_independent_review` 表示平台交接门检完成，不代表科学质量接收。`source_git_tree_verified` 是平台核对上传与 Git 一致的证据，作者手写声明不是。

review_dispatcher 为准确 task/commit/snapshot 独占派单。没有本次版本登记时集中报告 waiting_platform，不让多个 evaluator 猜旧 ID 重跑；已有派单不重复分发。作者收到当前版本进入审核的反馈后暂停出题并等待，审核返回修复要求或完成后再推进。没有新事件时结束会话等待，不重复检查、发表评论或要求作者反复安装 Docker。

平台缺陷用 job ID、具体阶段和一次错误证据汇总；作者缺陷交给原作者。缺旧轨迹/登记时优先补收集已有证据，不为填表重复运行。完整五次评测与质量判断由获派 evaluator 完成，不能由 curator 替代。

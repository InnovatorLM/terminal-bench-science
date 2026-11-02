# 已合格题目的审核排队反馈

实际观察到 author-08 的 `fa3945a4` 已通过 self-test 门检和不可变版本登记，并在 `pending-selftest-reviews.json` 等待 evaluator，但作者的 `review-feedback.json` 被旧 PR #36 的 `author_container_preflight` 覆盖。这会要求作者重复自测，虽然真正缺少的是空闲审核角色。

修复后，派单器为尚无空位的合格请求发布 `waiting_platform / evaluator_capacity`，绑定 author、job、idea、commit、task、收到请求时间、evaluation ID 和 snapshot。明确说明无需重构、重测或再次提交，空位出现后自动派发。当前作者在此等待，旧 PR 不得覆盖这一精确版本状态。

成功 self-test 的交接重放不会清除派单器的排队状态。实际 evaluator 接手后可以替换该状态；新的作者请求或新的思路不会被旧队列等待误暂停。复用既有的精确版本等待机制，无需重启控制器或正在进行的 ACS 评测。

34 项相关离线回归通过，其中新增流程覆盖：无空位 → 发布当前版本等待 → 旧 PR 扫描 → 成功交接重放 → 空位释放 → 唯一 evaluator 接手，以及旧等待不能暂停新的作者请求。测试不调用模型或云服务。

在修复部署前的后续观察中，author-08 已获得 evaluator-01，并进入五次正式评测等待。因此该实例已自然越过排队阶段；不能把这次观察当作新排队保护已在生产触发的证明。此修复防止后续遇到同类容量等待时再次暴露旧反馈。

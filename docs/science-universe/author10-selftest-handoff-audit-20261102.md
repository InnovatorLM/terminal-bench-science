# Author-10 自测与交接审计（2026-11-02）

本次只读核对运行证据，并修复 staging 目录别名被误当作任务身份的问题。
未创建云任务、调用模型、修改历史回执、修改作者工作区或进行部署。

## 确认的执行证据

| 内容 | 前一次 | 最近一次 |
| --- | --- | --- |
| job | `c66f583c47b740f88559d8ee7d5bbfdb` | `73c220ec47444a2ab5e4e3f5e9c93815` |
| source commit | `5ead0fe40dc608e0978550b1b576cbbdb32940fc` | `5c9277c8fcdb8d052487d1f86ac36e0d72356066` |
| execution task | `iod-enso-attribution` | `iod-stage` |
| source task | `tasks/earth-sciences/ocean-sciences/iod-enso-attribution` | 同左 |
| idea | `enso-iod-attribution-2` | 同左 |
| 控制结果 | oracle=1、nop=0，均退出0并确认清理 | 同左 |

两份记录都已核实来源 Git 树。前一次完成早于最近一次提交，commit 和完整任务快照均不同。
调整 staging 别名检查后，现存两次任务通过只读迭代关联校验；这只证明真实先后执行及版本变化，
不替代对修改说明、五小时预算或科学质量的判断。

最近一次没有 `request_review` 或 `iteration` 字段，因此停在 `review_request_required`。
现有客户端只有 `submit/status`，不能给已成功的任务补交评审请求。再次 `submit` 会创建新任务并重建、重跑。

## 轨迹暴露的版本漂移

`author-10/run-1793603700351-0064/transcript.jsonl`：

- 07:38:53 UTC，第33轮：将最近一次成功回执写入题包内的 `authoring/completeness-review.json`，再次提交。
- 07:45:03：生成 `d5b80388ec107e86b43de878852dbf870aa09a4b`；第一次推送到本地工作仓库因当前分支被检出而失败。
- 07:45:59：随后推送 Forgejo 成功。PR 文本把新 head 和实际已测 `5c9277c8` 分别列出。

只读 Git diff 确认两版本唯一变化是 `authoring/completeness-review.json`。
客户端会打包题目下所有文件，快照哈希也包括该文件，因而补写回执制造了新任务版本。
已确认版本漂移；截至审计截面，尚未看到作者为这个新版本再次提交云自测，不能称为已发生的完整重跑循环。

## 已修正的别名问题

修改 `selftest_registration.py` 与 `selftest_iteration.py`，以及对应测试：

- 逻辑身份仍绑定已经验证的 source repository/task/idea/commit。
- execution task 可以是另一合法单段 staging 名；拒绝绝对路径、遍历、斜杠、空值等非法路径。
- 每次快照、构建回执、镜像仓库、digest、标签均仍按其原 execution task 验证，不重命名或重写证明。
- 前后迭代可以使用不同 staging 名；仍核对前一次冻结字节、时序、同作者/同任务以及确实变化的 commit 和快照。

验证：3 个身份/迭代测试和13个登记/交接回归测试通过；没有云或模型调用。
另对真实最近一次任务的状态、冻结快照、原构建回执与正式 Codex 模板执行只读 `prepare()`，验证通过。

## 最小后续方案：补交请求，不重复自测

新增作者入口，例如：

```text
role-acs-selftest request-review <successful-job> --previous-job <prior-job> --iteration-note <真实修改说明>
```

1. 请求由该角色自己的队列提交。平台从原成功 job 推导作者、idea、source commit/task、snapshot；禁止请求方替换执行身份。
2. 将补交请求追加保存为独立的根用户持有文件，记录原请求、原状态和请求内容哈希。保留原 `request.json/status.json` 不变。
3. 重用原来的来源、快照、镜像、控制结果、清理、真实迭代和预算校验，只运行交接/登记，不进入构建或 ACS。
4. 同一 job、同一补交内容幂等；错误关联或缺失真实迭代仍回到作者补证，历史预算观测缺失则等待平台修复。
5. 交接证据绑定原请求与补交文件；不能以新 flag 伪造旧任务当时已要求评审，也不能伪造作者的迭代说明。

Author-10 已存在可关联的前一次真实自测；补入口有机会直接复用最近一次成功执行。
预算证据、作者真实修改说明、最终评审版本选择仍须正常核验，本审计没有授予科学接收资格。

后续执行结果应写入题包之外的交接目录/评审附件，引用不可变 commit 和 job。
不要简单把所有 `authoring/` 从哈希剔除，也不要将新 head 的证明冒充旧已测版本。

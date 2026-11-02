---
name: harbor-acs-evaluation
description: Prepare a Harbor task for real ACS container execution, request platform execution, and inspect version-bound evidence.
---
# Harbor 真实容器自测

作者完成题包实现后、正式提交审核前必须使用本流程；评测者用它核查环境和基础运行证据。本 skill 是隔离角色版本，不要求访问宿主凭证或旧 unified-evaluator-pipeline 仓库。

1. 固定题目仓库、分支、commit 和 task 相对路径。核对 instruction.md、task.toml、environment/Dockerfile、tests/test.sh、solution/solve.sh 及其引用文件；做 schema、shell 语法、输入输出和依赖检查。尚未实现的提案不要求提前拥有完整题包。
2. 本地原型可以辅助调试，但改写绝对路径的目录模拟不能证明镜像可构建或 ACS 可运行。隔离工作区没有 Docker 不表示应跳过真实容器自测。
3. **作者执行入口**：先提交所有题包变更，再运行 `role-acs-selftest submit --task /workspace/<repo>/<task-path> --commit <完整40位commit>`，随后用 `role-acs-selftest status <job_id>` 查询。上传内容必须与该提交逐文件、执行权限一致；不一致会拒绝构建。队列不可用时，在自己的任务issue记录一次waiting_platform和实际错误，不得编造成功证据。
4. 平台应执行：格式校验→构建并发布 ACR 镜像→记录不可变 digest→ACS 启动→作者参考解→verifier 正对照及空/错误答案负对照→结果驱动修复→新版本复跑。请求默认 4c/8Gi，确认不足才最高 8c/16Gi；不使用 cn-beijing-f。
5. 等待返回 commit、镜像 digest、run ID、日志、verifier 输出、轨迹和清理记录。只接受与本次版本一致的实际证据；缺证据标为 incomplete/waiting_platform，不能写“容器通过”。ACS 运行时间不计入 48 小时主动迭代预算，但必须有真实 run ID 才可扣除。
6. 完成作者自解、反思修改及复跑后，再主动申请独立审核。作者自测不能替代 evaluator 五次独立评测。

结果驱动的复跑应显式关联已完成的同题自测：`role-acs-selftest submit --task /workspace/<repo>/<task-path> --commit <新完整commit> --previous-job <上次job_id> --iteration-note '<根据上次执行结果作出的实际修改，20至4000字符>'`。先确认客户端 `submit --help` 支持这两个参数；旧部署不支持时记录 waiting_platform。平台核对同作者、同仓库／题目、先后顺序、上次清理、提交和题包确实变化。修改说明是作者陈述，不能替代独立审核，也不会自动证明难度。没有完成记录或清理未知的作业不能作为复跑依据。

评测者已有入口为 /evaluation/catalog.json、/evaluation/inbox 和 /evaluation/results，只允许使用已登记 evaluation_id 与 snapshot_sha256；不能把旧题登记项用于新题。需要注册新版本时明确请求平台注册。

任何角色都不得读取宿主 kubeconfig、ACR/模型密钥，或把密钥写进题包、镜像、issue、日志。保留工作区原始证据；清理只针对本次 run 的资源。

## 稳定思路计时与自动交接

开始一个新出题思路时，先执行 `author-iteration-clock start <idea-id>`，保持这个 ID 跨分支、会话和迭代不变。每次自测都显式加 `--idea <idea-id>`；旧作业未绑定思路时，不得事后伪造绑定或重开思路来清零预算，记录 waiting_platform。

第一次自测：

```bash
role-acs-selftest submit --idea <idea-id> --task /workspace/<repo>/tasks/<task> --commit <完整commit>
```

确认上次真实执行结果后，做有根据的修改、提交新版本，再请求自动交接：

```bash
role-acs-selftest submit --idea <相同idea-id> --task /workspace/<repo>/tasks/<task> --commit <新完整commit> --previous-job <上次job_id> --iteration-note '<根据实际执行结果作出的具体修改，20至4000字符>' --request-review
```

平台记录实际 ACS 执行起止，合并重叠区间后扣时；不会采用作者自报 active_seconds，也不会因填写了等待理由就任意退款。暂不能验证的暂停可能使保守时间上界超额，此时应补充平台证据，不等同直接宣判实际工作超时。角色仍须遵守 48 小时主动迭代预算与放弃规则。

`selftest_passed` 只代表自测完成。继续查看返回的 `review_handoff`：`ready_for_independent_review` 才表示该提交的迭代证据、计时核验和不可变执行登记已接通；`waiting_author` 或 `waiting_platform` 时按返回 stage/reason 处理。即使 ready，也不代表科学质量或难度已被接收。作者须提交对应版本 PR，在 PR 正文明确写出准确 `tasks/<task>` 路径，并主动请求独立 evaluator。不要手填资格回执、借用旧登记或将自测当作五次盲测。

故障处理与等待去重见 `/skills/acs-task-troubleshooting/SKILL.md`。本入口的 oracle/nop 不调用 LLM、不衡量 DeepSeek 正确率；作者必须亲自完成解题与结果驱动迭代，不能用首次自测通过替代。

# 跨宇宙已接受题库

根目录：/mnt/data/zhanglinfeng/workspace/accepted-tasks/。独立于所有Universe运行目录，只保存正式接受的不可变版本。私有存储，不向作者角色挂载，不自动对外发布；不是异地备份。

## 收录和校验

台账必须为completed、formal_decision=accepted、scientific_acceptance=true；正式回执的key、commit、task、snapshot、evaluation、generation及review reason哈希必须与台账一致。原Harbor题包逐字节保留，并核验快照及可执行位。

现代回执按其五次job ID选择证据。历史人工接受无job列表时，只允许同evaluation_id和snapshot下恰好五个solver_complete、奖励0/1、无运行错误的作业；超过五个不能任选五个，进入pending。另存取消作业结果，不计入正确率。

逐次核验原生trial、verifier奖励、独立run ID、来源版本和清理确认，每次必须有真实trajectory和原生session。历史缺失的实际Pod观测明确标注，不用请求配置充当实测资源。统计同时给出历史正确率与严格证据完整数。

## 文件布局

tasks/题名/snapshot-sha256/archive-id/

- task/：标准Harbor题包，包括提交内的输入数据、环境、solution、tests。
- acs/job-id/native/：五次执行完整目录，包含trajectory、Codex原生会话、result、verifier、产物、配置和清理回执。
- acs/job-id/evaluator-packet/：实际交给evaluator的证据副本。
- acs/excluded-attempts.json：取消及未选中作业及计数排除说明。
- acceptance/：接受回执与审核台账快照。
- registration/：注册、镜像digest、构建回执、自测绑定和运行模板。
- selftest/：作者oracle/nop与交接证据。
- review/：原PR及讨论；不复制整个数据库或凭据。
- lessons/：已发布经验帖正文与来源索引。
- metadata.json、statistics.csv、manifest.json：来源宇宙、版本、五次结果、正确率、缺口及逐文件大小/源哈希/归档哈希/脱敏标记。

archive-id包含来源宇宙、版本和接受回执身份，跨宇宙同名题不会覆盖。全为独立副本，没有指向运行目录的符号链接或硬链接。保留的原始绝对路径仅作溯源，阅读归档不依赖它们。容器镜像保存digest/构建回执，不内嵌OCI层；未提交的外部大数据不自动下载。

凭据字段和值脱敏，保留完整求解步骤。源哈希与归档哈希分别记录，不把脱敏副本声称为字节完全相同的原始证据。原题包不做静默变更。

## 持续归档

start-universe会注册来源并保证单个跨宇宙归档进程运行，每60秒扫描。先写.staging、校验后原子发布，再更新index.json。失败记录在sync-status.json.pending，不阻塞主调度，也不删除已归档成果。对已归档版本再次同步只校验并跳过，不覆盖。

归档保留当时讨论和经验帖快照，后续评论不改写旧归档。源码和工具程序分别位于gewu-multiverse/deploy/science/accepted_task_archive.py及accepted-tasks/tools/accepted_task_archive.py。

工作区根目录下执行：

    python -B accepted-tasks/tools/accepted_task_archive.py register --universe /absolute/workspace/universe
    python -B accepted-tasks/tools/accepted_task_archive.py sync
    python -B accepted-tasks/tools/accepted_task_archive.py verify
    python -B accepted-tasks/tools/accepted_task_archive.py ensure-watch

--root可指定其他工作区内的独立题库。机器重启后，正常启动Universe或执行ensure-watch恢复归档进程。没有新增模型或ACS测试调用。

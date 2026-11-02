# Verifier 构建定义前置检查

2026-11-03 的改动只做本地非执行检查及构建准备。没有启动宇宙、Docker 构建、ACR 请求、ACS 任务或 LLM 测试。部署后仍需要观察真实请求，不能将本次离线回归称为云端验收。

## 修复的问题

原来的 `require_contexts()` 遇到缺失 Dockerfile 直接略过；独立使用 image builder 时，也不会单独拒绝缺失或空的 `tests/test.sh`。更关键的是，显式声明 `[verifier.environment].docker_image` 时，builder 没有采用该镜像，而是使用 `tests/Dockerfile` 或回退到 agent official 镜像。这会把作者声明的 verifier 环境静默改掉。

现在 worker 在获得构建锁和启动云端前，检查冻结版本中的 `task.toml`、`environment/Dockerfile`、`tests/test.sh` 和静态 COPY/ADD 输入。image builder 的直接入口也执行相同检查，在实例化 Docker 客户端之前写出本地预检回执；失败归 `task_build_context_invalid` / `failure_owner=task`，不归为 ACS 故障。worker 为兼容已有反馈使用 `author_build_context_missing`，同样明确 `failure_owner=task`、`cloud_execution_started=false`。

## 保留的合法路径

| 冻结版本的 verifier 定义 | 使用方式 |
|---|---|
| 显式 `verifier.environment.docker_image` | 优先使用指定镜像；有旁置 Dockerfile 也不替代它。构建一个派生镜像，使用原有 resolver 将外部 base 绑定到不可变 digest，并记录原始来源与解析结果。 |
| 没有显式镜像，有非空 `tests/Dockerfile` | 使用 tests 构建上下文，检查其本地 COPY/ADD 输入。 |
| shared，未声明独立 verifier.environment，且无 tests/Dockerfile | 保留原来 FROM official 的回退。 |
| separate，但未声明 verifier.environment，且无 tests/Dockerfile | 保留 Harbor 所支持的“使用 agent 环境的新副本”回退。 |
| 显式声明 verifier.environment，但无镜像也无 tests/Dockerfile | 本地拒绝，指出缺少 verifier 构建定义，不进入 builder 或 ACS。 |

显式镜像优先的选择依据当前安装 Harbor 的 `task_env_config.docker_image` 语义；UniverseACK 最终仍只拉平台发布的 digest-bound 镜像。回执记录 `verifier_build.source`，使选择可审计。冻结题包不被改写。

构建回执不再从“是否存在 tests/Dockerfile”猜测测试脚本已上传；`standard_tests_uploaded_by_harbor=null`、`tests_materialization_verified=false` 明确留待实际 Harbor 执行配置及证据核验。

预检不执行任务脚本。变量展开、通配符、`.dockerignore`、Docker frontend/heredoc 等复杂语义仍明确交给真实 builder；静态输入存在不等于镜像能成功构建，更不等于 verifier 能运行或题目质量合格。此路径继续服务现有单个 root tests 上下文的任务；不宣称已新增多步骤独立镜像构建支持。

## 离线回归范围

回归覆盖真实本地 Git 冻结上传在缺 verifier 定义／空入口时不调用 builder 与 ACS；直接 builder 在缺定义时不创建 Docker 客户端；shared/separate 继承回退；有无旁置 Dockerfile 的显式镜像优先与 digest 绑定；保留的 Dockerfile/COPY 上下文；队列并发、继承锁与非云端统计。所有临时文件都在 workspace 下，测试不读 API 凭证。

本次共通过 61 个不同的离线测试（两组 42/22 项含 4 项重叠，另加 1 项完整 mock 构建；最后的失败分类收紧另复跑对应测试）。现有十进程并发、构建锁继承、源版本交接测试均包含在内。空队列 supervisor 的真实本地子进程产生 0600 的队列快照，并在 SIGTERM 后发布停止标记；没有 self-test 请求或云端执行。

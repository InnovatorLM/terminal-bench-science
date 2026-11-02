# 镜像构建前检查 COPY 输入

平台分别以 `environment/`、`tests/` 为构建上下文。`tests/Dockerfile` 中的 `COPY wheels /opt/wheels/wheels` 要求 **冻结 commit 的 `tests/wheels/`** 存在；角色环境安装了依赖、或者 `environment/wheels/` 存在，均不能满足它。Git 不保存空目录，下载完但未提交的 wheel 也不在冻结题包内。

提交前逐项核对两个 Dockerfile 的本地 `COPY`/`ADD` 输入：相对路径、真实文件、Git 跟踪状态、`.dockerignore`，以及容器 Python 版本和 wheel ABI。可对具体路径使用 `git ls-tree -r <commit> -- <task>/tests/wheels`；不必导出整个仓库或换题目名称。若使用在线安装，就从源 Dockerfile 去掉失效的离线目录引用并验证新的依赖安装方式。

`author_build_context_missing` 表示平台已在构建前发现确定缺失的输入，查看 `docker_context_preflight.missing_sources`，修复后形成新 commit 再提交。它不会创建 ACS 容器。预检只覆盖明确的本地路径；变量、glob、复杂 frontend 和 `.dockerignore` 仍可能在真实构建时失败，预检通过不是构建通过。

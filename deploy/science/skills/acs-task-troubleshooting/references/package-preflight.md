# 本地演练与题包预检

## 角色环境与题目镜像

角色里的 `/home/role/.local/env` 用于研究与本地演练，依赖不会自动进入 Docker 镜像。题目中的 `/app/data`、`/tests`、`/opt/wheels` 由各自 Dockerfile 和 Harbor 挂载产生，角色工作区没有这些绝对路径不说明题包缺文件。不要反复向角色根目录写这些路径，或把目录模拟称为容器通过。

本地需要软件时，用 `role-install <conda包>` 或明确解释器的 `python -m pip`。`cmp` 属于 diffutils，缺失时可安装 `role-install diffutils`。本地演练可使用单独 staging 和任务已有的路径参数；必须保留原题的输入输出契约，最终用 `role-acs-selftest` 验证真实镜像。独立 verifier 只收到声明的 artifact，不共享 solver 整个文件系统。

## pytest 与 CTRF

安装日志中的 “Successfully installed” 不证明插件可用。用实际执行测试的同一个解释器检查版本与 pytest 入口点，不猜 import 名：

```python
from importlib.metadata import distribution
d = distribution("pytest-json-ctrf")
print(d.version)
print([(e.name, e.value) for e in d.entry_points if e.group == "pytest11"])
```

例如已观察到 `pytest-json-ctrf` 0.6.1 的入口是 `ctrf.main`，不是 `pytest_ctrf` 或 `pytest_jsonctrf`。随后检查 `python -m pytest --help` 中的 `--ctrf`，运行一个小测试并实际读取 CTRF JSON 的测试数量/成功失败统计。使用题目约定的版本；角色的默认 pytest 版本不替代镜像中的固定版本。

若 pip 报某个传递依赖 `from versions: none`，保留最早报错、包名和目标 Python/平台。这个终行不足以证明整个 PyPI 不通，也不证明包不存在。为离线镜像准备 wheelhouse 时应包含完整传递依赖，并在匹配目标 Python/ABI 的干净环境下安装验证；只放 pytest 和 CTRF 两个 wheel 仍可能缺 pluggy、packaging 等依赖。不要把本地装好插件当作容器已装好。

## 提交内容与版本

当前客户端打包 `--task` 指向的实际目录，平台按 `--commit` 的 Git 树逐文件/执行位验证。运行时生成的 `__pycache__`、未提交输出、被 `.gitignore` 忽略的证据文件仍可能进入上传，从而被拒绝。自测日志和临时环境放在题包外；提交必要的源码、数据和版本证明，避免把缓存塞进镜像。

提交前只检查当前 task 路径的 tracked 修改、未跟踪与 ignored 文件，并比较执行位；不要全仓库反复 checkout/reset。若使用干净 staging，客户端已支持 `--source-repository /workspace/<repo> --source-task-path tasks/<path>` 绑定源仓库；staging 仍须对应完整 commit，不能手改原快照或用别的题目登记代替。

## 先验证结果，再判断失败

负对照故意失败时，把该分支的退出码与预期对照一起记录；不要将 `pytest` 的预期非零当成平台故障。正对照必须实际通过，必须有新的 reward/报告文件，不能读取上次残留。初始化解释器或安装插件失败后立即停止当前演练，不继续运行后面的命令用最终 exit 0 掩盖错误。命令管道被 `head` 提前关闭产生 141，也应与任务程序报错分开。

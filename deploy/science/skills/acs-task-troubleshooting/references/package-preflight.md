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

固定版本 NumPy 等包还要核对目标 Python 版本是否受支持，以及 wheel 的 ABI/架构。只修复实际依赖，不因安装报错改变科学题意或 JSON/CSV 输出契约；`json` 是 Python 标准库，不是需要 pip 安装或删除的第三方依赖。

若日志已取得包的 metadata、选中与目标 Python/ABI 匹配的 wheel，随后在 `files.pythonhosted.org` 等下载主机报 `ReadTimeoutError`，先按构建阶段依赖下载超时排查，不能归为包不存在、版本不兼容或 ACS 判题网络故障。记录最早失败命令、主机、下载进度和退出码，交平台检查构建容器的下载链路；也可准备包含完整依赖且已在目标环境验证的 wheelhouse。增加超时或重试只能作为有边界的尝试；同一快照反复排队不会补齐缺失依赖，不要用删除 NumPy 或改题意绕过问题。

## 提交内容与版本

当前客户端打包 `--task` 指向的实际目录，平台按 `--commit` 的 Git 树逐文件/执行位验证。运行时生成的 `__pycache__`、未提交输出、被 `.gitignore` 忽略的证据文件仍可能进入上传，从而被拒绝。自测日志和临时环境放在题包外；提交必要的源码、数据和版本证明，避免把缓存塞进镜像。

提交前只检查当前 task 路径的 tracked 修改、未跟踪与 ignored 文件，并比较执行位；不要全仓库反复 checkout/reset。若使用干净 staging，客户端已支持 `--source-repository /workspace/<repo> --source-task-path tasks/<path>` 绑定源仓库；staging 仍须对应完整 commit，不能手改原快照或用别的题目登记代替。

`--commit` 仅声明待校验版本，不会替换实际上传的文件。需检查历史版本时按 [版本交接](version-handoff.md) 仅导出该 task，避免整个仓库的 archive、checkout 或遍历历史。

## 先验证结果，再判断失败

负对照故意失败时，把该分支的退出码与预期对照一起记录；不要将 `pytest` 的预期非零当成平台故障。正对照必须实际通过，必须有新的 reward/报告文件，不能读取上次残留。初始化解释器或安装插件失败后立即停止当前演练，不继续运行后面的命令用最终 exit 0 掩盖错误。命令管道被 `head` 提前关闭产生 141，也应与任务程序报错分开。

负对照没有 solver 输出是需要显式处理的预期情形：创建 verifier 日志目录，清除旧结果，明确写本次 reward=0 和缺输出诊断。不要让 `set -e` 在产生诊断前因文件不存在退出。初始化或依赖异常仍应暴露为异常，不能一律写 0 掩盖环境故障。

Harbor 将整个 `solution/` 上传到 `/solution`，所以 `solution/solve.sh` 调用同目录 Python 文件时应使用 `/solution/solve.py`，或根据脚本自身位置解析路径；运行目录 `/app` 不会使 `/app/solution/solve.py` 自动存在。先读 `agent/oracle.txt` 和 `agent/exit-code.txt`：若参考解因路径错误退出，修改 `/tests` 权限不会产生缺失的答案文件。

独立 verifier 只检查 `artifacts` 声明并实际成功传递的输出，如 `/app/relaunch_validation.json`。先确认 oracle 真的生成了文件，再确认本地 `artifacts/` 里有相应字节，最后确认 verifier 使用相同路径。旧 ACK 下载器可能在未收到文件时返回成功，因此旧 `manifest.json` 的 `status=ok` 不能单独证明文件存在。对于 nop 的预期缺输出，判题脚本应明确输出缺文件诊断和零分，不能在 `test -f` 处静默退出。

`DownloadVerifierDirError` 的内层若是 `Missing verifier reward`，表示旧适配器把缺评分输出归成下载失败；不证明 ACS 网络有问题。新适配器保留已取得的日志并交 Harbor 报 `RewardFileNotFoundError`。两种情况下仍没有可计入正确率的分数，必须解决实际退出原因；不能手工为历史失败补写 reward。

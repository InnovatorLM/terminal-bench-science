# author-06 构建下载失败诊断（2026-11-02）

旧 self-test `8342a74713814c35b89871c7ec693e99` 在构建任务镜像时下载 NumPy wheel 超时。失败发生在 ACS oracle/nop 执行之前，不是模型答错，也没有证据表明是 ACR 鉴权或题目网络策略不兼容。增加 worker 可以减少队列等待，不能消除此类依赖下载阻塞。

本次只读取旧作业的冻结输入、状态和日志；未修改作者题包、历史回执或运行服务，未调用云端。新作业 `4733c59cffd44a2bb532c06ccb9290fe` 不在此次诊断或修改范围内。

## 冻结对象与证据

| 字段 | 值 |
| --- | --- |
| idea | `geoid18-control-mark-audit` |
| 源仓库及路径 | `terminal-bench-science` / `tasks/earth-sciences/geosciences/geoid18-mark-audit` |
| source commit | `d9a8e37e6613d9dc89d48d48d739bc3b758d929b` |
| execution commit | `327a46f1077dd6dd9a5f1564199f926a791108d5` |
| snapshot SHA256 | `9fa1eb29b66f853a501cd936cb00aa22ac1d194d0a85827ef2cef97f6cdda312` |
| Git 字节校验 | `source_git_tree_verified=true` |
| 任务网络预检 | 通过；agent/verifier 均为 public，独立 verifier |
| trial 结果 | 没有 `runs/*/*/result.json`，尚未执行 oracle/nop |

证据根目录：工作区下 `.tmp/gewu-terminal-bench-science-runtime-live/.state/gewu-multiverse/author-selftest/8342a74713814c35b89871c7ec693e99/`。

`build/docker-01.log` 至 `build/docker-04.log` 记录基础镜像拉取及检查成功。`build/docker-05.log` 中第一条实际失败为：

```text
Step 2/10 : RUN pip install --no-cache-dir numpy==1.26.4
Downloading numpy-1.26.4-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl (18.0 MB)
... 0.5/18.0 MB ...
pip._vendor.urllib3.exceptions.ReadTimeoutError:
HTTPSConnectionPool(host='files.pythonhosted.org', port=443): Read timed out.
The command '/bin/sh -c pip install --no-cache-dir numpy==1.26.4' returned a non-zero code: 2
```

`build.log` 的 `ValueError: Docker step 5 failed` 是包装层错误；应使用上述原始 Docker 日志定位。日志已选中 Python 3.12 AMD64 的兼容 wheel 并开始下载，因此不能把它解释为 NumPy 不支持该环境。Verifier 镜像中的 pytest 安装尚未执行，不能宣称 verifier 构建已通过。

## 责任边界与处理建议

已证明构建容器到依赖下载服务的正文传输超时；仅凭这份日志，不能进一步确认是构建机出口、链路拥塞还是远端 CDN 的问题，也没有 CPU/内存不足的证据。平台应检查构建阶段依赖交付链路，保留下载主机与首个失败日志。可采用经过实际干净安装验证的完整 wheelhouse 或受控依赖缓存，减少每道题重复联网下载。作者仍需将必要文件纳入正确 Docker context 并提交，不能把角色环境中已经安装的包当成镜像依赖。

现有 `acs-task-troubleshooting` skill 已覆盖目标 ABI、完整 wheelhouse、角色环境与镜像隔离；本次在 `references/package-preflight.md` 补充“metadata 与 wheel 解析成功后发生正文读取超时”的判别，避免继续误改题目或把失败计入模型正确率。

## `classify_trial` 现状及建议（未改代码）

只读回放 author-04 的两个旧作业 `57a633d8b8164808b0a4d88f26e0ed7a`、`7a6cf3d4790e4f13839b72e26a96d431` 共四个 trial：旧异常 `DownloadVerifierDirError` 均得到 `infrastructure_error / trial_exception_before_verifier`。将新适配器可返回的 `RewardFileNotFoundError` 输入同一分类器，也得到该结果。

保持 `scored=false / reward=null` 是正确的；但 `before_verifier` 不能准确表达“verifier 已经执行、却没有产生 reward”，`infrastructure_error` 也不能单独证明平台责任。旧 author-04 的已知证据包括 oracle 引用了不存在的 `/app/solution/solve.py`，判题脚本又因缺输出提前退出；此时题包缺陷与旧适配器误报都存在。

建议后续按证据分别表示：

1. 构建失败在 worker/build 层分类；author-06 没有 trial，不能伪造 trial 或通过 `classify_trial` 补分。
2. 缺 reward 使用明确的“verifier 输出缺失、待判因”类别，与真实下载传输异常分开。兼容旧异常时查看内层 `Missing verifier reward`，保留原始异常和日志。
3. 是否属于题包、平台或预期负对照，结合 oracle 退出码、verifier 日志及实际 artifact 字节判断；缺 reward 本身不自动证明任何一方责任。
4. 历史回执保持原样，新增诊断记录即可；不能补写 reward 将未完成运行变为模型失败。

本次未修改分类器，未重新运行题目。诊断回放为本地离线检查，没有调用模型或 ACS。

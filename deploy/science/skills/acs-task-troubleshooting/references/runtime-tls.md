# 模型连接与证书检查

适用于 Codex 持续输出 `Reconnecting... waiting for network`、没有模型回答或工具调用的运行。保留 job、实际 Pod、冻结版本和原始连接日志，交平台诊断；不要让五次同根因故障跑满四小时后算作模型答错，也不要自行重复提交。

平台应在**发生失败的同一 Pod**检查 DNS、系统 CA 文件和启用证书验证的 HTTPS 请求。宿主机 API 成功不能证明任务镜像的信任库完整。`CERTIFICATE_VERIFY_FAILED` 加上缺失 CA 文件是信任库证据；仅 DNS 解析成功不证明 HTTPS 可用。无密钥访问模型 endpoint 返回 401/403 可以证明本次 TLS 握手成功，但不证明账户授权或真实推理成功。不得打印密钥、使用 `verify=False` 或关闭证书检查来绕过。

最小题目镜像可能不带公共信任根。平台 runtime 可安装公共 CA bundle，并让实际 agent 使用 `SSL_CERT_FILE` 等信任路径；保留 bundle 摘要、TLS 检查和实际模型轨迹。公共证书不是登录凭证，不向作者复制模型密钥。

本部署的 Harbor agent factory 优先选择已知 `name`，再读取 `import_path`。同时写 `name=codex-preinstall` 和自定义 `import_path` 会绕过 adapter。平台使用自定义 agent 时应清空竞争的内置名称，并检查真实 factory 创建的类。简单题得分正常本身不能证明 adapter 被执行；还要看到对应准备回执和模型调用证据。

修复后先用已有简单题验证，再恢复受影响运行。保留被取消作业的日志、清理结果及替代关系，不篡改旧 reward，也不把平台故障当作难度证据。

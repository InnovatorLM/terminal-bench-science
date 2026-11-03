# Harbor 公网评测服务

更新：2026-11-03。实现位于 deploy/science/harbor_public_*.py，执行端使用维护中的 evaluation_queue.py。

## 公网入口

- 浏览器/API：<https://nlb-2vwqetbd2t1ieqwdum.cn-beijing.nlb.aliyuncsslb.com>
- 在线指南：<https://nlb-2vwqetbd2t1ieqwdum.cn-beijing.nlb.aliyuncsslb.com/guide>；无需登录，可直接分享。页面顶部提供 Markdown 原文。
- 当前公网 IP：47.94.155.222、47.95.113.113。使用域名以匹配 HTTPS 证书和后续 DNS 调整。
- 原 SSH 域名 nlb-o9iq643huhstroketg.cn-shanghai.nlb.aliyuncsslb.com 当前解析为 139.196.14.26、139.196.54.8。这是原 NLB 的公网入口，不等于 DSW 主机直接绑定的网卡地址。原 SSH NLB 未修改；当前账号没有权限管理其监听器，因此评测使用独立 NLB。
- 原 10.224.210.21:8787 是历史内网服务，不能作为普通公网用户的访问地址。

打开新入口，选择题包和尝试次数，点击“上传并评测”。无需全局服务密码。提交后点击“保存任务凭证”；凭证中有该任务专属 token，持有者可查看结果、下载轨迹。浏览器恢复最近一次任务，旧任务应保留各自凭证。请勿公开凭证。

## Agent、模型与流程

旧 mini-swe-agent 限制来自历史 operator 固定模板，并非 Harbor 格式要求。当前使用 **Codex CLI 0.164.0** 和指定的 **DawnFM / deepseek-v4.1-flash-1017**。Codex 是执行工具链，模型不是默认 OpenAI Codex 模型。模型、命令、集群和镜像由服务端配置。返回的 harness_version、镜像 digest 和源码 hash 是每次实际记录。

~~~text
HTTPS 上传 → 校验及冻结题包 → 构建并发布 ACR 镜像
  → ACS 运行 Codex → verifier 评分 → 保存轨迹、清理执行 Pod
  → 带任务凭证查询结果 / 下载证据 ZIP
~~~

保留 ACR 是为了给 ACS 提供不可变镜像；调用者不需要自己的 ACR 账号。当前仍依赖管理员的 ACR，未实现免镜像仓库路径。

## 题包与容量

支持 .tar、.tar.gz、.tgz、.zip；可直接包含以下内容，或外包一层目录。每包一道题。

~~~text
my-task/
├── instruction.md
├── task.toml
├── environment/Dockerfile
├── tests/test.sh
└── solution/solve.sh          # 可选，不执行参考解
~~~

包含脚本和 Dockerfile 引用的全部数据、依赖。缺少参考解时仅在执行副本中添加不执行的兼容入口；返回上传 hash、执行副本 hash 和 reference_placeholder: true，不会声称运行了 oracle。

支持独立评分镜像：设置 `[verifier] environment_mode = "separate"` 并提供 `tests/Dockerfile`，其构建上下文为 `tests/` 的冻结副本，评分镜像单独构建和发布。做题镜像使用 `environment/` 的冻结副本；Codex/rg 在另一个 runtime 上下文中添加，不受题目 environment/.dockerignore 影响。若明确声明 verifier.environment.docker_image，则该镜像声明优先于 tests/Dockerfile。

| 项目 | 当前限制 |
| --- | --- |
| 压缩包 | 最大 100 MiB |
| 解压内容 | 最大 128 MiB、10,000 个成员 |
| 尝试次数 | 默认 5 次独立并行；可选 1 次 |
| 执行资源 | 每次 4 CPU、8 GiB 内存、30 GiB 临时存储 |
| 时间 | Pod / agent 最长 4 小时；构建另计、最长 2 小时 |
| 排队 | 全局最多 10 个未结束任务，同一来源 IP 最多 2 个 |
| 每日容量 | 全局滚动 24 小时最多 20 个任务 |
| 调度 | 一次一个上传任务，内部最多 5 次并行试验 |
| 模型 | 每次试验最多 1,000 次请求；全局并发 8 |

支持与 ACS 兼容的 Linux CPU / Dockerfile 题目。GPU/TPU、Windows、外部 MCP、读取主机 skills、插值读取主机环境秘密不受支持。拒绝路径穿越、软硬链接、设备等特殊条目。支持 Harbor 格式不保证所有依赖都能构建或联网。

verifier 写 /logs/verifier/reward.txt（单个有限数值），或 /logs/verifier/reward.json，例如 {"reward": 0.75, "accuracy": 1.0}。保留小数和各指标；仅输出 PASS 不会产生分数。空 stdout 可以正常，缺失必要日志则属于证据不完整。

## API 示例

GET /health 返回后端存活和 accepting_uploads；GET /gateway-health 返回网关和反向连接状态。HTTP 跳转 HTTPS。上传是原始二进制请求体，**不是 multipart**；兼容旧 JSON {filename, data: base64, attempts}。

远程用户修改题包路径后运行下面的 Python 示例。凭证和结果写入当前目录的 harbor-client；每道新题使用新的目录/幂等键。

~~~python
import hashlib
import json
import os
from pathlib import Path
import time
import uuid
from urllib.request import Request, urlopen

base = 'https://nlb-2vwqetbd2t1ieqwdum.cn-beijing.nlb.aliyuncsslb.com'
archive = Path('my-task.tar.gz')
out = Path('harbor-client')
out.mkdir(mode=0o700, exist_ok=True)
receipt_path = out / 'receipt.json'
key_path = out / 'idempotency-key.txt'
if not key_path.exists():
    key_path.write_text(str(uuid.uuid4()))
if not receipt_path.exists():
    req = Request(base + '/tasks', data=archive.read_bytes(), headers={
        'Content-Type': 'application/octet-stream',
        'Idempotency-Key': key_path.read_text().strip(),
        'X-Harbor-Attempts': '5',  # 或 '1'
    })
    with urlopen(req, timeout=180) as response:
        receipt = json.load(response)
    fd = os.open(receipt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(receipt, f)
else:
    receipt = json.loads(receipt_path.read_text())
headers = {'Authorization': 'Bearer ' + receipt['token']}
while True:
    with urlopen(Request(base + receipt['status_url'], headers=headers), timeout=30) as response:
        result = json.load(response)
    print(receipt['run_id'], result['status'])
    if result['status'] in {'completed', 'failed', 'interrupted'}:
        break
    time.sleep(10)
(out / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
if result.get('artifacts_url'):
    with urlopen(Request(base + result['artifacts_url'], headers=headers), timeout=180) as response:
        data = response.read()
    assert hashlib.sha256(data).hexdigest() == result['artifacts_sha256']
    (out / (receipt['run_id'] + '.zip')).write_bytes(data)
~~~

提交成功 HTTP 202，含 run_id、token、status_url、artifacts_url。同次重试保持 key、题包字节、次数不变；key 按来源 IP 隔离，换 IP 不保证复用，优先使用 receipt。400：参数/题包错误；403：凭证错误；429：暂停/容量限制；503：连接恢复中。

## 结果解释

状态：queued → validating → building → evaluating → completed / failed；工作进程停止且不能继续时为 interrupted。

- completed：所有试验完成执行、评分、证据检查且清理确认；分数为 0 也可以正常完成。
- trials[]：保留每次状态、reward、完整 rewards 指标、token 使用量、轨迹、清理及诊断。失败试验不隐藏。
- summary.mean_reward：有标量 reward 的均值；passed_trials 只计 reward 恰为 1 的次数，不适合所有评分量纲。
- failed / interrupted：读取 error 和诊断。基础设施失败不能当作模型答错。校验/构建早期失败没有 agent 轨迹，但正常异常收尾会生成诊断 ZIP；工作进程被强制终止时可能仍需管理员补收。
- ZIP 包含 result.json、逐次 verifier 输出、轨迹及镜像/Pod/清理证据。最终 API 状态有 ZIP SHA-256；ZIP 内结果不含自身 hash。scientific_acceptance: false 表示自动执行不等于人工科学验收。

构建失败排查：使用原任务 token 查询 `/tasks/<run_id>`，查看 `build_diagnostics.first_error`、`invocation.returncode`、`docker_commands` 和失败阶段。用同一凭证下载 `/tasks/<run_id>/artifacts`，其中 `diagnostics/` 保存完整保留日志（敏感值脱敏）、各次命令/返回码、Dockerfile 与 .dockerignore。新任务分别保存 stdout/stderr；combined 日志按 stdout、stderr 顺序拼接，不表示交错时间顺序。旧任务可能只有合并输出，无法事后恢复流边界；旧命令缺失会明确标注，不能把重建推断当成当时的命令记录。数据仍由任务 token 保护，日志不会发布在公共指南中。

## 部署与运维

工作区：/mnt/data/zhanglinfeng/workspace。

| 内容 | 路径/标识 |
| --- | --- |
| 代码 | gewu-multiverse/deploy/science/harbor_public_*.py |
| 启动入口 | gewu-multiverse/deploy/science/start-harbor-public |
| 配置 | .config/harbor-public/settings.json |
| 运行数据 | .state/harbor-public/runs/ |
| Supervisor | .config/harbor-api/supervisord.conf（含凭据，勿公开） |
| Kubernetes namespace | harbor-public-linfeng |
| Service / Deployment | harbor-public / harbor-public-gateway |
| 部署清单 | .state/harbor-public/gateway-service.json、gateway-deployment.json |
| TLS 状态 | .state/harbor-public/certificate-status.json |
| 验收记录 | .state/harbor-public/public-verification.json、recovery-verification.json、final-verification.json |

公网 NLB 80/443 → ACS gateway → 认证反向连接 → DSW 127.0.0.1:8790。harbor-public-tunnel 转发私有桥接端口，harbor-public-connector 转发请求。网关只有桥接密钥和 TLS 密钥，没有模型/云管理员凭据。

Supervisor 托管 harbor-public、harbor-public-tunnel、harbor-public-connector、harbor-public-certificates，退出自动重启。API 重启接管存活的独立 worker；停止的 worker 标为 interrupted，管理员核查清理后重新提交。网关单副本 Recreate，避免内存桥接队列跨副本分流；替换有短暂连接中断，应重试查询。

settings.json 中 accepting_uploads=false 暂停新提交，不取消运行任务；true 恢复，动态读取。模型令牌按任务短期签发并在结束时吊销；额度落盘，API 重启不重置。长期 DawnFM 密钥只在本机后端。

Let’s Encrypt 证书到期 2027-01-31。维护进程每 12 小时检查，不足 30 天时续期、更新 Secret；网关每分钟检查证书变化。需保持公网 80 和 ACME challenge 可用。证书签发已验证；未来续期仍需监控，不以安装程序代替实际续期记录。

网关有 HTTPS 就绪/存活探针；TLS 握手在有超时和连接上限的线程内，避免 TCP 健康检查阻塞接收循环。

### Git 与宇宙报告的转发

同一网关的公网 3443 端口转发 Forgejo（本机 127.0.0.1:3000）；443 上的 `/universe/` 转发报告站点（本机 127.0.0.1:8791）。`http://47.94.155.222/git/team/terminal-bench-science` 跳转到 Git 的 HTTPS 地址。Git 登录信息保存在工作区 `.config/universe-observatory/git-login.json`（0600），不得复制到公共报告或仓库。

2026-11-03 修复了连续浏览 Git 后出现 `Backend reconnecting; retry shortly` 的故障：Forgejo 的 `/user/events` 是不会结束的 SSE 通知响应，而桥接通道读取完整响应后才返回；8 个转发线程被占满后无法继续拉取请求，网关因此判定后端断连。该状态下 Supervisor 进程仍可显示 RUNNING，应同时检查公网 `/gateway-health` 的 `backend_connected` 与本地 Forgejo `/api/healthz`。

转发器现在仅在 Git HTML 的客户端配置中将 `notificationSettings.EventSourceUpdateTime` 设为 0，启用 Forgejo 自带的通知轮询；通知查询间隔为 10–60 秒。遇到旧标签页发起的 `text/event-stream` 响应时，在读取正文前关闭连接并返回 204，避免持续占用线程。上游异常立即返回 502，且不自动重放写请求。修改 HTML 后清除旧的校验与长度响应头，保留各条登录 Cookie。

修复代码同时保存在本机与 Kubernetes `gateway-code` ConfigMap 中；此次只需重启 `harbor-public-connector` 加载转发逻辑。7 项回归测试包含 8 个无限通知响应不能阻塞普通页面、通知配置替换、登录重定向及 Cookie、上游写请求失败不重试，以及 TLS 接收循环。现场诊断及浏览器验收记录位于工作区 `.state/universe-observatory/private/git-relay-repair/`，不对外发布。

北京时间 2026-11-03 14:34 的浏览器验收通过：登录后连续浏览 10 次 Issue、打开 4 个标签页并再次刷新全部返回 200；20 次通知轮询均成功，没有 `/user/events` 请求，也没有 429/502/503/504。报告页和已有 Harbor 健康检查均为 200，网关保持连接，宇宙控制器处于运行状态。该轮 Issue 加载耗时 17.6–29.3 秒，本地 Forgejo 日志显示后端处理占据主要耗时；本次验收不代表该性能问题已解决。

这是有容量限制的公网实例。NLB、常驻网关、执行 Pod、ACR 和模型调用产生资源消耗。目前没有跨主机高可用；DSW 停机/销毁、Supervisor 整体退出、工作区丢失时后端不可用。整机重启自动拉起依赖平台启动钩子，本次未配置。任务和 ACR 镜像保留，需规划容量和保留周期。

## 已有验证

2026-11-02 公网 HTTPS 上传、真实 DawnFM 调用、ACS 执行、结果与轨迹下载的 5 次并行测试：5/5 得分 1.0，清理全部确认。任务 harbor-b35c336952c4468c92925b1f00b88184。TLS 修复后两个公网 IP 信任链校验通过，HTTP 308 正常。额外 ZIP / 可选参考解 / 小数多指标及恢复检查记录在 final-verification.json。

最终验收：ZIP 不含参考解的真实任务 harbor-7ddf42811fae49c8a6496a65389e5f88 返回 reward=0.75、accuracy=1.0，轨迹和清理完整；24 项相关回归测试通过。两个结果包经公网下载并核对 SHA-256，未认证查询和已吊销模型 token 均返回 403。构建中 API 重启保留原 worker，隧道进程退出后自动恢复；两个公网 IP 最终 accepting_uploads=true。完整记录见 final-verification.json。

# EchoMind 部署脚本（Windows 优先）

本目录提供两个可重复执行的 PowerShell 脚本，用于在 `docker compose up -d` **前后**快速定位“跑不起来 / 起来了但聊天全挂”的原因。两个脚本都只用 PowerShell 5.1 兼容语法，Windows PowerShell 5.1 与 PowerShell 7（`pwsh`）均可运行。

| 脚本 | 作用 | 是否修改东西 |
| --- | --- | --- |
| `preflight.ps1` | 启动前环境自检：Docker 引擎、Compose V2、端口、`.env`、磁盘、监控配置 | 只读，不启动容器、不改文件 |
| `smoke.ps1` | 启动后接口冒烟：逐个打接口，输出状态码/耗时/摘要 | 只发 HTTP 请求（含 1 次 `POST /chat`，可用 `-SkipChat` 跳过） |

---

## 快速开始

在仓库的 `EchoMind/` 目录下执行：

```powershell
# 1) 启动前自检（当前环境有问题时会给出中文修复建议）
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\preflight.ps1

# 2) 按提示修好后启动
docker compose up -d
docker compose ps          # 等 echomind 变为 healthy（首次启动约 1-2 分钟）

# 3) 冒烟（没有真实 API Key 时先加 -SkipChat）
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke.ps1 -SkipChat

# 4) 配好真实 Key 后再验证聊天
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke.ps1
```

如果你的机器装了 PowerShell 7，也可以写成：

```powershell
pwsh -NoProfile -File .\scripts\preflight.ps1
pwsh -NoProfile -File .\scripts\smoke.ps1 -SkipChat
```

> 当前这台机器没有安装 `pwsh`（只有 Windows PowerShell 5.1），所以请用 `powershell -NoProfile -ExecutionPolicy Bypass -File ...` 的形式执行。

---

## preflight.ps1 — 启动前自检

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\preflight.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\preflight.ps1 -Help   # 帮助
Get-Help .\scripts\preflight.ps1 -Detailed                                          # 注释版帮助
```

检查项与判定：

| 检查项 | PASS | WARN | FAIL |
| --- | --- | --- | --- |
| 仓库目录 | 找到 `docker-compose.yml` | — | 找不到（不在仓库根目录） |
| docker CLI | `docker --version` 正常 | — | 未安装 docker |
| Docker 引擎 | `docker info` 返回 Server 信息 | — | permission denied / 引擎未启动（附修复步骤） |
| docker compose | `docker compose version` 正常（V2） | 只有 V1 `docker-compose` | 命令不可用 |
| 端口占用 | 80/8000/8001/6379/9090 全空闲 | 有端口被占用 | — |
| 磁盘空间 | 最小可用 ≥ 15GB | < 15GB | — |
| `.env` 文件 | 存在且可读 | — | 不存在（compose 会报错） |
| API Key | `ANTHROPIC_API_KEY` 已填真实值 | — | 仍是 `xxx` 等占位值（聊天必失败） |
| 模型配置 | 模型名与 Base URL 自洽 | DeepSeek 地址配了非官方模型 ID | — |
| 监控/告警 | 配置与实际一致 | `rule_files` 指向空目录 / Grafana 实际不存在 | — |
| 离线构建 | — | — | 仅 INFO 提示（无外网无法 build） |

**退出码**：`0` = 无 FAIL（允许 WARN）；`1` = 存在 FAIL；`3` = 脚本自身异常（已捕获，不会抛裸堆栈）。

## smoke.ps1 — 接口冒烟

```powershell
# 默认直连主应用 http://localhost:8000，跳过需要真实 Key 的 /chat
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke.ps1 -SkipChat

# 经 Nginx（80 端口）访问
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke.ps1 -BaseUrl http://localhost -SkipChat

# 配好真实 Key 后连 /chat 一起测（超时默认 120s）
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke.ps1

# 其它高级参数
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke.ps1 -TimeoutSec 15 -ChatTimeoutSec 180
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `-BaseUrl` | `http://localhost:8000` | 服务地址；经 Nginx 用 `http://localhost`（80 端口） |
| `-SkipChat` | 关 | 跳过 `POST /chat`（无真实 Key 时**建议加上**） |
| `-TimeoutSec` | `10` | 普通 GET 接口超时秒数 |
| `-ChatTimeoutSec` | `120` | `/chat` 超时秒数（内部串行调用多次大模型） |
| `-Help` | — | 显示帮助后退出 |

依次测试的接口与判定：

| 接口 | 是否依赖大模型 | 说明 |
| --- | --- | --- |
| `GET /health` | 否 | `status=ok` + Agent 统计字段；503 表示进程已起但未就绪 |
| `GET /skills` | 否 | 已加载 Skill 数量与解析错误数 |
| `GET /knowledge/stats` | 否 | `total_chunks` 知识库片段数 |
| `GET /monitor` | 否 | 活跃告警数、优化建议数 |
| `GET /metrics` | 否 | Prometheus 文本指标行数与 `echomind_*` 样本数 |
| `POST /chat` | **是** | 输出 intent / agent / latency / 回复摘要；Key 无效时 401 |

**退出码**：`0` = 全部通过（SKIP 不计失败）；`1` = 有接口失败；`2` = 服务整体不可达；`3` = 参数或脚本异常。

---

## Windows 前置条件（必须满足）

1. **Docker Desktop 的引擎必须处于 Running**
   - 光打开 Docker Desktop 界面不够：托盘鲸鱼图标要稳定为 “Engine running”，且 `docker info` 能返回 `Server:` 段。
   - 若 `docker info` 报 `permission denied while trying to connect to the docker API ...`，按顺序排查：
     1. 打开 Docker Desktop，等待引擎完全启动；
     2. 管理员执行 `wsl --update`，然后 `wsl --shutdown`，再重启 Docker Desktop；
     3. 管理员执行 `net localgroup docker-users %USERNAME% /add`，注销重登；
     4. 重启 Docker Desktop / 重启机器。
2. **WSL2 后端可用**
   - `wsl -l -v` 应能看到 `docker-desktop` 且状态为 `Running`；
   - Docker Desktop 设置 → General 勾选 “Use the WSL 2 based engine”；Resources → WSL Integration 打开。
3. **PowerShell 执行策略**
   - 推荐每次显式加 `-ExecutionPolicy Bypass`（不改系统策略）；
   - 或只对当前会话放行：`Set-ExecutionPolicy -Scope Process Bypass`。
4. **端口未被其它程序占用**：`80`（Nginx）、`8000`（主应用）、`8001`（ChromaDB）、`6379`（Redis）、`9090`（Prometheus）。
5. **磁盘至少 15GB 可用**：镜像 + ONNX 模型 + 构建缓存。

---

## 离线 / 无外网环境部署方案（重点）

本机完全无外网时，`docker compose build` **一定失败**：`EchoMind/Dockerfile` 第 22-41 行在构建期要下载约 79MB 的 ONNX embedding 模型（`https://chroma-onnx-models.s3.amazonaws.com/...`），另外还要拉基础镜像和 pip 依赖。正确做法是**在有网机器构建好镜像，再导出/导入**。

### 步骤

1. **在有网的同架构机器上**，切到同一分支、准备 `.env`（Key 可以在离线机器再改，容器运行时才读取）：
   ```bash
   git checkout feat/offline-eval-hardening
   docker compose build
   ```
2. **查出需要携带的镜像清单**（这一步不需要引擎也能跑）：
   ```bash
   docker compose config --images
   ```
   本仓库当前输出示例：
   ```
   chromadb/chroma:0.5.23
   prom/prometheus:latest
   echomind-echomind
   nginx:alpine
   redis:7-alpine
   ```
   > `echomind-echomind` 是 compose 构建产物（`<项目名>-<服务名>`，项目名默认取目录名）。**如果你本地目录名不是 `echomind`，请以实际输出为准，或统一加 `-p echomind` / 设置 `COMPOSE_PROJECT_NAME=echomind`。**
3. **导出为一个 tar（可一次带多个镜像）**：
   ```bash
   docker save -o echomind-offline.tar \
     redis:7-alpine \
     chromadb/chroma:0.5.23 \
     prom/prometheus:latest \
     nginx:alpine \
     echomind-echomind
   ```
4. 把 `echomind-offline.tar`（可选带 `.env.example`）拷到离线机器。
5. **离线机器导入**：
   ```powershell
   docker load -i echomind-offline.tar
   ```
6. **启动（不要再 build）**：
   ```powershell
   docker compose -p echomind up -d --no-build
   docker compose -p echomind ps
   ```
   `--no-build` 保证不会触发需要联网的构建；`-p echomind` 保证 compose 找的镜像名和服务名与导入的一致。
7. **验证**：
   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\preflight.ps1
   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke.ps1 -SkipChat
   ```

### 注意事项

- **架构必须一致**：在 ARM（如 Apple Silicon）上构建的镜像在 x86_64 上跑不起来。构建时可用 `--platform linux/amd64`，或用 `docker image inspect <镜像> --format '{{.Architecture}}'` 确认。
- **`prom/prometheus:latest` 未锁版本**：建议在有网机器记录实际 digest 再 `docker save`，避免“昨天能导出、今天导出成另一个版本”。
- **代码是构建时 COPY 进镜像的**：改了 Python 代码/配置，必须在有网机器重新 `docker compose build` 并重新导出，不能只改 compose。
- **ONNX 模型已经烘进镜像**：离线机器不需要再下载；`data/`（Redis/Chroma 持久化数据）按需另外拷贝，不属于镜像。
- 若内网有私有 registry，也可以 `docker push` / `docker pull`，效果等价于 `save`/`load`。

---

## 典型排障流程

```text
preflight FAIL（Docker 引擎/端口/Key）
        │
        ├─ 引擎 permission denied → 按 preflight 输出的 4 步修复（Docker Desktop / WSL2 / docker-users 组）
        ├─ 端口被占用             → docker compose down（本项目旧容器）或找到占用进程结束
        └─ API Key 仍是 xxx       → 编辑 .env（顶部有三分钟配置指南）
        │
        ▼
docker compose up -d  →  docker compose ps（等 healthy）
        │
        ▼
smoke.ps1 -SkipChat
        │
        ├─ 服务不可达（退出码 2）→ 容器没起/端口没映射/引擎没就绪，回到 preflight
        ├─ 503                     → 进程已起但未就绪，等 1-2 分钟或看日志
        └─ 全部通过                → 去掉 -SkipChat 再验证 /chat（需真实 Key）
```

常用日志命令：

```powershell
docker compose logs -f echomind
docker compose logs -f chromadb
docker compose ps
```

---

## 已知遗留问题（本次未修，属于其它文件边界）

1. **Nginx 超时**：`config/nginx/nginx.conf` 的 `proxy_read_timeout` / `proxy_send_timeout` 仍是 `60s`，而 `/eval/run` 会串行调用十几次大模型，经 Nginx 访问大概率 504。需要把这两个值调到 `600s`（本次只在 `docker-compose.yml` 里补了说明注释，未改 nginx.conf）。
2. **Grafana / 告警规则实际不存在**：`config/prometheus.yml` 声明了 `rule_files: alerts/*.yml`，但 `config/alerts/` 为空；`config/grafana/` 也是空目录，compose 里既没有挂载也没有 grafana 服务。演示前请补齐，或暂时删掉 `rule_files` 行。`preflight.ps1` 会对这一点给出 WARN。
3. **部署脚本仍是 Compose V1 命令**：`docker-deploy.sh`、`build-image.sh`、`run-image.sh` 里用的是 `docker-compose`（V1），本机只有 V2 的 `docker compose`，直接执行会 `command not found`。请手动使用 `docker compose ...`，或等维护者统一改造（本次按改动边界未修改这些脚本）。
4. **`.env` 的 Key 是占位值**：`ANTHROPIC_API_KEY=xxx` 不替换时，服务能起、`/health` 返回 200，但 `/chat`、`/search`、`/eval/run` 全部失败。
5. **模型名可能不存在**：`ANTHROPIC_MODEL=deepseek-v4-pro` 不是 DeepSeek 官方模型 ID（官方为 `deepseek-chat` / `deepseek-reasoner`）；走 Anthropic 官方路线时应改成官方模型名并清空 `ANTHROPIC_BASE_URL`。

---

## 兼容性说明

- 两个脚本**只使用 Windows PowerShell 5.1 支持的语法**：没有 `&&`、`??`、三元运算符、`-Parallel`、`??=` 等 PowerShell 7 专有写法。
- 已在本机 `Windows PowerShell 5.1.26100` 实测通过：`preflight` 正常跑完并输出彩色表格；`smoke` 在服务未启动时优雅输出“服务不可达”并返回退出码 2。
- 脚本通过 `$PSScriptRoot` 定位仓库根目录，因此在任意工作目录下执行都可以（只要脚本仍在 `EchoMind/scripts/` 内）。
- 所有异常都用 `try/catch` 捕获后输出中文提示，不会抛出裸堆栈。

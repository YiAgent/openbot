# Heroku 部署 runbook

OpenBot v0.1 在 Heroku 上的部署形状：

| 进程  | Dyno   | 价格    | 行为                                                |
|------|--------|--------|----------------------------------------------------|
| web   | Basic  | $7/mo  | 永不休眠，HMAC 验签 → Redis dedup → 入队 → 返回 202 |
| worker| Eco    | $5/mo  | `python -m openbot.entrypoints.worker`，消费 Stream      |

外部依赖（不走 Heroku addons）：

- **Postgres**：Neon（`sslmode=require`，asyncpg 驱动）
- **Redis**：Redis Cloud Addon（`redis://`）
- **Sandbox**：Daytona（`OPENBOT_SANDBOX_BACKEND=daytona`）

LLM provider key、LangSmith key 等通用密钥由 `scripts/doppler-bootstrap-shared.sh` 从 `infra/prd` 同步过来，已在 `openbot/prd` 里。

---

## 监控与日志

OpenBot 预置了以下监控工具：

| 工具 | 用途 | 查看方式 |
|------|------|----------|
| **Papertrail** | 实时日志流 & 搜索 | `heroku addons:open papertrail` |
| **Better Stack** | Uptime 监控 | `heroku addons:open betteruptime` |
| **Sentry** | 异常上报 | `heroku addons:open sentry` |

### Papertrail 使用建议

Papertrail 默认会接收所有 Heroku logs。你可以通过以下命令在 CLI 查看：
```bash
heroku logs --tail -a openbot
```
或者打开 Web UI 进行搜索和过滤：
```bash
heroku addons:open papertrail
```

---

## Secrets 管理

**唯一写入点**：Doppler `openbot/prd`。该 config 已绑定 Doppler→Heroku 自动同步，写入后秒级生效到 Heroku 的 config vars。

**直接 `heroku config:set` 会被覆盖**，不要用。

### 一次性 bootstrap

```bash
bash scripts/heroku-doppler-bootstrap.sh
```

幂等地完成：

1. 旧 key 重命名：
   - `POSTGRES_URL` → `OPENBOT_POSTGRES_URL`
   - `REDIS_URL` → `OPENBOT_REDIS_URL`
   - `OPENBOT_GITHUB_APP_PRIVATE_KEY_BASE64` → `OPENBOT_GITHUB_APP_PRIVATE_KEY_PEM`（新方案，配合 `openbot.core.settings.py` 的 `github_app_private_key_pem`）
2. 常量写入：`OPENBOT_SANDBOX_BACKEND=daytona` / `PYTHONUNBUFFERED=1` / `OPENBOT_WORKER_CONCURRENCY=4`
3. 扫描空值并打印仍缺什么

### 必填的密钥（部署前要填）

| Doppler key                            | 来源                                                            |
|----------------------------------------|----------------------------------------------------------------|
| `OPENBOT_GITHUB_WEBHOOK_SECRET`        | GitHub App settings → Webhook secret                          |
| `OPENBOT_POSTGRES_URL`                 | Neon dashboard → connection string, 改成 `postgresql+asyncpg://...?sslmode=require` |
| `OPENBOT_REDIS_URL`                    | Upstash dashboard → Redis URL（`rediss://...`）                |
| `DAYTONA_API_KEY`                      | Daytona dashboard → API keys                                   |

### 写入方式

```bash
doppler secrets set OPENBOT_POSTGRES_URL --project openbot --config prd
# 然后从 stdin 粘贴值，Ctrl-D 结束
```

PEM 私钥（单行写入会破坏换行，必须文件喂）：

```bash
cat ./secrets/github-app-private.pem | \
  doppler secrets set OPENBOT_GITHUB_APP_PRIVATE_KEY_PEM \
    --project openbot --config prd
```

### 可选

| Doppler key                          | 不填的代价                                          |
|--------------------------------------|----------------------------------------------------|
| `OPENBOT_GITHUB_APP_ID`              | 没有这个，写回（评论 / label）功能整体禁用          |
| `OPENBOT_GITHUB_APP_PRIVATE_KEY_PEM` | 同上                                              |
| `OPENBOT_SENTRY_DSN`                 | 没有就不上报错误到 Sentry（`sentry_sdk.init(dsn=None)` 文档保证 no-op）|
| `OPENBOT_ENVIRONMENT`                | 默认 `development`；prod 应设 `production` 给 Sentry event 打 tag |
| `OPENBOT_SENTRY_TRACES_SAMPLE_RATE`  | 默认 0.0（仅 error）；想看性能 trace 设 `0.05-0.2`，注意吃 Sentry 配额 |

`webapp._build_auth` 看到 `app_id` 或 `pem` 任一为空就返回 `None`，webhook 仍能接受 + 验签 + 入队，只是 worker 跑完 workflow 后没法发表评论。Receive-only 部署可以先这样上线。

---

## 首次部署

前置：bootstrap 跑完且必填密钥都已填。

```bash
# Heroku CLI 已登录、git remote 'heroku' 已指向 git.heroku.com/openbot.git
git push heroku main

# Procfile 首次被识别后，给 dyno 选规格 + 起 1 个进程
heroku ps:type web=basic worker=eco -a openbot
heroku ps:scale web=1 worker=1 -a openbot

# 看启动日志
heroku logs --tail -a openbot
```

`heroku ps -a openbot` 应该看到两个进程都 up。webhook 探活：

```bash
curl https://openbot-ac02d94253df.herokuapp.com/health
```

---

## 日常运维

| 操作                       | 命令                                                          |
|---------------------------|---------------------------------------------------------------|
| 重新部署                   | `git push heroku main`                                       |
| 查实时日志                 | `heroku logs --tail -a openbot`                              |
| 查最近 1500 行             | `heroku logs -n 1500 -a openbot`                             |
| 重启所有 dyno              | `heroku restart -a openbot`                                  |
| 只重启 worker              | `heroku ps:restart worker -a openbot`                        |
| 停 worker（紧急止血）       | `heroku ps:scale worker=0 -a openbot`                        |
| 一次性命令（debug shell）   | `heroku run bash -a openbot`                                 |
| 跑一次 Python REPL         | `heroku run python -a openbot`                               |
| 看当前 config              | `heroku config -a openbot`                                   |
| 回滚到上一版               | `heroku releases -a openbot` 找版本号 → `heroku rollback vNN -a openbot` |

### 改 dyno 规格

```bash
heroku ps:type web=standard-1x -a openbot   # 升级到 $25/mo（1GB RAM）
heroku ps:type web=basic       -a openbot   # 回到 $7/mo
```

### 改 worker 并发

```bash
doppler secrets set OPENBOT_WORKER_CONCURRENCY=8 --project openbot --config prd
heroku ps:restart worker -a openbot
```

Eco dyno 是 512MB，4 并发 asyncio 消费者一般够用。提高之前看一眼 `heroku ps:wait` 后的 `Memory quota vastly exceeded` 告警。

---

## 监控与告警

- **Heroku Metrics**（Basic dyno 自带）：CPU / memory / response time 在 dashboard 看
- **应用层日志**：JSON 行直接进 Heroku Logplex；推荐挂一个 Papertrail / Logtail addon 做归档
- **LangSmith**：trace 自动写入 `openbot-prd` project（`LANGSMITH_PROJECT` 已在 Doppler）

### 健康检查告警建议

GitHub App 的 webhook delivery dashboard 是最直接的健康信号 —— 它显示每次发的 2xx/5xx。如果连续 5xx，先看 `heroku logs --tail`。

---

## 已知限制

1. **无持久磁盘**：所有写到 `/app` 的运行时文件下次 dyno 重启会丢。Dedup state 全在 Redis，正确。GitHub App 私钥走 `OPENBOT_GITHUB_APP_PRIVATE_KEY_PEM` env var，不依赖文件。
2. **boot 超时 60 秒**：lifespan 里只做 Redis client + Postgres engine + `create_schema` 幂等调用，远小于这个限制。如果以后加了重的预热逻辑（比如 model warm-up），考虑挪到后台任务。
3. **Eco dyno hour 配额**：账户 1000 小时 / 月共享。单 worker 24/7 ≈ 720 小时，剩 280 小时给其他 Eco app 使用。

---

## 灾难回滚

```bash
heroku releases -a openbot                  # 列出历史
heroku rollback v123 -a openbot             # 回到 v123（slug + config 都回滚）
```

注意：Doppler 同步过来的 config vars 不在 release 里 —— config 回滚只回滚直接 `heroku config:set` 的部分。如果是密钥改坏了，要去 Doppler 改回旧值，等同步。

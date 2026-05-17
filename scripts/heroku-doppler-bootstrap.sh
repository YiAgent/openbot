#!/usr/bin/env bash
# Heroku 部署前置：把 openbot/prd Doppler config 调成 webapp + worker 真正能跑的形状。
#
# 为什么写到 Doppler 而不是直接 `heroku config:set`：
#   - 该 Heroku app 已经绑定了 Doppler→Heroku 自动同步（openbot/prd → openbot app）。
#   - 直接 set 会被下一次同步覆盖；统一以 Doppler 为 source of truth。
#
# 这个脚本做三件事：
#   1) 重命名旧 key 到 pydantic-settings 期望的 `OPENBOT_` 前缀形式
#      POSTGRES_URL                          → OPENBOT_POSTGRES_URL
#      REDIS_URL                             → OPENBOT_REDIS_URL
#      OPENBOT_GITHUB_APP_PRIVATE_KEY_BASE64 → OPENBOT_GITHUB_APP_PRIVATE_KEY_PEM (新方案)
#   2) 写入必需的常量（Heroku runtime 行为）：OPENBOT_SANDBOX_BACKEND / PYTHONUNBUFFERED / OPENBOT_WORKER_CONCURRENCY
#   3) 列出仍是空占位的 key，提醒人工填入
#
# 幂等：可重复跑。已经迁移过的 key（新名字有值、旧名字也已删除）会被跳过。

set -euo pipefail

PROJECT="openbot"
CONFIG="prd"

# old_key:new_key —— 仅迁移 old_key 当前非空且 new_key 当前空的情形
RENAMES=(
  "POSTGRES_URL:OPENBOT_POSTGRES_URL"
  "REDIS_URL:OPENBOT_REDIS_URL"
  "OPENBOT_GITHUB_APP_PRIVATE_KEY_BASE64:OPENBOT_GITHUB_APP_PRIVATE_KEY_PEM"
)

# Heroku-runtime 常量（值固定；不存在则写入，已存在则跳过）
CONSTS=(
  "OPENBOT_SANDBOX_BACKEND=daytona"
  "PYTHONUNBUFFERED=1"
  "OPENBOT_WORKER_CONCURRENCY=4"
)

# 部署前必填的 key（用于最后一步扫描空值并提醒）
REQUIRED_NONEMPTY=(
  "OPENBOT_GITHUB_WEBHOOK_SECRET"
  "OPENBOT_POSTGRES_URL"
  "OPENBOT_REDIS_URL"
  "DAYTONA_API_KEY"
)

OPTIONAL_NONEMPTY=(
  "OPENBOT_GITHUB_APP_ID"
  "OPENBOT_GITHUB_APP_PRIVATE_KEY_PEM"
)

_dop() {
  doppler "$@" --project "$PROJECT" --config "$CONFIG"
}

# 读：返回 plain 值（key 不存在或为空都返回空串，永不报错）
_get() {
  local key=$1
  _dop secrets get "$key" --plain 2>/dev/null || true
}

# 写：值经 stdin 传入避免出现在 process listing
_set() {
  local key=$1 value=$2
  printf '%s' "$value" | _dop secrets set "$key" 2>&1 | tail -1
}

# 删
_del() {
  local key=$1
  _dop secrets delete "$key" --yes >/dev/null 2>&1 || true
}

echo "→ Target: doppler ${PROJECT}/${CONFIG} (auto-syncs to Heroku app openbot)"
echo ""

echo "=== 1) Renames ==="
for pair in "${RENAMES[@]}"; do
  old=${pair%%:*}
  new=${pair##*:}
  old_val=$(_get "$old")
  new_val=$(_get "$new")
  if [ -n "$old_val" ] && [ -z "$new_val" ]; then
    echo "  ${old} → ${new}  (migrating)"
    _set "$new" "$old_val" >/dev/null
    _del "$old"
  elif [ -n "$old_val" ] && [ -n "$new_val" ]; then
    echo "  ${old} → ${new}  (both populated; keeping new, deleting old)"
    _del "$old"
  elif [ -z "$old_val" ] && [ -n "$new_val" ]; then
    echo "  ${old} → ${new}  (already migrated)"
  else
    echo "  ${old} → ${new}  (neither set; deleting stale old)"
    _del "$old"
  fi
done

echo ""
echo "=== 2) Constants ==="
for kv in "${CONSTS[@]}"; do
  key=${kv%%=*}
  val=${kv#*=}
  cur=$(_get "$key")
  if [ "$cur" = "$val" ]; then
    echo "  ${key}=${val}  (already)"
  elif [ -z "$cur" ]; then
    echo "  ${key}=${val}  (setting)"
    _set "$key" "$val" >/dev/null
  else
    echo "  ${key}=${val}  (override existing '${cur}')"
    _set "$key" "$val" >/dev/null
  fi
done

echo ""
echo "=== 3) Missing required values ==="
missing_required=()
for key in "${REQUIRED_NONEMPTY[@]}"; do
  val=$(_get "$key")
  if [ -z "$val" ]; then
    missing_required+=("$key")
  fi
done

missing_optional=()
for key in "${OPTIONAL_NONEMPTY[@]}"; do
  val=$(_get "$key")
  if [ -z "$val" ]; then
    missing_optional+=("$key")
  fi
done

if [ ${#missing_required[@]} -gt 0 ]; then
  echo "  ❌ Required (deploy will not work):"
  for k in "${missing_required[@]}"; do echo "     - $k"; done
fi

if [ ${#missing_optional[@]} -gt 0 ]; then
  echo "  ⚠️  Optional (write-back / GitHub App features off until set):"
  for k in "${missing_optional[@]}"; do echo "     - $k"; done
fi

if [ ${#missing_required[@]} -eq 0 ] && [ ${#missing_optional[@]} -eq 0 ]; then
  echo "  ✓ All required + optional keys populated."
fi

echo ""
echo "Done. Verify Heroku side after Doppler auto-sync:"
echo "  heroku config -a openbot | grep -E 'OPENBOT_|DAYTONA|PYTHONUNBUFFERED'"

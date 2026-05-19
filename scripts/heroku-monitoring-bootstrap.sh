#!/usr/bin/env bash

set -euo pipefail

APP_NAME="${1:-openbot}"
READY_PATH="${OPENBOT_BETTERUPTIME_PATH:-/ready}"
CHECK_FREQUENCY="${OPENBOT_BETTERUPTIME_CHECK_FREQUENCY:-30}"
MONITOR_NAME="${OPENBOT_BETTERUPTIME_MONITOR_NAME:-${APP_NAME} readiness}"

require_cmd() {
  local cmd=$1
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "error: required command not found: $cmd" >&2
    exit 1
  fi
}

heroku_config_get() {
  local key=$1
  heroku config:get "$key" -a "$APP_NAME" 2>/dev/null || true
}

create_addon_if_missing() {
  local addon_name=$1
  shift
  if heroku addons:info "$addon_name" -a "$APP_NAME" >/dev/null 2>&1; then
    echo "  ${addon_name}: already attached"
    return 0
  fi

  echo "  ${addon_name}: attaching"
  heroku addons:create "$@" -a "$APP_NAME"
}

require_cmd heroku
require_cmd curl
require_cmd python3

echo "==> Bootstrapping Heroku monitoring for app: ${APP_NAME}"

app_web_url="$(
  heroku apps:info -a "$APP_NAME" --json | python3 -c '
import json, sys
data = json.load(sys.stdin)
app = data.get("app") or {}
print((app.get("web_url") or "").rstrip("/"), end="")
'
)"

if [ -z "$app_web_url" ] && [ -z "${OPENBOT_BETTERUPTIME_URL:-}" ]; then
  echo "error: failed to resolve Heroku web URL for ${APP_NAME}" >&2
  echo "hint: set OPENBOT_BETTERUPTIME_URL explicitly and re-run" >&2
  exit 1
fi

redis_url="$(heroku_config_get REDISCLOUD_URL)"
if [ -z "$redis_url" ]; then
  redis_url="$(heroku_config_get OPENBOT_REDIS_URL)"
fi

if [ -z "$redis_url" ]; then
  echo "error: neither REDISCLOUD_URL nor OPENBOT_REDIS_URL is set on ${APP_NAME}" >&2
  exit 1
fi

monitor_url="${OPENBOT_BETTERUPTIME_URL:-${app_web_url}${READY_PATH}}"

echo ""
echo "==> Add-ons"
create_addon_if_missing redismonitor redismonitor:free --url "$redis_url"
create_addon_if_missing betteruptime betteruptime:test

echo ""
echo "==> Better Uptime monitor"
betteruptime_token=""
for _ in 1 2 3 4 5 6 7 8 9 10; do
  betteruptime_token="$(heroku_config_get BETTERUPTIME_API_TOKEN)"
  if [ -n "$betteruptime_token" ]; then
    break
  fi
  sleep 2
done

if [ -z "$betteruptime_token" ]; then
  echo "error: BETTERUPTIME_API_TOKEN is still empty after attaching the add-on" >&2
  echo "hint: wait a few seconds, then re-run this script" >&2
  exit 1
fi

existing_monitor_id="$(
  curl -fsS --request GET \
    --get \
    --url "https://uptime.betterstack.com/api/v2/monitors" \
    --header "Authorization: Bearer ${betteruptime_token}" \
    --data-urlencode "url=${monitor_url}" \
    | python3 -c '
import json, sys
data = json.load(sys.stdin)
items = data.get("data") or []
print(items[0]["id"] if items else "", end="")
'
)"

if [ -n "$existing_monitor_id" ]; then
  echo "  Better Uptime monitor already exists for ${monitor_url} (id=${existing_monitor_id})"
else
  create_payload="$(
    MONITOR_URL="$monitor_url" \
    MONITOR_NAME="$MONITOR_NAME" \
    CHECK_FREQUENCY="$CHECK_FREQUENCY" \
    python3 -c '
import json
import os

payload = {
    "monitor_type": "status",
    "url": os.environ["MONITOR_URL"],
    "pronounceable_name": os.environ["MONITOR_NAME"],
    "email": True,
    "check_frequency": int(os.environ["CHECK_FREQUENCY"]),
    "follow_redirects": True,
}
print(json.dumps(payload), end="")
'
  )"

  created_monitor_id="$(
    curl -fsS --request POST \
      --url "https://uptime.betterstack.com/api/v2/monitors" \
      --header "Authorization: Bearer ${betteruptime_token}" \
      --header "Content-Type: application/json" \
      --data "$create_payload" \
      | python3 -c '
import json, sys
data = json.load(sys.stdin)
item = data.get("data") or {}
print(item.get("id", ""), end="")
'
  )"
  echo "  Created Better Uptime monitor for ${monitor_url} (id=${created_monitor_id})"
fi

echo ""
echo "Done."
echo "  Memetria dashboard:     heroku addons:open redismonitor -a ${APP_NAME}"
echo "  Better Uptime dashboard: heroku addons:open betteruptime -a ${APP_NAME}"
echo "  Health target:          ${monitor_url}"
echo ""
echo "If you want Redis memory analysis in Memetria, upgrade to:"
echo "  heroku addons:upgrade redismonitor:monitor0 -a ${APP_NAME}"

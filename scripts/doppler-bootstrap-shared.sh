#!/usr/bin/env bash
# 把账户级共享 LLM token 从 `infra/prd` 复制到 `openbot/{dev,stg,prd}`。
#
# 何时跑：
#   1) 初次 setup（创建 openbot project 后）
#   2) infra 里某个 token rotate 之后（手动同步漂移）
#
# 设计权衡：
#   - 选了"复制" pattern 而非 runtime 双 fetch —— 单 token、operational 简单。
#   - 代价是 rotate 后需要重跑此脚本；接受这个 trade-off，因为 token rotation 频率低。
#
# 安全：
#   - 不在 shell history / process listing 里出现明文 secret（值经 stdin pipe 上传）。
#   - 仅 maintainer 本地跑；CI 不需要这个脚本。
#
# 依赖：doppler CLI v3+，对 infra & openbot 都有读/写权限。

set -euo pipefail

SOURCE_PROJECT="infra"
SOURCE_CONFIG="prd"
TARGET_PROJECT="openbot"
TARGET_CONFIGS=(dev stg prd)

# 仅复制 openbot 实际用得到的 keys —— 显式 allowlist，防止盲拷贝整个 infra
SHARED_KEYS=(
  ANTHROPIC_API_KEY
  OPENAI_API_KEY
  LANGSMITH_API_KEY
  LANGSMITH_ENDPOINT
)

_TARGET_CSV=$(printf '%s,' "${TARGET_CONFIGS[@]}" | sed 's/,$//')

echo "→ Source: ${SOURCE_PROJECT}/${SOURCE_CONFIG}"
echo "→ Target: ${TARGET_PROJECT}/{${_TARGET_CSV}}"
echo "→ Keys:   ${SHARED_KEYS[*]}"
echo ""

# 从 source 一次性拉所有需要的 keys，构造 JSON
SECRETS_JSON=$(python3 - <<PY
import json, subprocess, sys
keys = """${SHARED_KEYS[@]}""".split()
out = {}
for k in keys:
    r = subprocess.run(
        ["doppler", "secrets", "get", k,
         "--project", "${SOURCE_PROJECT}",
         "--config", "${SOURCE_CONFIG}",
         "--plain"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(f"WARN: cannot read {k} from ${SOURCE_PROJECT}/${SOURCE_CONFIG}: {r.stderr.strip()}", file=sys.stderr)
        continue
    out[k] = r.stdout.rstrip("\n")
print(json.dumps(out))
PY
)

if [ -z "$SECRETS_JSON" ] || [ "$SECRETS_JSON" = "{}" ]; then
  echo "FATAL: no shared keys fetched from ${SOURCE_PROJECT}/${SOURCE_CONFIG}" >&2
  exit 1
fi

# 上传到每个 target config（stdin 而非命令行，避免 secret 入 process table）
for CFG in "${TARGET_CONFIGS[@]}"; do
  printf '%s' "$SECRETS_JSON" \
    | doppler secrets upload \
        --project "$TARGET_PROJECT" \
        --config "$CFG" \
        /dev/stdin >/dev/null
  echo "✓ synced shared keys → ${TARGET_PROJECT}/${CFG}"
done

echo ""
echo "Done. Verify: doppler secrets --project ${TARGET_PROJECT} --config dev --only-names"

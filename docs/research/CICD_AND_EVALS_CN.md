# Open SWE 的 CI/CD 与 Evals 评测体系

> 这是 `docs/PROJECT_GUIDE_CN.md` 的姐妹篇。前一篇讲"项目长什么样"，这一篇讲"项目怎么验证自己是对的"——分两条主线：
>
> 1. **CI/CD（GitHub Actions）**：每次 push / PR 自动跑什么、自动发布到哪里。
> 2. **Evals（LangSmith 离线评测）**：用 50 个真实开源 PR 的人类 review 当金标，让 LLM 当裁判，给 Reviewer Agent 打分。
>
> 看完之后你能：① 解释 `.github/workflows/` 里每个 yml 的目的；② 自己改 prompt 之后跑一次 eval 做回归验证；③ 对比自己实现的代码审查 agent 与 Devin Review。

---

## 一、CI/CD 总览

```
.github/
├── dependabot.yml                  # 每月升级 uv / docker / actions 依赖
└── workflows/
    ├── ci.yml                      # 主 CI：lint + format + unit test
    ├── pr_lint.yml                 # PR 标题语义化校验（feat/fix/...）
    └── promote_main_to_prod.yml    # 每天 8:00 UTC 把 main 强推到 prod
```

只有 4 个文件，但它们覆盖了"质量门禁 + 命名规范 + 自动发布 + 依赖维护"四件事。**注意：项目没有传统意义的 deploy pipeline**——LangGraph Cloud 自己监听 `prod` 分支，CI 只负责"把 main 升到 prod"，剩下的部署是平台行为。

---

## 二、`ci.yml` —— 主质量门禁

```yaml
name: Agent CI
on:
  push:    { branches: ["main"] }
  pull_request:
  workflow_dispatch:
```

**触发条件**有三个：
1. 推到 `main` —— 守住主干。
2. 任何 PR —— 在合并前就拦下问题。
3. 手动 `workflow_dispatch` —— 方便复跑。

```yaml
concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true
```

**concurrency 是关键省钱招**：同一分支再次 push 时，把上一轮还没跑完的 job 直接 cancel。这意味着你连续推 5 次提交，CI 只跑最后一次的完整流程，前面 4 次都会被打断——CI 信用、Actions 分钟数双重节省。

### 三个并行 job

| Job | 跑什么 | Make 目标 |
|---|---|---|
| `lint` | `ruff check` + `ruff format --diff` | `make lint` |
| `format` | `ruff format --check`（只检查不改） | `make format-check` |
| `unit-tests` | `pytest -vvv tests/` | `make test` |

三者**并行**跑（无 `needs:` 声明），节省墙钟时间。每个 job 重复同一段 4 步前奏：

```yaml
- uses: actions/checkout@v6
- uses: astral-sh/setup-uv@08807647e7069bb48b6ef5acd8ec9567f424441b   # ★
- run: uv sync --locked --extra dev
- run: make {lint|format-check|test}
```

#### 为什么用 `uv` 而不是 `pip`？

- 速度：`uv sync` 解析 + 安装比 `pip install -r requirements.txt` 快 10-100×。
- 锁定：`uv.lock` 保证 CI 跟本地完全一样的依赖图。**`--locked` 标志要求 lockfile 必须存在且匹配**，否则 CI 直接失败——这能拦下"忘提交 lockfile"或"手动改了 pyproject 但没重锁"。
- `--extra dev`：装上 `[project.optional-dependencies].dev`（pytest、ruff、pytest-asyncio、Pygments）。

#### 那一长串 SHA 是干啥的？

```yaml
uses: astral-sh/setup-uv@08807647e7069bb48b6ef5acd8ec9567f424441b   # v8.1.0
```

**用 commit SHA 锁第三方 action 版本，不要 tag**。tag 可以被作者重指（极少数情况下也可能被恶意覆盖），SHA 不可变。SHA 后面用注释标明"这是 v8.1.0"既保留可读性又锁死供应链。Dependabot 会按月帮你把 SHA + tag 一起升上去（见后文）。

### 这套 CI 的缺口

诚实地说，这套 CI **没做**几件事：
1. ❌ 没有覆盖率门禁——`pytest --cov` 没跑。
2. ❌ 没有 integration test（`make integration_tests` 在 `tests/integration_tests/` 不存在时 no-op）。
3. ❌ 没有安全扫描（bandit、安全 SCA）。
4. ❌ 没有 type check（mypy / pyright 都没接）。
5. ❌ 没有 docker 构建——LangGraph Cloud 帮你构建运行时镜像。

要不要补这些 = 取决于你对 Open SWE 的二次开发目标。这套 CI 的哲学是 **"只挡严重错误，靠手段而不是流程"**：lint/format 防风格漂移，pytest 防单元行为回归，剩下的真值检验交给 evals。

---

## 三、`pr_lint.yml` —— PR 标题语义化

```yaml
on:
  pull_request:
    types: [opened, edited, synchronize]
uses: amannn/action-semantic-pull-request@48f256284bd46cdaab1048c3721360e808335d50
```

强制 PR 标题符合 `type(scope): description` 格式，类型必须在白名单：

```
feat | fix | docs | style | refactor | perf | test
build | ci | chore | revert | release
```

scope（可选）：`shared | cli | web | open-swe | docs | deps`

**为什么这个 check 重要**：
- 后续可以用 conventional-commits 自动生成 changelog；
- 让 PR 列表本身就是项目史；
- 单元测试改、生产逻辑改、依赖升一眼看出。

如果某条 PR 真的不适合（比如 release branch），加 label `ignore-lint-pr-title` 即可绕过。

---

## 四、`promote_main_to_prod.yml` —— 自动发布

```yaml
on:
  schedule:
    - cron: "0 8 * * *"     # 每天 UTC 08:00（北京 16:00）
  workflow_dispatch:

jobs:
  promote:
    steps:
      - uses: actions/checkout@v6
        with: { ref: main, fetch-depth: 0 }
      - run: git push --force origin HEAD:refs/heads/prod
```

干的事情极简：**把 `main` 强推到 `prod`**。

这套发布模式背后的假设：
- LangGraph Cloud（或你的 deploy 平台）**监听 `prod` 分支**，一旦更新就重建并部署。
- `main` 是"准随时可发"的稳定分支——但发不发要看时机。
- 每天 08:00 UTC 把 24h 内合并的所有 main 提交一次性推到 prod，等同于**自动 daily release**。

`concurrency: cancel-in-progress: false` 是有意为之：一次 promote 不能被另一次中断，否则可能 push 到一半被打断、留下半状态。

**手动场景**：
- 紧急修复：合到 main 后跑 `workflow_dispatch: Run workflow` 立刻推 prod；
- 暂停 daily：直接 disable 这个 workflow 即可。

**为什么要 force-push 而不是 fast-forward？** —— 偶尔 prod 会有手工 hotfix 漂移（极少见），force-push 永远把 prod 拽回 main 的状态，避免分叉。

---

## 五、`dependabot.yml` —— 自动升级依赖

```yaml
updates:
  - package-ecosystem: "uv"            # pyproject.toml + uv.lock
  - package-ecosystem: "docker"        # Dockerfile（如果有）
  - package-ecosystem: "github-actions"
  schedule: { interval: "monthly" }
  groups:
    minor-and-patch: { update-types: [minor, patch] }
    major:           { update-types: [major] }
```

三个生态 + 按月升级 + **major 单独分组**——这一招很关键：
- minor/patch 攒成一个大 PR，全部一起合，CI 通过即视为安全；
- major 单独发 PR，**因为 major 通常意味着 breaking change**，需要人工评估是否要改业务代码再合。

CI 工作流自身也走 dependabot（`github-actions` 生态），这就是为什么 `setup-uv` 那行注释里写着 `# v8.1.0` —— dependabot 会同时改 SHA + 注释。

---

## 六、CI 全景速记

```
              ┌─────────────────────────────────────────────┐
              │              触发                            │
              │  push to main / open PR / manual / cron     │
              └────────────┬──────────────┬──────────┬──────┘
                           │              │          │
        ci.yml ────────────┘              │          │
          ├─ lint        (ruff check + format --diff)
          ├─ format      (ruff format --check)
          └─ unit-tests  (pytest -vvv tests/)
                                          │
        pr_lint.yml ──────────────────────┘
          └─ PR 标题必须 feat/fix/... 开头
                                                     │
        promote_main_to_prod.yml ─(cron 每天 8:00 UTC)┘
          └─ git push --force main → prod
        (LangGraph Cloud 监听 prod，自动重建部署)

        dependabot.yml ─(每月)────────────────────────
          ├─ uv 依赖升级
          ├─ docker 镜像升级
          └─ GitHub Actions 升级（含 SHA 注释同步）
```

---

# 第二部分：Evals —— 怎么测试 Agent 效果

> Agent 评测的根本难题：**没有客观正确答案**。同一个 PR 你 review 出 3 个问题，我 review 出 4 个，未必谁错。Open SWE 借鉴 Martian 的 `withmartian/code-review-benchmark` 思路：**把人类专家给过的 review 评论当作"金标"，让裁判 LLM 判每条 agent 评论是不是"指向同一个 bug"**。

整套体系全部 LangSmith 托管，只针对 **Reviewer Agent**（不针对主写代码 agent，因为主 agent 的"对错"得靠 PR 是不是真的 merge 来验证，那是另一条 pipeline）。

## 七、Evals 目录速览

```
evals/reviewer/
├── README.md
├── golden_comments/        # 5 个仓库 × 每仓 10 个 PR = 50 个 PR 的人类 review
│   ├── cal_dot_com.json    # 10 PRs, 31 comments
│   ├── discourse.json      # 10 PRs, 28 comments
│   ├── grafana.json        # 10 PRs, 22 comments
│   ├── keycloak.json       # 10 PRs, 24 comments
│   └── sentry.json         # 10 PRs, 31 comments
├── build_dataset.py        # 把 golden_comments → LangSmith Dataset
├── target.py               # 跑一次 reviewer agent，收集所有 add_finding
├── judge.py                # 用 claude-opus-4-5 配对判断 candidate vs golden
└── run_eval.py             # 把 target + judge 串起来调用 langsmith.aevaluate
```

每个 PR 的 golden 长这样：

```json
{
  "pr_title": "Async import of the appStore packages",
  "url": "https://github.com/calcom/cal.com/pull/8087",
  "comments": [
    { "comment": "Consider adding try-catch around the await ...", "severity": "Low" },
    { "comment": "Replace forEach with for...of ...",              "severity": "Critical" }
  ]
}
```

50 个 PR × 平均 ~3 条评论 = **~136 条金标评论**，构成一份小而精的基准集。

## 八、评测的四步流水线

```
   golden_comments/*.json
            │
            │ 1. build_dataset.py
            ▼
   LangSmith Dataset (openswe-reviewer-v1)
            │
            │ 2. run_eval.py 触发 langsmith.aevaluate(...)
            ▼
       并发 N 个例子，每个例子：
       │
       ├── target.py: review_pr(inputs)
       │   └── 调 langgraph_sdk 触发 reviewer 图
       │       └── reviewer agent 跑完一轮，返回 add_finding tool calls
       │
       └── judge.py: judge_match(run, example)
           └── 对每条 candidate × 每条 golden 调 claude-opus-4-5 判 match
           └── 算 precision / recall / f1（per-example）
            │
            │ 3. judge.py: aggregate_pr 汇总
            ▼
     LangSmith Experiment 里出现：
       micro_precision/recall/f1   (合并所有 TP/FP/FN 统一算)
       macro_precision/recall/f1   (每个 example 各算再平均)
```

下面挨个剖析每一步在干什么。

---

## 九、Step 1 —— `build_dataset.py`：金标 → LangSmith

**输入**：5 个 JSON 文件。
**输出**：1 个 LangSmith Dataset（`openswe-reviewer-v1`），每条 example 一份完整 PR 信息 + 人类评论金标。

核心两件事：

### 9.1 PR URL → 实际 SHA

文件里只有 `https://github.com/.../pull/8087`，但 reviewer agent 需要 `base_sha` / `head_sha` 才能确定性地拉 diff。所以 `build_example` 调 `gh`：

```python
gh pr view 8087 --repo calcom/cal.com --json baseRefOid,headRefOid,...
```

把 SHA 解析出来，**这意味着 Dataset 一旦上传就被"冻结"在那个 SHA**——以后 upstream PR rebase / force-push 也影响不了你的评测结果。这是 reproducibility 的根基。

### 9.2 example 格式

```python
inputs = {
  "repo": "calcom/cal.com",
  "pr_number": 8087,
  "pr_url": "...",
  "pr_title": "...",
  "base_sha": "abc123",         # gh 解出来的真实 SHA
  "head_sha": "def456",
  "base_ref": "main",
  "head_ref": "feat/async-import",
}
outputs = { "golden_comments": [...] }
metadata = { "source_file": "cal_dot_com", "pr_state": "MERGED", ... }
```

**inputs / outputs / metadata 三段式**是 LangSmith Dataset 的标准格式：
- `inputs` 喂给 target 函数；
- `outputs` 拿来当 ground truth；
- `metadata` 不参与评分，只是分组、过滤、调试用。

### 9.3 命令

```bash
# Dry-run：本地生成 evals/reviewer/dataset_dryrun.json 看一眼，不上传
uv run python -m evals.reviewer.build_dataset --dry-run

# 限制前 N 个，调试用
uv run python -m evals.reviewer.build_dataset --dry-run --limit 3

# 真上传（前置：LANGSMITH_API_KEY + gh auth status）
uv run python -m evals.reviewer.build_dataset --dataset-name openswe-reviewer-v1
```

**重要保护**：脚本检查同名 dataset 是否存在，**已存在就拒绝上传**（防止把历史评测的 baseline 覆盖掉）。要重做就改名，例如 `--dataset-name openswe-reviewer-v2`。

---

## 十、Step 2 —— `target.py`：把 reviewer agent 跑起来

这个文件做的事情就一句话：**把一个 example 的 inputs 翻译成 LangGraph run，等它跑完，拿回 agent 发的所有 `add_finding` tool call**。

### 10.1 触发图

```python
client = get_client(url=LANGGRAPH_URL)          # 默认 http://localhost:2024
thread = await client.threads.create()
result = await client.runs.wait(                # 同步等运行结束
    thread["thread_id"],
    assistant_id=REVIEWER_ASSISTANT_ID,         # 默认 "reviewer"，对应 langgraph.json 的图
    input={"messages": [{"role": "user", "content": _build_user_message(inputs)}]},
    config={"configurable": _build_configurable(inputs)},
)
```

`configurable` 里塞的 `__is_for_execution__: True` 是关键——`agent/reviewer.py:get_reviewer_agent` 检查这个 flag 才会启沙箱，否则只返回个"introspection 占位"。

### 10.2 用户消息

```text
Review pull request https://github.com/calcom/cal.com/pull/8087.

- repo: calcom/cal.com
- pr_number: 8087
- title: ...
- base_sha: abc123
- head_sha: def456

Record each issue you find with the `add_finding` tool, then call
`publish_review` once at the end.
```

注意 **eval 故意不让 agent 真的 publish**——`reviewer.py` 的 `publish_review` 会调 GitHub API 提交评论，eval 跑 50 次会污染真实仓库。这里靠的是 **`publish_review` 在 reviewer thread 上读 metadata 决定 PR 号**，但 eval 走的是临时 thread，PR 号是从 user message 来的——细看 `publish_review.py` 里会发现没配 `pr_number` / `head_sha` 时它会优雅地跳过 GitHub 调用，只把 finding 留在 metadata 里。

### 10.3 抓取 findings

```python
def _extract_comments(result):
    for msg in result.get("messages") or []:
        for tc in msg.get("tool_calls") or []:
            if tc.get("name") != "add_finding":
                continue
            # 归一化成 martian 评分用的旧格式：{file, line, body, severity}
            yield {
                "file":     args["file"],
                "line":     args.get("end_line") or args.get("start_line"),
                "body":     args["description"],
                "severity": args["severity"],
            }
```

**为什么要做格式归一化？** —— Martian 的判官 prompt 是 verbatim 拷过来的（见下一节），它认 `{file, line, body, severity}`。而 Open SWE 的 `add_finding` 用 `{file, start_line, end_line, description, severity}`。归一层只在 eval 里加，**不污染线上 agent 代码**。

### 10.4 thread 清理

`_THREAD_IDS` 是个进程级 set + lock，每次 `review_pr` 创建 thread 就记一笔。run_eval 跑完后调 `drain_thread_ids()` 拿出全部 thread id，批量 delete——避免 eval 跑完留下几百个垃圾 thread。

---

## 十一、Step 3 —— `judge.py`：LLM-as-judge 配对评分

这是评测最微妙的一环：**怎么判断"两条不同写法的评论指向同一个 bug"？** 答案是**让另一个强 LLM 当裁判**。

### 11.1 裁判模型和 prompt

```python
JUDGE_MODEL = "claude-opus-4-5"     # 跟 Martian 一致，结果可直接对比 Devin
```

判官 prompt（保持和 Martian 一字不改，便于横评）：

```
You are evaluating AI code review tools.
Determine if the candidate issue matches the golden (expected) comment.

Golden Comment: {格式化的 golden}
Candidate Issue: {格式化的 agent finding}

Instructions:
- Determine if the candidate identifies the SAME underlying issue as the golden
- Accept semantic matches - different wording is fine if it's the same problem
- Focus on whether they point to the same bug, concern, or code issue

Respond with ONLY a JSON object:
{"reasoning": "...", "match": true/false, "confidence": 0.0-1.0}
```

`temperature=0.0` 让判官尽量稳定可复现。`max_tokens=512` 限制开销。

### 11.2 配对算法（贪心一对一）

```python
matched_goldens = set()
matched_candidates = set()
for ci, cand in enumerate(candidates):
    for gi, gold in enumerate(goldens):
        if gi in matched_goldens:
            continue                    # 一个 golden 不能被两条 candidate 同时匹中
        if _judge_pair(gold, cand)["match"]:
            matched_goldens.add(gi)
            matched_candidates.add(ci)
            break                        # 一条 candidate 配上一个 golden 就停
```

**这是 O(n×m) 的全量 LLM 调用**：n 条 agent finding × m 条 golden = 一个 example 最多 n×m 次裁判调用。这就是 README 里写的"50 PRs × ~3 goldens × ~10 candidates ≈ 1500 judge calls"成本估算的来源。

### 11.3 三个指标

```
TP = 匹配上的 golden 数量          (agent 找到的真问题)
FP = 没配上的 candidate 数量        (agent 报的假阳性)
FN = 没被匹中的 golden 数量         (agent 漏掉的问题)

precision = TP / (TP+FP)            # 你说有问题的，里面真的有几个？
recall    = TP / (TP+FN)            # 真问题里你抓到了几个？
f1        = 2pr / (p+r)
```

`judge_match` 是 **per-example evaluator**——每个 PR 单独算出三个指标。

### 11.4 汇总：micro vs macro

```python
def aggregate_pr(runs, examples):
    micro_tp = sum(c["tp"] for c in all_examples)   # 全部 example 的 TP/FP/FN 加总
    micro_p  = micro_tp / (micro_tp + micro_fp)     # 再算 precision，等同"全数据集级别"

    macro_p  = mean(c["precision"] for c in all)    # 每个 example 算好再平均
```

二者区别：
- **micro** 把所有评论当作一个池子，**实际数量多的 PR 权重大**。
- **macro** 每个 PR 一票，**小 PR 跟大 PR 等权**。

实践中：
- 如果你只关心"agent 整体能找到多少问题"，看 micro。
- 如果你想避免"一个超大 PR 抓得好就掩盖了一堆小 PR 漏抓"，看 macro。

这俩在 LangSmith experiment 页都显示出来，**对比基线时记得同时看两个**。

### 11.5 进程间通信的小细节

`judge.py` 用全局 dict `_PER_EXAMPLE_COUNTS` + `threading.Lock` 在 `judge_match` 和 `aggregate_pr` 之间传数据。注释明确写了：

> "Falls back to an empty result set if the cache is empty (e.g. summary evaluator ran in a different process)."

也就是说**这套设计依赖单进程内运行**。如果哪天你用多进程方式跑 LangSmith 评测，aggregate_pr 会拿不到数据。**已知限制，不是 bug**。

---

## 十二、Step 4 —— `run_eval.py`：把上面三步拼起来

```python
async def main():
    await aevaluate(
        review_pr,                              # 来自 target.py
        data=args.dataset_name,                 # "openswe-reviewer-v1"
        evaluators=[judge_match],               # per-example
        summary_evaluators=[aggregate_pr],      # 跑完全部后汇总
        experiment_prefix=args.experiment_prefix,
        max_concurrency=args.max_concurrency,   # 默认 5 并发
        num_repetitions=1,
    )
    drain_thread_ids() → _cleanup_threads()     # 清理 LangGraph thread
```

### 12.1 常用调用方式

```bash
# 烟测：先跑 3 个 PR，5 分钟看看是否能跑通
uv run python -m evals.reviewer.run_eval --limit 3

# 完整评测（默认 5 并发，~50 examples × ~5min/example ÷ 5 = 1 小时左右）
uv run python -m evals.reviewer.run_eval \
  --experiment-prefix openswe-reviewer-baseline \
  --max-concurrency 5

# 想保留 thread 排查问题
uv run python -m evals.reviewer.run_eval --no-cleanup
```

### 12.2 实验前置条件 checklist

```text
[ ] LANGSMITH_API_KEY          # 上传 dataset / 写 experiment
[ ] ANTHROPIC_API_KEY          # judge 用 claude-opus-4-5
[ ] gh auth status             # build_dataset 时用 gh CLI 查 SHA
[ ] langgraph dev 已在跑       # target 需要 http://localhost:2024
[ ] dataset 已上传             # 否则会报 dataset not found
[ ] (可选) REVIEWER_ASSISTANT_ID 指向特定 assistant
[ ] (可选) LANGGRAPH_URL 指向云端而非本地
```

### 12.3 LLM Token 成本预估

> 一次完整跑（50 PRs，5 并发，默认设置）大致：
>
> - **Reviewer Agent 本体**：50 × 1 次完整 PR review ≈ 50 × (10 万-50 万 input tokens) = 几百万到几千万 tokens。**这部分是大头**。
> - **Judge**：~50 × ~3 golden × ~10 candidate = ~1500 次 claude-opus-4-5 调用 × (~500 input + ~80 output)。
>
> 一句话：**烧的几乎全是 reviewer 本身，judge 是配菜**。所以**优化 Open SWE 评测的成本，要从减少 reviewer 不必要的工具调用入手**，而不是优化 judge。

---

## 十三、怎么用 evals 做"开发回归"

典型工作流：

```
1. 改 prompt / 改工具 / 加中间件
        │
        ▼
2. 起 langgraph dev (终端 A)
        │
        ▼
3. run_eval.py --limit 3 (终端 B)  ── 烟测，5-10 分钟
        │
        ▼
4. 检查 LangSmith experiment 页
   ├─ 看 micro_f1 / macro_f1 是不是没崩
   ├─ 点开 fp 高的 example → 看 agent 找出了什么"金标没有"的问题
   ├─ 点开 fn 高的 example → 看 agent 漏了什么 → 改 prompt
   └─ 看 judge 的 reasoning 是否合理（必要时改判官 prompt）
        │
        ▼
5. 跑完整评测（50 例），写到 experiment_prefix="openswe-reviewer-{你的改动}"
        │
        ▼
6. 在 LangSmith 上对比 baseline 和新 experiment 的差异
   - micro_f1 提升 + macro_f1 没退 → 真改好了
   - micro_f1 提升 + macro_f1 退 → 你在某些大 PR 上变好，但伤了一些小 PR
   - 两个都退 → 回滚
```

### 一个具体例子：把 reviewer 的严重度门槛降下来

假设你想"让 reviewer 也报 low / informational 级别的问题"。改动两步：

1. `agent/reviewer.py` 的 prompt 里去掉"严苛校准"段落；
2. `publish_review(severity_threshold="low")` 默认值改成 `"low"`。

跑一次 eval，预期看到：
- **recall ↑**（抓得多了，漏的少了）
- **precision ↓**（误报也变多）
- **f1 走向不定**——看 LLM 在低严重度上是稳还是飘。

这就是 eval 最大的价值：**让"应该降一档"变成"实测降一档后 f1 升 / 降几个点"**，把直觉变成数字。

---

## 十四、和 Devin Review / Greptile / CodeRabbit 横评

Open SWE Reviewer 用 Martian benchmark 是有目的的——**Martian 已经在同一份数据集 + 同一个 judge prompt + 同一个 judge 模型上**给 Devin Review 等商业工具打了分。你要做的就是：

1. 把自己的 micro/macro 数字记下来；
2. 去 Martian leaderboard（`withmartian/code-review-benchmark` README）抓 Devin/Greptile 等的数；
3. 直接比。

这种"用对方公开评测的同一脚本"做横评的方式，比"我们自己造的 benchmark 我们最强"可信度高得多。

---

## 十五、扩展评测：超出 reviewer 之外

如果想给**主写代码 agent**也做 eval，目前项目里**没有现成脚手架**，但已有的 evals/reviewer 是好模板。可参考思路：

| 维度 | 建议 metric | 数据来源 |
|---|---|---|
| 能否提 PR | binary：PR 是否真的被开出来 | LangSmith run + GitHub API |
| PR 通过率 | CI 是否绿 | `scripts/check_pr_merge_status.py` |
| 人工接收度 | PR 是否被 merge | GitHub API |
| 改动幅度 | LOC 增删 / file count | `git diff --stat` |
| 修复成功率 | 跟 issue 关联的 PR 是否真解决 issue | 人工标 + judge LLM |
| 安全 | 是否引入 secret / SSRF / SQL injection | bandit / semgrep / judge LLM |

可以仿照 `evals/reviewer/` 加一个 `evals/agent/`，重用 `judge.py` 的 micro/macro 套路，把 "PR is merged within 7 days" 当 binary label，调 `langsmith.aevaluate` 就行。

---

## 十六、要点回顾

### CI/CD
- **4 个 yml 文件，各司其职**：质量门禁（ci.yml）、命名规范（pr_lint.yml）、自动发布（promote_main_to_prod.yml）、依赖更新（dependabot.yml）。
- **uv + `--locked` + SHA pin** 是供应链安全的三板斧。
- **concurrency: cancel-in-progress** 帮你省钱、省时间。
- **main → prod 强推 + LangGraph Cloud 监听 prod** 是这套发布的"隐式契约"。

### Evals
- **金标 + LLM 裁判 + LangSmith experiment** 三件套。
- **micro/macro 同时看**，理解二者差异。
- **dataset 一旦冻结就不变**——SHA 是关键。
- **judge prompt 不要乱改**，保持和 Martian 一致才能横评 Devin。
- **改 agent 行为前先 `--limit 3` 烟测**，再跑完整。
- **成本主要在 reviewer 本身**，不在 judge。

---

## 十七、推荐阅读顺序

1. 先看 `.github/workflows/ci.yml`（30 行，直观）。
2. 再看 `evals/reviewer/README.md`（70 行，作者亲自解释设计意图）。
3. `evals/reviewer/build_dataset.py` → `target.py` → `judge.py` → `run_eval.py` 顺序读。
4. 拿 1 个 golden 文件用 `--dry-run --limit 1` 实际跑一次，看输出 JSON。
5. 跑一次 `--limit 3` 真评测，去 LangSmith 看 experiment 长什么样。
6. 改一点 reviewer prompt，再跑一次，看数字怎么动。

到这一步，你就有了完整的"agent 开发 → 评测 → 迭代"闭环。

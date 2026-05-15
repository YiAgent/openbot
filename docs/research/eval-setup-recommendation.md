# 自研 GitHub Bot Eval 体系推荐方案

> 制定日期：2026-05-14
> 适用场景：个人开发者，单轮 eval 预算 $50–200
> 配套报告：[robobun & 同类 bot 调研](./robobun-and-similar-bots.md) · [GitHub Bot 评测 benchmark 调研](./github-bot-evaluation-benchmarks.md) · [Eval Runner 开发计划](./eval-runner-development-plan.md)

---

## 你的输入

- **Bot MVP 功能**：① PR 代码 review（评论）② Issue → PR 自动 bugfix ③ Issue triage / 复现 / dedup
- **预算**：中等（单轮 $50–200）
- **不在 MVP**：PR description / commit message / 文档生成（这部分先跳过）

## TL;DR 一句话推荐

**框架用 Inspect AI**（UK AISI 开源，原生 agent + sandbox + SWE-bench task），**tracking / experiment 用 LangSmith**（与 LangChain / LangGraph 原生集成）；如果未来更重视自托管，再切 Langfuse。三个功能各自的核心 benchmark：

| 功能 | 核心 benchmark | 单轮成本 | 跑的频率 |
|---|---|---|---|
| PR review | Martian Code Review Bench（50 PR）+ 自家 30-50 PR shadow set | $5–40 | 每次 prompt 改动 |
| Issue → PR bugfix | SWE-bench Lite（300，Haiku/mini 跑迭代）+ Aider Polyglot（225） | $50–200 | 主版本变更 |
| Issue triage / 复现 / dedup | GitBugs 分类子集 + LIBRO（复现）+ 自家 issue 抽样 | $10–50 | 每次 prompt 改动 |

**报对外分数时**才花一次 $300–500 跑全量 SWE-bench Verified + Sonnet/Opus；日常用 Haiku 4.5 / GPT-5 mini 在 Lite + Polyglot 上迭代。

---

## Part 1 · 框架选型

### 主框架：Inspect AI

为什么不用其它：

| 候选 | 推荐？ | 理由 |
|---|---|---|
| **Inspect AI** | ✅ 主选 | 免费开源、原生 agent eval、内置 SWE-bench task、Docker sandbox 抽象、能映射到 Open SWE 的 LangGraph + sandbox |
| LangSmith Evals | 🔶 不是主 runner | 很适合做 dataset / experiment / online eval / human review；但仍不如 Inspect 适合作为统一 agent runner |
| Braintrust | ❌ | UI 最好，但收费，个人开发者不值 |
| Promptfoo | 🔶 辅助 | YAML 最低门槛，适合 prompt 微调，但不是 agent-native |
| OpenAI Evals | ❌ | 单 turn 设计，跑不了 agent trajectory |
| DeepEval | ❌ | RAG 评测强项，coding agent 不是它专长 |

### Tracking / Experiment：LangSmith 主选

- 与 LangChain / LangGraph 原生集成，接 trace 最省胶水代码
- 同时支持 dataset、offline experiment、online evaluation、annotation queue、pairwise comparison
- 能直接承接现有 reviewer eval 资产，迁移成本最低
- 很适合记录 `(tokens_in, tokens_out, model, $/task, latency)` 并对比不同版本实验

如果未来更重视**完全自托管**，再把观测层切到 Langfuse；但对当前项目，先用 LangSmith 更务实。

### 一行装配

```bash
# 主框架
uv add inspect-ai inspect-evals

# Tracking / Experiment
export LANGSMITH_API_KEY=...

# 跑现成 SWE-bench Verified task
inspect eval inspect_evals/swe_bench_verified --model anthropic/claude-haiku-4-5
```

Inspect 的 `inspect_evals` 仓库自带 SWE-bench、SWE-bench Verified、SWE-bench Multimodal、CyBench、GAIA 等 200+ task，你的 bot 只需要包成一个 `Solver` 就能复用。

---

## Part 2 · 三个功能的 benchmark 推荐

### 功能 1：PR review

**目标**：bot 在 PR 上 comment，指出真实 bug / 改进建议。

#### 推荐 benchmark：分三层

**Layer A — Martian Code Review Bench（必跑）**

- 仓库：[withmartian/code-review-benchmark](https://github.com/withmartian/code-review-benchmark)
- 数据：50 PR × 5 repo（Sentry / Grafana / Cal.com / Discourse / Keycloak），每个 PR 有人工核实过的 "real issues" 黄金列表
- 评测：LLM-as-judge 把 bot 评论与黄金列表对齐，输出 Precision / Recall / F1
- 单次成本：$5–30（50 个 PR，每个 review 一次，~50K tokens × 50 = 2.5M tokens）
- 为什么必跑：**业界唯一被认可的第三方开源 review benchmark**，CodeRabbit / BugBot 都报这个分；你能拿到行业可比的数字

**Layer B — 自家 30-50 PR shadow set（最关键）**

- 数据：从你自己的 monorepo 或你 bot 服务的目标仓库，抽过去 6 个月已合并的 30-50 个 PR
- 评测：让 bot 对 PR diff 跑 review，但**不公开评论**给原作者；事后比对：
  - bot 评论数 vs 真实 reviewer 评论数
  - 真实 reviewer 提到的 bug，bot 是否 catch（recall）
  - bot 提到但 reviewer 没提的，是否真有问题（precision，需要你自己 spot-check）
- 单次成本：$10–50
- 为什么最关键：**完全防污染、贴近你真实分布、连 GPT-5 都没见过**

**Layer C — Online resolution rate（上线后必加）**

- 把 bot 上线后，对每条 inline comment 埋点
- 看 PR merge 时，bot 评论处的代码是否被改动
- 长期跟踪 resolution rate（参考 Cursor BugBot 范式：52% → 70%+）
- 成本：~零（生产数据，只需 API 写日志）

#### 评测命令示例

```bash
# Martian benchmark
git clone https://github.com/withmartian/code-review-benchmark
cd code-review-benchmark
# 把 bot 包成 reviewer plugin（他们的 README 有 spec）
python run.py --bot path/to/your_bot_adapter.py --model claude-sonnet-4-5

# 自家 shadow set（自己实现，建议用 LangSmith dataset）
python eval/shadow_review.py --dataset internal_prs_v1 --bot-version v0.3.2
```

#### 不要跑

- **CodeReviewer FSE 2022（BLEU 评分）**：BLEU 已被证明跟人类判断弱相关，跑了也没意义
- **Greptile 自评 benchmark**：vendor-curated，Augment 复跑差 37 个点，没公信力

---

### 功能 2：Issue → PR 自动 bugfix

**目标**：bot 收到 issue 后能写出修复 patch，FAIL_TO_PASS 转绿。

#### 推荐 benchmark：分三层

**Layer A — SWE-bench Lite（300，日常迭代）**

- 数据：300 个 Python instance，从 SWE-bench 2294 抽样
- 评测：apply patch → 跑 FAIL_TO_PASS / PASS_TO_PASS
- 单次成本：
  - **Claude Haiku 4.5**：$50–150（推荐用这个迭代）
  - Claude Sonnet 4.5：$200–500（偶尔跑里程碑用）
  - **GPT-5 mini / OpenAI o4-mini**：$30–80（最省）
- 为什么 Lite 而不是 Verified：你预算够不上日常 Verified；Lite 题目子集跟 Verified 重合度高，能反映"有没有退化"，是开发期日常 CI 的标配
- 跑法：

```bash
inspect eval inspect_evals/swe_bench_lite \
  --model anthropic/claude-haiku-4-5 \
  --max-samples 300 \
  --max-connections 10
```

**Layer B — Aider Polyglot（225 题 × 6 语言）**

- 数据：Exercism 习题，C++/Go/Java/JS/Python/Rust
- 评测：跑题目自带 unit test，`% Correct` + `Edit format adherence`
- 单次成本：
  - **Claude Haiku 4.5**：$5–30
  - Claude Sonnet 4.5：$30–100
  - GPT-5 mini：$3–20
- 为什么跑：① 测多语言能力 ② 测 diff 格式鲁棒性 ③ 跑得快（半小时级）④ 防止只在 Python 过拟合
- 跑法：

```bash
git clone https://github.com/Aider-AI/polyglot-benchmark
cd polyglot-benchmark
aider --model claude-haiku-4-5 --benchmark
```

**Layer C — SWE-bench Verified 全量（里程碑发版前跑一次）**

- 数据：500 instance，人工 reviewed
- 评测：同 SWE-bench
- 单次成本：
  - Claude Sonnet 4.5：$300–800（推荐用这个出对外数字）
  - Claude Haiku 4.5：$80–200（如果只看自己内部对照）
- 频率：版本号 bump 一次（v0.1 → v0.2）跑一次，平时不跑

```bash
inspect eval inspect_evals/swe_bench_verified \
  --model anthropic/claude-sonnet-4-5 \
  --max-samples 500
```

#### 不要跑（个人开发者预算下）

- **SWE-bench Pro Private**：需要联系 Scale AI 申请 access，且任务粒度大（平均 107 行 patch、~$5–20/task），单轮跑全量超出 $200 预算
- **SWE-bench Multimodal**：除非你的 bot 处理前端任务，否则跳过
- **Multi-SWE-bench**：单语言不需要，多语言用 Aider Polyglot 已经够
- **SWE-Lancer**：Expensify 仓库覆盖太窄，对通用 bot 信号弱
- **MLE-bench / RE-Bench**：ML 研究 agent 方向，与 GitHub bot 无关
- **BigCodeBench / HumanEval+ / LiveCodeBench**：function-level，留作"基础模型能力健康检查"，不投精力

---

### 功能 3：Issue triage / 复现 / dedup

**目标**：bot 收到 issue 后能 ① 自动 label ② 找出重复 issue ③ 尝试复现 ④ 评估优先级。

#### 推荐 benchmark：按子能力分

**子能力 a · 自动 label / 分类**

- benchmark：**GitBugs**（[arXiv 2504.09651](https://arxiv.org/abs/2504.09651)，15 万+ 报告，预切 train/test）
- 评测：multi-class F1（severity / type / component），与 ground truth 对比
- 单次成本：**$10–30**（只调 LLM 分类，不跑 agent loop，token 量小）
- 跑法：用 Inspect AI 写个简单 task，dataset 用 GitBugs 提供的 test split

**子能力 b · Duplicate detection（dedup）**

- benchmark：**Cupid 论文 dataset**（[arXiv 2308.10022](https://arxiv.org/abs/2308.10022)）或自己从 GitHub close-as-duplicate 抓
- 评测：Recall@10（给 bot 一个 candidate list，看真重复在不在前 10）—— 不要用 P@1，dedup 是 retrieval 任务
- 单次成本：$5–20
- 备注：你 bot 实际跑 dedup 时会先嵌入 + 检索 + LLM 排序；评测只测最终 ranking

**子能力 c · 自动复现（reproducer）**

- benchmark：**LIBRO**（[arXiv 2209.11515](https://arxiv.org/abs/2209.11515)，[coinse/libro](https://github.com/coinse/libro)）+ **Defects4J / BugsInPy** 数据集
- 评测：reproduce success rate —— bot 生成的 test 是否 fail-before-fix / pass-after-fix
- 单次成本：$30–100（要跑 agent loop + 在 sandbox 里 apply patch 验证）
- 重要性：**这是 robobun 的核心能力**——如果你的 bot 主打"先复现再开干"，这个指标必须报

**子能力 d · 自家 issue retrospective**

- 数据：从你自己 repo 抽 50-100 个已 closed-as-duplicate / 已 labeled 的 issue
- 评测：let bot 重新跑一遍，比对它输出的 label / duplicate-of 与历史正确答案
- 单次成本：$10–40
- 价值：**最贴近你真实分布**——比 GitBugs 更能反映你 bot 实际用户的 issue 分布

#### 不要跑

- **DRONE / EMSE 2014 priority prediction**：方法老，数据集小，不值得花时间复现
- **KGYM**（Linux 内核 crash 复现）：除非你 bot 主打内核，否则任务太特殊

---

## Part 3 · 单轮成本预算表

按你的中等预算（$50–200/轮）规划：

| Benchmark | 任务数 | 用 Haiku 4.5 | 用 Sonnet 4.5 | 用 GPT-5 mini | 频率 |
|---|---|---|---|---|---|
| Martian Code Review | 50 PR | $5–15 | $15–40 | $3–10 | 每次 prompt 改 |
| 自家 PR shadow set | 30-50 PR | $10–30 | $30–80 | $5–20 | 每次 prompt 改 |
| SWE-bench Lite | 300 | **$50–150** | $200–500 | $30–80 | 每周 |
| Aider Polyglot | 225 | $5–30 | $30–100 | $3–20 | 每周 |
| GitBugs 分类子集 | 500-1000 | $5–15 | $15–40 | $2–8 | 每次 prompt 改 |
| LIBRO 复现 | 50-100 | $20–60 | $50–150 | $15–40 | 双周 |
| 自家 issue retrospective | 50-100 | $5–20 | $15–50 | $3–10 | 每次 prompt 改 |
| **SWE-bench Verified（里程碑）** | 500 | $80–200 | **$300–800** | $50–150 | 月度 / 发版前 |

**Sonnet 4.5 报价**：input $3/M、output $15/M。**Haiku 4.5**：input $0.80/M、output $4/M（5x 便宜）。**GPT-5 mini**：input $0.25/M、output $2/M（~10x 便宜）。

**典型每周开销**（开发期）：
- 每周 1 次 Lite + Polyglot（Haiku 4.5）= ~$80–180
- 每次 prompt 调整 ~3 次小 eval（Martian + shadow + 分类）= ~$30–80
- **每月总计 ~$300–600**

**典型每月开销**（稳定期）：
- 月度 1 次 Verified（Sonnet 4.5）= ~$400–800
- 周度 Lite/Polyglot 回归（Haiku）= ~$300–600
- **每月总计 ~$700–1400**

如果太贵，**用 Haiku 4.5 跑迭代、用 Sonnet 4.5 跑里程碑**是最性价比的组合。

---

## Part 4 · 实施 Roadmap（4 周）

### Week 1 · 框架 + 自家数据集

**目标**：能跑通最小 eval loop，对照你目前的 bot 给出第一组 baseline 分数。

- [ ] 装 Inspect AI + LangSmith
- [ ] 把 Open SWE 的 `agent.server:get_agent` 包成 Inspect `Solver`（参考 [inspect_evals/swe_bench](https://github.com/UKGovernmentBEIS/inspect_evals/tree/main/src/inspect_evals/swe_bench)）
- [ ] **建自家 PR shadow set**：从你 bot 服务的目标 repo 抽 30-50 个 merged PR，存成 `internal_prs_v1.jsonl`
- [ ] **建自家 issue retrospective set**：抽 50-100 个 closed issue 含 label / duplicate-of 关系
- [ ] 跑一次 Aider Polyglot baseline，记录数字

**产出**：v0.1 baseline 报告（Aider Polyglot + 自家 shadow + 自家 issue），用 Haiku 4.5

**预算**：~$50

### Week 2 · 接入公开 benchmark

- [ ] 跑 Martian Code Review Bench 一次（Sonnet 4.5）
- [ ] 跑 SWE-bench Lite（Haiku 4.5）
- [ ] 跑 GitBugs 分类子集（Haiku 4.5）
- [ ] 设 LangSmith 自动采集 cost / latency / step count

**产出**：v0.2 报告含 4 个 benchmark 分数 + cost 表

**预算**：~$150

### Week 3 · 加 trajectory / 上线监控 / A-B 框架

- [ ] 加 trajectory scorer（用 [langchain-ai/agentevals](https://github.com/langchain-ai/agentevals)）：记录每次 run 的 tool 调用序列、step 数、retry 次数
- [ ] 上线后给每条 bot 评论埋 `comment_id`，在 PR merge webhook 里检查该处代码是否被改 → resolution rate
- [ ] Slack 加 thumbs up/down reaction，作为长期 Elo 信号

**产出**：v0.3 含 online 指标 + trajectory metrics

**预算**：~$80（主要是 trajectory eval）

### Week 4 · 跑里程碑 + 红队

- [ ] 跑 SWE-bench Verified 全量（Sonnet 4.5）—— 出对外可公布数字
- [ ] 跑 LIBRO 复现 benchmark（如果 bot 主打 reproducer）
- [ ] 手写 20 条 prompt injection 红队 case 跑一遍（参考 ["Comment and Control"](https://oddguan.com/blog/comment-and-control-prompt-injection-credential-theft-claude-code-gemini-cli-github-copilot/) 范式）

**产出**：v0.4 对外发布报告 + 安全断言

**预算**：~$400

---

## Part 5 · 具体 code 示例

### 5.1 把 bot 包成 Inspect Solver

```python
# eval/solvers.py
from inspect_ai.solver import solver, Generate, TaskState
from agent.server import get_agent  # 你的 Open SWE 入口

@solver
def open_swe_solver():
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        # 准备 sandbox + issue 文本
        issue = state.input_text
        config = {"configurable": {"thread_id": state.sample.id}}

        agent = await get_agent(config)
        result = await agent.ainvoke({"messages": [("user", issue)]}, config)

        # 从 sandbox 拉 patch
        patch = await extract_patch_from_sandbox(state.sample.id)
        state.output.completion = patch
        return state
    return solve
```

### 5.2 自家 shadow set 评测脚本

```python
# eval/shadow_review.py
from inspect_ai import Task, eval, task
from inspect_ai.dataset import json_dataset
from inspect_ai.scorer import scorer, Score

@scorer(metrics=["precision", "recall", "f1"])
def review_overlap_scorer():
    """用 LLM judge 比对 bot 评论与真实 reviewer 评论的重合度"""
    async def score(state, target):
        bot_comments = state.output.completion
        real_comments = target.text  # 真实 reviewer 评论
        # 调 Claude 当 judge 评估重合度
        ...
        return Score(value={"precision": p, "recall": r, "f1": f})
    return score

@task
def shadow_review_v1():
    return Task(
        dataset=json_dataset("internal_prs_v1.jsonl"),
        solver=open_swe_review_solver(),
        scorer=review_overlap_scorer(),
    )
```

### 5.3 CI gate 配置

```yaml
# .github/workflows/eval-regression.yml
name: Eval Regression
on:
  pull_request:
    paths: [ "agent/**", "prompts/**" ]
jobs:
  eval:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: uv sync
      - name: Run cheap regression suite
        run: |
          inspect eval inspect_evals/swe_bench_lite \
            --model anthropic/claude-haiku-4-5 \
            --max-samples 50 \
            --log-dir ./eval-logs
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
      - name: Check regression
        run: python scripts/check_eval_threshold.py --min-resolved 0.35
```

---

## Part 6 · 不要做的事（避免烧钱 / 走弯路）

1. **不要一上来就跑 SWE-bench Verified 全量**——一次 Sonnet 4.5 跑下来 $500+，开发期改一行 prompt 就报废。先用 Lite + Haiku 跑。
2. **不要相信自己跑出来的 vendor 自评数字**——只有第三方独立榜单（Martian / Scale / Epoch）才有公信力。自家 shadow set 信号最强，但**只对自己**有意义，不能对外比较。
3. **不要用 BLEU/ROUGE 评 review 或 PR description**——已被反复证明跟人类判断弱相关。用 LLM judge with rubric。
4. **不要只信一个数字**。SWE-bench Lite、Martian、自家 shadow、online resolution rate 这四类信号要同时存在且互相印证，单一指标一定会被 over-fit。
5. **不要跑 Multi-SWE-bench**——除非你明确支持 5+ 语言。MVP 阶段单 Python 用 Lite 即可，多 1 个语言加 Aider Polyglot 对应语言切片。
6. **不要装 Braintrust / DeepEval / Phoenix 一堆框架**——个人开发者一个 Inspect AI + 一个 LangSmith 已经够，多装一个就多一份维护成本。
7. **不要漏掉 prompt injection 红队**——你的 bot 一旦能 push commit 或评论里贴 token，必须把 ["Comment and Control"](https://oddguan.com/blog/comment-and-control-prompt-injection-credential-theft-claude-code-gemini-cli-github-copilot/) 范式的 20 条 case 当回归测试。这个不需要外部 benchmark，自己写就行。

---

## Part 7 · 一张图概括

```
                        ┌─ Martian Bench ──┐
PR Review        ──────┤  自家 shadow set  ├── 每次 prompt 改
                        └─ online resolution┘

                        ┌─ SWE-bench Lite ──┐
Issue → PR              │  Aider Polyglot   │── 每周
bugfix           ──────┤  自家 issue→PR    │
                        └─ Verified（月度） ──┘

                        ┌─ GitBugs 分类    ──┐
Issue triage /          │  Cupid dedup       │── 每次 prompt 改
复现 / dedup     ──────┤  LIBRO 复现        │── 双周
                        └─ 自家 retrospective┘

────────────────────────────────────────────────
框架：Inspect AI（运行）+ LangSmith（tracking / experiment）
模型：开发 Haiku 4.5 / 里程碑 Sonnet 4.5
预算：开发期 ~$300-600/月，稳定期 ~$700-1400/月
```

---

## 关键链接（按工具/数据集）

- **Inspect AI**：[inspect.aisi.org.uk](https://inspect.aisi.org.uk/) · [inspect_ai GitHub](https://github.com/UKGovernmentBEIS/inspect_ai) · [inspect_evals](https://github.com/UKGovernmentBEIS/inspect_evals)
- **LangSmith**：[smith.langchain.com](https://smith.langchain.com/)
- **Langfuse**：[langfuse.com](https://langfuse.com/) · [self-host docs](https://langfuse.com/self-hosting)（若后续转向自托管）
- **Martian Code Review Bench**：[codereview.withmartian.com](https://codereview.withmartian.com/) · [GitHub](https://github.com/withmartian/code-review-benchmark)
- **SWE-bench**：[swebench.com](https://www.swebench.com/) · [Lite](https://www.swebench.com/lite.html) · [Verified](https://www.swebench.com/verified.html)
- **Aider Polyglot**：[Leaderboards](https://aider.chat/docs/leaderboards/) · [polyglot-benchmark](https://github.com/Aider-AI/polyglot-benchmark)
- **GitBugs**：[arXiv 2504.09651](https://arxiv.org/abs/2504.09651)
- **LIBRO**：[arXiv 2209.11515](https://arxiv.org/abs/2209.11515) · [coinse/libro](https://github.com/coinse/libro)
- **Cupid**：[arXiv 2308.10022](https://arxiv.org/abs/2308.10022)
- **AgentEvals**：[langchain-ai/agentevals](https://github.com/langchain-ai/agentevals)
- **Prompt Injection 案例**：[Comment and Control](https://oddguan.com/blog/comment-and-control-prompt-injection-credential-theft-claude-code-gemini-cli-github-copilot/)

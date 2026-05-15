# Open SWE 测试体系深度解析

> 这是项目教学文档的第 3 篇。
> 第 1 篇讲"项目长什么样"（`PROJECT_GUIDE_CN.md`）；
> 第 2 篇讲"CI/CD + Evals"（`CICD_AND_EVALS_CN.md`）；
> 这一篇专门讲 **`tests/` 目录里 29 个测试文件、~7000 行单测是怎么设计的**。
>
> 看完这篇你能：① 解释 pytest 配置；② 看懂每种测试模式的套路；③ 给新功能挑对正确的测试模式写新测试。

---

## 一、先看全貌

```
tests/
├── middleware/                          # 中间件场景化测试（沙箱恢复链路）
│   └── test_sandbox_recovery.py
├── test_auth_sources.py                 # ↓ 30 个文件，按"被测对象"切片
├── test_daytona_integration.py
├── test_encryption.py
├── test_ensure_no_empty_msg.py
├── test_github_comment_prompts.py
├── test_github_issue_webhook.py         ← 最大：1284 行
├── test_github_token_ttl.py
├── test_http_security.py                ← SSRF 防护
├── test_langsmith_sandbox_config.py
├── test_model_fallback_middleware.py
├── test_multimodal.py
├── test_notify_step_limit_middleware.py
├── test_proxy_auth.py
├── test_public_repo_org_gate.py
├── test_recent_comments.py
├── test_refresh_slack_status_middleware.py
├── test_repo_extraction.py
├── test_reviewer.py
├── test_reviewer_diff.py
├── test_reviewer_findings.py
├── test_reviewer_publish.py
├── test_reviewer_tools.py
├── test_reviewer_watch.py
├── test_sandbox_paths.py
├── test_sanitize_tool_inputs.py
├── test_slack_assistants_status.py
├── test_slack_context.py                ← 第二大：754 行
└── test_slack_feedback.py

# 一共 7048 行测试代码。生产代码 ~11000 行 → 测试代码占比约 64%。
```

**这套测试有两个特征**：
1. **完全单元测试，没有 integration test**。`tests/integration_tests/` 不存在，Makefile 里那个 target 是 no-op。
2. **命名严格按"被测文件"对应**：`test_reviewer_diff.py` 测 `agent/reviewer_diff.py`，`test_encryption.py` 测 `agent/encryption.py`，1:1 不打折。

---

## 二、pytest 配置（极简）

`pyproject.toml`：

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

依赖（在 `[project.optional-dependencies].dev`）：

```toml
pytest>=9.0.3
pytest-asyncio>=1.3.0
```

就这两条配置在控制全局：

### `asyncio_mode = "auto"` —— 不写装饰器

默认情况下用 pytest-asyncio 需要每个 async 函数加 `@pytest.mark.asyncio`。`auto` 模式让 pytest **自动把所有 `async def test_xxx`** 当成 asyncio 测试。

```python
# ✅ auto 模式下：直接写
async def test_get_github_token_from_thread_skips_expired() -> None:
    ...

# ❌ 不需要这么写
@pytest.mark.asyncio
async def test_x() -> None:
    ...
```

但项目里**还是有不少地方显式写了 `@pytest.mark.asyncio`**（见 `test_reviewer.py:17`、`test_proxy_auth.py` 各处）——这是冗余但无害的写法，因为它们在 auto 模式下早就生效了。我猜是写早期的代码留下来的，删掉也行。

### `testpaths = ["tests"]`

让 `pytest`（不带参数）只扫 `tests/` 目录，不会跑到 `evals/` 之类的地方。

### 没有 conftest.py

整个项目**一个 conftest.py 都没有**。这意味着：
- 没有项目级 fixture，每个测试自包含；
- 没有跨文件复用的 mock 工厂；
- 想跑某个测试不需要先 understand 一层 fixture 树。

**取舍点**：阅读友好（看到测试就懂依赖），但有些 setup（比如设置 `TOKEN_ENCRYPTION_KEY` 环境变量）就要在多个文件里重复。

---

## 三、命名规则与组织模式

### 3.1 文件名 = 被测模块名

| 测试文件 | 测的生产文件 |
|---|---|
| `test_encryption.py` | `agent/encryption.py` |
| `test_reviewer_diff.py` | `agent/reviewer_diff.py` |
| `test_sanitize_tool_inputs.py` | `agent/middleware/sanitize_tool_inputs.py` |
| `test_github_issue_webhook.py` | `agent/webapp.py` 里的 GitHub webhook 路径 |
| `test_slack_context.py` | `agent/utils/slack.py` 里上下文相关函数 |

**特殊情况**：webapp.py 太大被测试拆成了多个文件——`test_github_issue_webhook.py`、`test_public_repo_org_gate.py`、`test_slack_context.py`、`test_slack_feedback.py` 全都在测 webapp 的不同切面。

### 3.2 类命名 = `Test{ProductionFunction/Behavior}`

```python
# tests/test_encryption.py
class TestParseEncryptionKeys: ...        # 测 _parse_encryption_keys 函数
class TestGetEncryptionKeys: ...          # 测 _get_encryption_keys 函数
class TestSingleKeyRoundtrip: ...         # 测加解密往返
class TestMultiKeyDecrypt: ...            # 测 multi-key 解密
class TestRotationRoundtrip: ...          # 测 key 轮换全流程
```

**类只是命名空间**，不继承 `unittest.TestCase`，**不用 `setUp/tearDown`，状态全部局部化**。`pytest-asyncio` 也支持类方法风格的 fixture，但项目几乎不用，靠 monkeypatch 解决一切初始化。

### 3.3 测试函数名 = "what it asserts"

风格规范：`test_{被测函数或场景}_{期望行为}`，**结果直接在名字里**：

```python
def test_extracts_leading_integer_from_comma_string() -> None:   # 输入→期望
def test_returns_none_when_no_digits() -> None:                  # 输入→期望
def test_does_not_mutate_original_dict() -> None:                # 反向断言

# Asyncio 场景
async def test_get_github_token_from_thread_skips_expired() -> None:
async def test_sandbox_client_error_recreates_sandbox() -> None:
```

**别写**：`test_encryption_1` / `test_basic` / `test_happy_path`。看到 fail report 里只有名字就要能猜出哪一行业务断了。

---

## 四、AAA 模式（Arrange-Act-Assert）

项目大多数测试都很显式地遵守 AAA。例：

```python
def test_extracts_leading_integer_from_comma_string(self) -> None:
    # Arrange
    raw = "1, 80"
    # Act
    result = _coerce_int(raw)
    # Assert
    assert result == 1
```

复杂场景虽然不画注释线，但结构依然清晰。打开 `tests/test_reviewer.py` 看：

```python
async def test_reviewer_uses_cached_thread_token_for_slack_review_request() -> None:
    # ─── Arrange ───
    config: RunnableConfig = {
        "configurable": {
            "__is_for_execution__": True,
            "thread_id": "reviewer-thread-id",
            "source": "slack",
            ...
        },
        "metadata": {},
    }
    dummy_agent = _DummyAgent()

    with (
        patch("agent.reviewer.get_github_token_from_thread", ...),
        patch("agent.reviewer.resolve_github_token", ...),
        patch("agent.reviewer.ensure_sandbox_for_thread", ...),
        ...,
    ):
        # ─── Act ───
        await reviewer.get_reviewer_agent(config)

    # ─── Assert ───
    metadata = config["metadata"]
    assert metadata["github_token_encrypted"] == "encrypted-token"
    mock_get_thread_token.assert_awaited_once_with("reviewer-thread-id")
    mock_resolve_token.assert_not_called()
```

注意右下角那一条 `mock_resolve_token.assert_not_called()`——**断言"这件事没发生"也是断言**。这避免了"代码看起来跑过了，但其实走错分支了"。

---

## 五、八种核心测试模式

把项目里所有 7000 行测试归纳一遍，本质上就这 8 种模式。

### 模式 1：纯单元测试（无 mock）

**适用**：纯函数、数据结构、解析器。

例：`test_reviewer_diff.py`

```python
_TWO_FILE_DIFF = """diff --git a/foo.py b/foo.py
...
@@ -10,3 +10,4 @@ def existing():
     pass
+    new_line_13 = 1
+    new_line_14 = 2
...
"""

def test_compute_diff_line_set_covers_each_hunks_new_lines() -> None:
    line_set = compute_diff_line_set(_TWO_FILE_DIFF)
    assert line_set["foo.py"] == {10, 11, 12, 13}
    assert line_set["bar.py"] == {1, 2, 3, 51, 52, 53, 54}
```

**特点**：
- 输入是常量字符串；
- 没有外部依赖（不读文件、不发请求）；
- 一眼看清"输入"和"期望输出"。

项目里这类测试占比最大：`test_reviewer_diff.py`、`test_reviewer_findings.py`、`test_sanitize_tool_inputs.py`、`test_repo_extraction.py`、`test_encryption.py` 都是这一类。

### 模式 2：monkeypatch 环境变量

**适用**：依赖 `os.environ` 的代码。

```python
def _set_key(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", value)

def test_missing_env_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TOKEN_ENCRYPTION_KEY", raising=False)
    with pytest.raises(EncryptionKeyMissingError):
        _get_encryption_keys()
```

`monkeypatch` 是 pytest 内置 fixture，**测试结束自动还原**——绝不会污染后续测试。这是为什么测试可以放心改环境变量。

### 模式 3：`autouse=True` fixture 设置通用前置

例：`test_github_token_ttl.py` 里每个测试都要加密 Token，加密又需要 key：

```python
_TEST_FERNET_KEY = "GMI8FNqVnhFzVfKDUTpGAUq8a2cm14kU0SyXzMTM4Yc="

@pytest.fixture(autouse=True)
def _set_encryption_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", _TEST_FERNET_KEY)
```

`autouse=True` 意思是**这个文件里所有测试都自动跑这个 fixture**，不用每个测试显式写参数。

**注意点**：硬编码 Fernet key 是**测试专用**，不能复制到 production 配置里。

### 模式 4：`unittest.mock.patch` 拦截函数

**适用**：依赖外部库（httpx、langgraph_sdk、langsmith）。

```python
from unittest.mock import AsyncMock, MagicMock, patch

with (
    patch("agent.reviewer.get_github_token_from_thread",
          new_callable=AsyncMock, return_value=("app-token", "encrypted-token", None)),
    patch("agent.reviewer.ensure_sandbox_for_thread",
          new_callable=AsyncMock, return_value=MagicMock()),
    patch("agent.reviewer.make_model", return_value=MagicMock()),
    patch("agent.reviewer.create_deep_agent", return_value=dummy_agent),
):
    await reviewer.get_reviewer_agent(config)
```

**几个细节**：

1. **patch 路径写"被使用的地方"，不是"定义的地方"**。
   `get_github_token_from_thread` 定义在 `agent.utils.github_token` 里，但 `reviewer.py` 用 `from .utils.github_token import get_github_token_from_thread` 导入，于是测试 patch 的是 `agent.reviewer.get_github_token_from_thread`——**导入后的引用**。这条规则是 Python mock 新手最常踩的坑。

2. **`AsyncMock` vs `MagicMock`**。同步函数用 `MagicMock`，async 函数用 `AsyncMock`——后者支持 `await` 协议。混用会得到 `TypeError: object MagicMock can't be used in 'await' expression`。

3. **`with (patch(...), patch(...), ...)` 多 patch 组合写法**（Python 3.10+ 的 PEP 617 括号化 with）。比嵌套 `with` 干净。

### 模式 5：`monkeypatch.setattr` 替换模块属性

**适用**：依赖项目内函数（不是第三方库）。

```python
async def fake_post_slack_ephemeral_message(channel_id, user_id, text, thread_ts=None) -> bool:
    called["channel_id"] = channel_id
    ...
    return True

monkeypatch.setattr(auth, "post_slack_ephemeral_message", fake_post_slack_ephemeral_message)
```

**setattr vs patch 怎么选**：
- 项目内函数：用 `monkeypatch.setattr(module, name, fake)`，**重启即恢复**。
- 第三方库 / 跨模块 import：用 `patch("a.b.c")`，**显式 patch path**。

实际上两者可以互换，但 monkeypatch 更轻；`patch` 在需要 `assert_called_with` 这类自动断言时更方便。

### 模式 6：FakeXxx 类替代 Mock

**适用**：被测代码会调多个方法、有状态。

例：`test_github_token_ttl.py` 自造的 httpx mock：

```python
class _MockResponse:
    def __init__(self, status_code: int, json_data: Any | None = None) -> None:
        self.status_code = status_code
        self._json = json_data or {}
    def json(self) -> Any:
        return self._json


class _MockHttpxClient:
    def __init__(self, status_code: int, json_data: Any | None = None) -> None:
        self.status_code = status_code
        self.json_data = json_data
        self.posts: list[dict[str, Any]] = []
        self.gets: list[dict[str, Any]] = []

    async def __aenter__(self): return self
    async def __aexit__(self, *args): return None

    async def post(self, url, **kwargs):
        self.posts.append({"url": url, **kwargs})
        return _MockResponse(self.status_code, self.json_data)

    async def get(self, url, **kwargs):
        self.gets.append({"url": url, **kwargs})
        return _MockResponse(self.status_code, self.json_data)


# 用法
mock_client = _MockHttpxClient(status_code=401)
monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: mock_client)
```

**对比 `AsyncMock`** 的优势：
- 可以自动 `__aenter__`/`__aexit__`，模拟 context manager；
- 录所有调用历史到 `.posts` / `.gets` 列表里，断言时可以检查"调了几次、传了什么 url"；
- 测多个测试时可以变更状态。

**用 mock 还是用 fake**：调用次数少、单点验证 → mock；模拟复杂的协议对象 → fake class。

### 模式 7：FastAPI `TestClient` 测 webhook

**适用**：测 webapp.py 的 HTTP 入口。

例：`test_github_issue_webhook.py`：

```python
from fastapi.testclient import TestClient
from agent import webapp

_TEST_WEBHOOK_SECRET = "test-secret-for-webhook"

def _sign_body(body: bytes, secret: str = _TEST_WEBHOOK_SECRET) -> str:
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={sig}"

def _post_github_webhook(client: TestClient, event_type: str, payload: dict):
    body = json.dumps(payload, separators=(",", ":")).encode()
    return client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": event_type,
            "X-Hub-Signature-256": _sign_body(body),     # ★ 真实 HMAC 签名
            "Content-Type": "application/json",
        },
    )
```

**关键点**：
- **签名是真的算出来的**，不绕过校验。这保证了 webhook 签名验证逻辑被覆盖到。
- `content=body`（不是 `json=payload`）——因为签名是对原始字节算的，FastAPI 内部 `json=` 可能 re-encode 出不同字节，导致签名对不上。
- 这是**真实 HTTP 请求路径**：经过 FastAPI 路由、依赖注入、签名校验，但**业务侧的副作用（GitHub API 调用、LangGraph runs.create）全都 monkeypatch 掉**——这样测的是"路由 + 校验 + 派发"，不是真实集成。

### 模式 8：`pytest.mark.parametrize` 批量参数化

**适用**：同一逻辑、多组输入。

```python
@pytest.mark.parametrize(
    ("start", "end"),
    [(1, 1), (1, 3)],
)
def test_extract_diff_hunk_supports_single_line_and_range(start: int, end: int) -> None:
    hunk = extract_diff_hunk(_TWO_FILE_DIFF, "bar.py", start, end)
    assert hunk is not None
    assert "import sys" in hunk
```

每组参数 = 一个独立测试 case。fail 时显示 `test_xxx[1-1]` / `test_xxx[1-3]`——比放在一个 for 循环里好得多。

项目里 parametrize 用得不多（grep 出来 < 10 次），更多是"每个 case 一个测试函数"风格。理由：**单独的测试函数名比 parametrize id 描述力强**。

---

## 六、按测试目标的分类

把测试再换一个维度看：**它们到底在保护什么属性？**

### 6.1 行为正确性测试

最常见，约 70% 的测试都是这类。例：

- `test_encryption.py` —— Fernet 加解密是否往返。
- `test_reviewer_diff.py` —— diff parser 是否正确。
- `test_sanitize_tool_inputs.py` —— 字符串 int 是否被正确提取。

**特征**：纯函数 + 常量输入 + 期望输出。

### 6.2 错误路径与边界测试

第二常见。专门测"东西坏了之后会怎样"。

例 1：`test_encryption.py`

```python
def test_decrypt_fails_when_no_key_matches(self, monkeypatch):
    """所有 key 都解不开 → 返回空字符串，而不是 raise"""
    old_key = Fernet.generate_key().decode()
    _set_key(monkeypatch, old_key)
    old_ciphertext = encrypt_token("ghp_token")

    unrelated_key = Fernet.generate_key().decode()
    _set_key(monkeypatch, unrelated_key)
    assert decrypt_token(old_ciphertext) == ""
```

例 2：`test_github_token_ttl.py`

```python
def test_get_github_token_returns_none_for_expired_run_metadata():
    """过期的 token → None（不要返回旧的字符串！）"""
```

**这类测试是项目最值钱的部分**——因为生产 bug 大多发生在"奇怪情况"，写测试就是在制造这些奇怪情况。

### 6.3 安全测试

`test_http_security.py` 单独保护 SSRF 防护层。

```python
def test_fetch_url_blocks_private_ip_without_issuing_a_request(monkeypatch):
    def fail_request(*args, **kwargs):
        raise AssertionError("request should not be issued for blocked URLs")
    monkeypatch.setattr(http_request_tool.requests, "request", fail_request)

    result = fetch_url_tool.fetch_url(
        "http://169.254.169.254/latest/meta-data/iam/security-credentials/"   # AWS 元数据
    )
    assert "Request blocked" in result["error"]
```

**精妙之处**：用一个**主动抛 AssertionError 的 fake `request`** 占位。如果 SSRF 防护失效、代码真的发了请求，立刻 fail——比"事后断言请求没发出"更显眼。

### 6.4 协议兼容性测试

例：`test_proxy_auth.py` 验证发给 LangSmith 的 proxy_config payload 字段名一字不差。

```python
def test_sends_correct_payload_shape(self):
    _configure_github_proxy("sandbox-abc123", token)

    payload = mock_client.patch.call_args.kwargs["json"]
    assert "proxy_config" in payload
    rules = payload["proxy_config"]["rules"]
    assert rules[0]["name"] == "github-api"
    assert rules[0]["match_hosts"] == ["api.github.com"]
    assert rules[0]["headers"][0]["name"] == "Authorization"
    assert rules[0]["headers"][0]["type"] == "opaque"        # ★ 字段名挑剔
    assert rules[0]["headers"][0]["value"] == f"Bearer {token}"
```

**这种"对着外部 API 的 contract 写测试"是必要的**——LangSmith 改字段、你的代码不改，CI 立刻报警。

### 6.5 回归测试（Production trace replay）

代码里写得最直白——`test_sanitize_tool_inputs.py`：

```python
def test_extracts_leading_integer_from_comma_string(self):
    # Production trace 1: offset='1, 80'
    assert _coerce_int("1, 80") == 1

def test_extracts_leading_integer_from_embedded_json(self):
    # Production trace 2: offset='170, "limit": 60'
    assert _coerce_int('170, "limit": 60') == 170

def test_extracts_leading_integer_from_trailing_comma(self):
    # Production trace 3: offset='1504, '
    assert _coerce_int("1504, ") == 1504
```

注释直接写"这是从真实 production 日志里抓出来的输入"。**踩坑 → 加测试**，永远是最有效的工作流。

### 6.6 中间件场景化测试（多状态机模拟）

`tests/middleware/test_sandbox_recovery.py` 是这套测试的精华：完整模拟"沙箱挂掉 → 重建 → 再挂 → 触发电路熔断"链路。

```python
async def test_sandbox_client_error_recreates_sandbox():
    middleware = ToolErrorMiddleware()
    request = _tool_request()
    old_backend, backend = FakeSandboxBackend(), FakeSandboxBackend()
    old_backend.id, backend.id = "sb-old", "sb-new"
    proxy = set_sandbox_backend("thread-1", old_backend)

    async def handler(_request):
        raise SandboxClientError("Sandbox request timed out: sb-dead")    # 故意抛错

    with (
        patch("agent.server._recreate_sandbox", new_callable=AsyncMock) as mock_recreate,
        patch("agent.server.client") as mock_client,
    ):
        mock_recreate.return_value = backend
        result = await middleware.awrap_tool_call(request, handler)

    # 验证恢复链：
    mock_recreate.assert_awaited_once_with("thread-1")        # 触发重建
    mock_client.threads.update.assert_awaited_once_with(...)  # 元数据被写
    assert proxy.current is backend                           # 代理被热替换
    assert proxy.id == "sb-new"
    payload = json.loads(result.content)
    assert payload["recovery"] == "sandbox_recreated_after_client_error"
```

**这种测试在写之前要先理解："出错时正确的链路应该走哪几步？"** 然后逐步骤断言。

### 6.7 中间件 hook 模式测试

中间件本质是 `(state, runtime) → state | None`，所以测试也按这个 shape 写。`test_ensure_no_empty_msg.py`：

```python
def test_injects_no_op_when_user_not_messaged(self) -> None:
    empty_ai = AIMessage(content="")        # 空 AI 消息（这是中间件要救的场景）
    state = {
        "messages": [
            HumanMessage(content="fix the bug"),
            ToolMessage(content="result", tool_call_id="1", name="bash"),
            empty_ai,
        ]
    }

    result = ensure_no_empty_msg.after_model(state, self._make_runtime())

    assert result is not None                                          # 中间件介入了
    assert len(result["messages"]) == 2                                # 注入了两条消息
    assert result["messages"][0].tool_calls[0]["name"] == "no_op"      # 注入的是 no_op
```

**模板化**：所有中间件测试都长这样——构造一个特定 `state` → 调中间件钩子 → 断言 state diff。`test_notify_step_limit_middleware.py`、`test_refresh_slack_status_middleware.py`、`test_model_fallback_middleware.py` 全是这套。

### 6.8 动态模块加载测试

最罕见但最巧的模式。`test_daytona_integration.py` 在 daytona SDK 未安装时也要能跑：

```python
def _load_daytona_module(monkeypatch):
    # 先伪造 daytona 和 langchain_daytona 这两个 SDK
    fake_daytona = types.ModuleType("daytona")
    fake_daytona.CreateSandboxFromSnapshotParams = _FakeCreateSandboxFromSnapshotParams
    ...
    monkeypatch.setitem(sys.modules, "daytona", fake_daytona)
    monkeypatch.setitem(sys.modules, "langchain_daytona", fake_langchain_daytona)

    # 然后用 importlib 动态加载被测模块（绕过常规 import）
    module_path = ROOT / "agent" / "integrations" / "daytona.py"
    spec = importlib.util.spec_from_file_location("daytona_under_test", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
```

**为什么这么搞**：如果 daytona SDK 不在依赖里，普通 `from agent.integrations.daytona import xxx` 会因 `ImportError` 失败。这套手法**先用 fake module 占住 `sys.modules`**，再让真正的被测模块执行 import 时拿到 fake。

`test_http_security.py` 用了类似招数 stub `exa_py`。

---

## 七、async 测试的几个套路

### 7.1 用 `asyncio.run` 起动同步入口

```python
def test_leave_failure_comment_posts_to_slack_thread(monkeypatch):
    async def fake_post_slack_ephemeral_message(...):
        ...
    monkeypatch.setattr(auth, "post_slack_ephemeral_message", fake_post_slack_ephemeral_message)

    asyncio.run(auth.leave_failure_comment("slack", "auth failed"))
```

测试函数是**同步**的，但被测函数是 async 的——`asyncio.run(coro)` 启一个临时 loop 跑完即扔。

### 7.2 直接写 `async def test_xxx`

```python
async def test_get_github_token_from_thread_skips_expired() -> None:
    fake_client = AsyncMock()
    fake_client.threads.get.return_value = {...}
    with patch.object(github_token, "client", fake_client):
        token, encrypted, expires_at = await github_token.get_github_token_from_thread("tid")
    assert token is None
```

得益于 `asyncio_mode = "auto"`，pytest 自动起 loop。

**两种风格在项目里都有，没有统一**。一般做法：
- 测试只 await 一两次 → 用同步函数 + `asyncio.run`；
- 测试需要多次 await、要操作 AsyncMock → 用 `async def`。

### 7.3 `AsyncMock` 用法速记

```python
mock = AsyncMock(return_value="ok")
await mock("anything")                          # 返回 "ok"
mock.assert_awaited_once_with("anything")       # 注意是 awaited 不是 called

# 抛异常
mock.side_effect = httpx.HTTPStatusError(...)

# Patch 时直接传 class
with patch("agent.x.something", new_callable=AsyncMock) as m:
    m.return_value = ...
```

---

## 八、最重要的一个反模式：**不测 LLM 输出**

项目里**没有任何测试**断言 LLM 返回了"什么内容"。原因：
1. LLM 输出不稳定，断言 == 必然 flaky；
2. **正确的测法是 evals**——见 `CICD_AND_EVALS_CN.md`。

测试覆盖的是**确定性逻辑**：sanitize 参数、解析 diff、签名验签、状态管理、错误恢复。**LLM 行为本身留给 evals**。

这是新人最容易做错的事：写出 `assert result.content == "expected"` 这种断言，运行几次就开始 flaky。

---

## 九、几个特定文件的"为什么这样设计"

### 9.1 `test_github_issue_webhook.py`（1284 行）为什么这么大？

因为 webhook 路径分支多：
- 不同 event_type（`issue_comment`、`pull_request_review_comment`、`push`、`pull_request`...）
- 各自有"是不是 @open-swe"、"sender 是不是 bot"、"repo 在不在白名单"、"是新 issue 还是 follow-up comment" 等子分支
- 每条 happy path + 每条 reject path 都至少一个测试

一份测试基本是"模拟一个 GitHub webhook payload + 验证最终是否触发 run"。

### 9.2 `test_slack_context.py`（754 行）为什么这么大？

Slack 上下文挑选逻辑非常微妙：
- "thread_start"（从头开始抓）
- "last_mention"（从上次 @bot 开始抓）
- 多条 @bot 怎么处理边界
- bot 自己发的消息要排除
- 图片附件要转 multimodal

每种边界都要测。

### 9.3 `test_recent_comments.py`（27 行）为什么这么小？

它就只测了一个特别窄的特性："给定一组 GitHub PR comments 列表，只取最近一次 `@open-swe` 之后的"。是个纯函数，3 个测试就讲完了。

**测试大小是被测代码的复杂度决定的，不是 KPI**。

---

## 十、跑测试的若干姿势

```bash
# 全部
make test                                           # = uv run pytest -vvv tests/

# 单文件
make test TEST_FILE=tests/test_encryption.py

# 单测
uv run pytest -vvv tests/test_encryption.py::TestSingleKeyRoundtrip::test_encrypt_decrypt

# 关键字过滤
uv run pytest -vvv -k "encryption"
uv run pytest -vvv -k "expired and token"          # bool 表达式

# 只跑失败的
uv run pytest --lf                                  # 上次 fail 的

# 第一次失败就停
uv run pytest -x

# 显示打印输出
uv run pytest -s

# 并行（需装 pytest-xdist，目前项目没装）
# uv run pytest -n auto
```

---

## 十一、为新功能写新测试的 checklist

按下面顺序问自己：

1. **被测对象是纯函数吗？** → 模式 1（纯单元测试）。
2. **依赖环境变量吗？** → 加 `monkeypatch.setenv`。
3. **依赖外部 HTTP？** → 用 `monkeypatch.setattr` 替 `httpx.AsyncClient` 或自定义 `_MockHttpxClient`。
4. **依赖 LangGraph SDK / langsmith.Client？** → `with patch("agent.xxx.client") as mock_client: mock_client.threads.get = AsyncMock(...)`。
5. **是中间件？** → 构造 `state = {"messages": [...]}`，调 hook 函数，断言返回的 state diff。
6. **是 webhook？** → 用 `TestClient` + 真实签名 + 把下游业务函数都 monkeypatch 掉。
7. **是错误路径？** → 让 mock 抛对应异常，断言系统优雅退化。
8. **是 LLM 行为？** → ❌ **别在 tests/ 里测**，去写 eval（`evals/reviewer/`）。

写完跑一次 `make lint` 防 ruff 报错，再 `make test TEST_FILE=tests/test_xxx.py` 确认绿。

---

## 十二、整体设计哲学的总结

如果用 5 句话概括 Open SWE 的测试设计：

1. **一个文件对一个模块，文件名见即知。**
2. **纯函数能纯函数测，绝不引入 mock。**
3. **复杂依赖用 patch / setattr 解耦，path 写在"被使用的地方"。**
4. **要么 fake class 模拟协议，要么 AsyncMock 单点验证，二选一别混合。**
5. **LLM 输出不进 tests/，进 evals/。**

理解这五条，可以写出和现有代码同风格的新测试，CI 跑通的概率会高得多。

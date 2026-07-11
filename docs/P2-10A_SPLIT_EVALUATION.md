# P2-10A: 大文件拆分评估报告

> 本阶段只做评估，不修改产品代码。

## 0. 文件概览

| 文件 | 行数 | 函数/类数 | 测试文件 |
|------|------|-----------|----------|
| `daily_report/src/stock_daily_agent/tools.py` | 3148 | 70 函数 + 19 类 | `test_article_url_security.py` (7 tests) |
| `stock_watch_list_back_end.py` | 2992 | 63 函数 | `test_db_init_separation.py` (18 tests), `test_stock_data_post.py` (14 tests), `test_health_check.py` (18 tests), `test_p2_breadth_null_semantics.py` |
| `app_streamlit_multiuser.py` | 2489 | 55 函数 | `test_health_check.py` (部分), `test_login_security.py` |
| `daily_report/jobs.py` | 989 | 40 函数 + 5 类 | `test_email_dedup.py`, `test_queue_capacity.py`, `test_download_rate_limit.py` |

---

## 1. 每个文件的职责列表

### 1.1 `tools.py` (3148 行) — AI Agent 工具集

| 职责集群 | 行范围 | 行数 | 说明 |
|----------|--------|------|------|
| RunContext 管理 | 29-41 | 13 | 全局 `_CONTEXT` 变量 + set/get |
| 技术摘要生成 | 43-100 | 58 | `_technical_summary()` — 从数据字典生成中文摘要 |
| LLM 响应解析 | 103-171 | 69 | `_plain_text_from_llm_response`, `_extract_json_payload`, DashScope 响应解析 |
| DashScope 源记录 | 174-206 | 33 | `_dashscope_source_record`, `_match_dashscope_source_id` |
| 市场推断 | 216-244 | 29 | `infer_market_type`, `infer_search_languages` |
| SearXNG 搜索 | 245-272, 391-432 | 70 | URL 构建 + 结果解析 + 原始搜索 |
| Serper 搜索 | 303-350, 433-468 | 84 | API key/endpoint + 结果解析 + 原始搜索 |
| HTTP 辅助 | 272-302 | 31 | `_http_get_json`, `_http_post_json` |
| 证据处理管线 | 469-842 | 374 | 去重、域名提取、质量评分、分级、标注、重排、ID 分配 |
| 数值验证 | 597-636 | 40 | 禁用 USD/中文"亿"、数值 token 提取、支持率计算 |
| 焦点覆盖评估 | 637-684 | 48 | `_required_focus_coverage`, `_evaluate_evidence_sufficiency` |
| 可验证记录管理 | 830-925 | 96 | JSON 文件加载、URL 规范化、记录收集、note→evidence 匹配 |
| **文章抓取（SSRF 防护）** | **926-1268** | **343** | `_ArticleTextParser`, `ArticleFetchSecurityError`, IP 验证、URL 验证、SSL pinning、重定向验证、文章抓取 |
| 市场查询构建 | 1270-1339 | 70 | `_build_market_queries` |
| 19 个 BaseTool 类 | 1340-3126 | 1787 | 工具定义，每个含 `_run()` 方法 |
| 评分计算引擎 | 2463-2867 | 405 | `_clamp_score` 到 `_compute_final_rating_payload` |
| 工具工厂 | 3127-3148 | 22 | `build_custom_tools()` |

### 1.2 `stock_watch_list_back_end.py` (2992 行) — Flask 后端 + 数据层

| 职责集群 | 行范围 | 行数 | 说明 |
|----------|--------|------|------|
| 模块级常量 | 34-55 | 22 | DB 路径、批次大小、breadth ticker 集合等 |
| SQLite 缓存层 | 57-301 | 245 | Schema、迁移、连接管理、per-user DB 隔离 |
| 缓存初始化封装 | 301-320 | 20 | `init_db()`, `init_global_market_cap_db()` |
| 市场日期 + StockAnalysis 缓存 | 320-465 | 146 | `get_cached_stock_analysis`, beta 缓存 |
| Ticker 名称缓存 | 466-572 | 107 | `get_cached_ticker_names` |
| 市值缓存 | 573-661 | 89 | `get_cached_market_caps` |
| **盘前盘后交易逻辑** | **662-1026** | **365** | 时间判断、价格过滤、缓存更新、overlay 应用 |
| 价格缓存操作 | 1027-1245 | 219 | save/load, `get_prices_with_cache` |
| 健康检查 | 1246-1288 | 43 | `health_check`, deprecated 端点标记 |
| SP500/Nasdaq100 管理 | 1289-1782 | 494 | 符号缓存、成分股缓存、元数据获取 |
| **市场宽度 + Treemap** | **1783-2037** | **255** | chip 分布、treemap 数据、breadth 计算 |
| **`get_stock_data()` 主函数** | **2038-2577** | **540** | 请求解析、数据获取、技术指标计算、响应组装 |
| 其他 API 路由 | 2578-2992 | 415 | breadth_data, kline_data, fear_greed, fear_greed_crypto, breadth_data_trail |

### 1.3 `app_streamlit_multiuser.py` (2489 行) — Streamlit 前端

| 职责集群 | 行范围 | 行数 | 说明 |
|----------|--------|------|------|
| 页面配置 + 常量 | 1-177 | 177 | COLUMNS, SECTION_META, 货币映射, THEMES |
| 后端健康检查 | 179-240 | 62 | `check_backend_health`, `ensure_backend` |
| CSS 注入 | 241-469 | 229 | `inject_css` — 大量内联 CSS |
| 表格渲染 | 470-835 | 366 | 分组、着色、渲染 |
| **货币转换** | **836-996** | **161** | ticker 货币推断、EUR 转换、DF/Kline 转换 |
| 数据获取封装 | 997-1067 | 71 | fetch_stock_data, fetch_breadth_data 等 |
| **图表构建** | **1068-1730** | **663** | breadth chart, treemap, fear&greed gauge, kline chart |
| 认证面板 | 1731-1776 | 46 | `render_auth_panel` |
| 配置管理 | 1776-1836 | 61 | get/save active config, page editor |
| Watchlist 渲染 | 1836-1976 | 141 | `render_section`, `render_kline` |
| **报告表单 + 作业状态** | **1977-2389** | **413** | 报告表单、状态表、邮件作业、周计划、日报 |
| 主页面流程 | 2390-2489 | 100 | 顶层 Streamlit 脚本执行 |

### 1.4 `daily_report/jobs.py` (989 行) — 作业队列 + 定时计划

| 职责集群 | 行范围 | 行数 | 说明 |
|----------|--------|------|------|
| 异常类 | 28-47 | 20 | 5 个异常类 |
| 时间 + 配置辅助 | 49-76 | 28 | `_now`, `_iso`, 限制读取 |
| SQLite 连接 | 78-102 | 25 | `_connect`, `_connection` |
| Schema 初始化 | 104-214 | 111 | `init_job_db` — 3 张表 + 迁移 |
| 邮件验证 | 215-239 | 25 | `validate_email`, `mask_email` |
| 作业队列操作 | 240-604 | 365 | 入队、查询、恢复、claim、存储、标记 |
| 周计划 CRUD | 605-874 | 270 | 创建、查询、激活/停用、删除、到期物化 |
| 下载生成管理 | 875-963 | 89 | 清理、限制、开始、完成 |

---

## 2. 高耦合全局状态

### 2.1 `tools.py`
- **`_CONTEXT: RunContext | None`** (行 29) — 全局变量，通过 `set_context()` 设置。所有 BaseTool 类通过 `get_context()` 访问。这是唯一的全局状态，但它是整个模块的中心枢纽。
- **影响**：拆分时，任何需要访问 `ctx` 的子模块必须通过参数传递或 import `get_context`。

### 2.2 `stock_watch_list_back_end.py`
- **`CURRENT_DB_PATH: ContextVar`** (行 43) — Flask 请求上下文绑定的 DB 路径
- **`_DB_SCHEMA_INITIALIZED: set`** (行 87) — 进程级 schema 初始化去重集合
- **`_DB_INIT_LOCK: threading.Lock`** (行 88) — schema 初始化锁
- **`DB_PATH`** (行 35) — 全局默认 DB 路径
- **影响**：这些状态被 DB 操作、Flask 路由、缓存函数深度共享。拆分 DB 层需要传递这些状态或建立新的模块级单例，风险高。

### 2.3 `app_streamlit_multiuser.py`
- **`_DEV_MODE`** (行 175), **`_backend_ready`** (行 176) — 模块级布尔值
- **`st.session_state`** — Streamlit 会话状态，被大量函数隐式访问
- **影响**：Streamlit 的执行模型（top-to-bottom 脚本）使得拆分后模块间的状态共享变得复杂。`st.session_state` 是隐式全局状态，无法通过参数传递轻松解耦。

### 2.4 `daily_report/jobs.py`
- **`_INIT_LOCK`** (行 24), **`_INITIALIZED_DATABASES`** (行 25) — schema 初始化去重
- **影响**：耦合度低。这些状态仅在 `init_job_db()` 中使用，其余函数都通过 `_connection()` 上下文管理器获取新连接。

---

## 3. 可独立提取且已有测试覆盖的模块

### ✅ `tools.py` → 文章抓取子模块

**函数列表** (行 926-1268, ~343 行):
- `class _ArticleTextParser(HTMLParser)` — HTML 文本提取
- `class ArticleFetchSecurityError(ToolError)` — 安全异常
- `_ARTICLE_REDIRECT_STATUSES`, `_MAX_ARTICLE_RESPONSE_BYTES` — 常量
- `_article_fetch_int_env()` — 环境变量读取
- `_article_fetch_allowed_ports()` — 端口白名单
- `_is_public_ip()` — 公网 IP 验证
- `_validate_article_url()` — URL 验证 + DNS 解析 + IP 验证
- `_article_request_target()` — 请求目标地址
- `_article_host_header()` — Host 头
- `class _PinnedHTTPSConnection` — SSL pinning 连接
- `_open_pinned_article_request()` — 打开 pinning 请求
- `_read_article_response()` — 读取响应（带大小限制）
- `_decode_article_response()` — 解码响应
- `_fetch_article_text()` — 主入口，抓取文章全文
- `_enrich_evidence_with_articles()` — 批量抓取并丰富证据

**已有测试**: `tests/test_article_url_security.py` — 7 个测试，覆盖 IP 验证、URL 验证、DNS 重绑定防护、重定向验证、响应大小限制。

**依赖**: 仅使用标准库 (`http.client`, `ssl`, `ipaddress`, `socket`, `html.parser`, `urllib.parse`)。唯一的外部依赖是 `ToolError`（从 `.utils` 导入），可改为参数传递或重新导入。

**调用方**: 
- `FetchArticleTextTool._run()` (行 2089-2135) — 调用 `_fetch_article_text()`
- `_enrich_evidence_with_articles()` (行 1220-1269) — 被 `_prepare_evidence_from_raw()` 调用
- `_validate_article_url()` 被 `test_article_url_security.py` 直接测试

---

## 4. 暂时不应拆分的区域

### ❌ `stock_watch_list_back_end.py` — 不建议拆分

**理由**:
1. **Flask 路由与业务逻辑深度耦合** — `get_stock_data()` (540 行) 是核心 API 路由，同时调用 DB 操作、yfinance 下载、技术指标计算、缓存管理。拆分任何一个子模块都需要穿透这个函数。
2. **DB 层与 Flask 请求上下文绑定** — `set_request_cache_db()` 使用 `request.values` 和 `request.get_json()`，`CURRENT_DB_PATH` 是 ContextVar 绑定到 Flask 请求。提取 DB 层需要重构请求上下文管理。
3. **盘前盘后逻辑被多处调用** — `apply_extended_hours_to_daily_kline()` 被 `get_kline_data()` 调用，`get_latest_extended_hours_price()` 被 `get_stock_data()` 调用，`update_extended_hours_price_cache()` 也被 `get_stock_data()` 调用。这些函数之间互相调用（如 `_filter_extended_session` → `_extended_effective_time_for_bar` → `_extended_label_for_effective_time`），形成紧密集群。
4. **SP500/Nasdaq100 管理虽独立但收益有限** — ~494 行可提取，但提取后仍需在 backend 中导入大量函数，接口面太大（~20 个公共函数）。
5. **拆分 `get_stock_data()` 风险极高** — 该函数有 540 行，但每一步都依赖前一步的局部变量。提取为独立函数需要传递大量中间状态。

### ❌ `app_streamlit_multiuser.py` — 不建议拆分

**理由**:
1. **Streamlit 执行模型** — 脚本 top-to-bottom 执行，主流程（行 2390-2489）直接调用上方定义的函数。拆分后子模块需要被主脚本导入，但 Streamlit 的 `st.session_state` 和 `st.columns`/`st.tabs` 等上下文管理器无法跨模块传递。
2. **CSS/主题常量被全局使用** — `THEMES` 字典被 `inject_css()`、`build_breadth_chart()`、`build_kline_chart()` 等多个函数引用。提取图表模块需要同时提取主题。
3. **图表构建函数与 Streamlit API 耦合** — `build_breadth_chart()` 等函数返回 Plotly Figure，但调用方直接使用 `st.plotly_chart()`。虽然图表构建本身可以提取，但收益有限（~663 行），且需要传递 `dark_mode`、`THEMES` 等参数。
4. **货币转换函数被表格和图表共享** — `convert_stock_df_for_display()` 被主流程调用，`convert_kline_data_for_display()` 被 `render_kline()` 调用，`get_ticker_currency()` 被表格渲染调用。提取货币模块需要被至少 3 个其他模块导入。
5. **报告表单与 jobs.py/mailer.py/service.py 已解耦** — 这部分虽然 413 行，但已经通过 import 实现了模块化，无需进一步拆分。

### ❌ `daily_report/jobs.py` — 不建议拆分

**理由**:
1. **行数适中** (989 行) — 在单一职责模块的合理范围内。
2. **高度内聚** — 所有函数都围绕同一 SQLite 数据库的 3 张表操作。作业队列和定时计划共享连接管理、schema 初始化、异常体系。
3. **下载生成管理** (89 行) 虽可独立，但太小不值得拆分。
4. **测试覆盖完善** — 3 个测试文件覆盖了所有主要函数。

---

## 5. 拆分收益和回归风险

### 唯一推荐拆分: `tools.py` → `article_fetcher.py`

| 维度 | 评估 |
|------|------|
| **行数减少** | tools.py: 3148 → ~2805 (-343 行, -11%) |
| **职责清晰度** | 显著提升 — 文章抓取（含 SSRF 防护）是一个完整的安全关键子系统 |
| **测试覆盖** | 已有 7 个专门测试 (`test_article_url_security.py`) |
| **依赖外部性** | 零 — 仅使用 Python 标准库 + `ToolError` 异常 |
| **接口面** | 小 — 仅 2 个调用点在 tools.py 内部 |
| **回归风险** | 低 — 所有函数为 `_` 前缀内部函数，外部不直接调用 |
| **安全审计便利性** | 显著提升 — SSRF 防护代码集中在一个文件，便于审查 |

---

## 6. 建议执行的拆分 (最多 1 个)

### 拆分方案: 提取 `article_fetcher.py`

**从** `daily_report/src/stock_daily_agent/tools.py`
**到** `daily_report/src/stock_daily_agent/article_fetcher.py`

**移动内容** (行 926-1268):
```
class _ArticleTextParser(HTMLParser)
class ArticleFetchSecurityError(ToolError)
_ARTICLE_REDIRECT_STATUSES
_MAX_ARTICLE_RESPONSE_BYTES
_article_fetch_int_env()
_article_fetch_allowed_ports()
_is_public_ip()
_validate_article_url()
_article_request_target()
_article_host_header()
class _PinnedHTTPSConnection
_open_pinned_article_request()
_read_article_response()
_decode_article_response()
_fetch_article_text()
_enrich_evidence_with_articles()
```

**保留在 tools.py 中的内容**: 所有其他函数和类不变。

---

## 7. 兼容接口和测试计划

### 7.1 兼容接口

**新文件** `article_fetcher.py`:
```python
# 从 .utils 导入 ToolError（保持异常继承关系）
from .utils import ToolError

# 导出以下公共接口:
class ArticleFetchSecurityError(ToolError): ...
def _fetch_article_text(url, timeout=12, max_chars=5000) -> dict: ...
def _validate_article_url(url) -> tuple: ...
def _enrich_evidence_with_articles(items, max_urls, max_chars, timeout) -> tuple: ...
def _is_public_ip(address) -> bool: ...
# 以及所有 _article_* 辅助函数和 _ArticleTextParser
```

**tools.py 中的修改**:
```python
# 在文件顶部添加:
from .article_fetcher import (
    ArticleFetchSecurityError,
    _fetch_article_text,
    _validate_article_url,
    _enrich_evidence_with_articles,
    _is_public_ip,  # 如果 test_article_url_security.py 直接 import
)
```

**对外部调用方的影响**: 零。
- `agent_runner.py` 只导入 `set_context` 和 `build_custom_tools`，不直接访问文章抓取函数。
- `test_article_url_security.py` 当前从 `tools` 模块导入函数 — 拆分后需要更新导入路径为 `article_fetcher`。

### 7.2 测试计划

1. **更新现有测试导入路径**:
   - `test_article_url_security.py` 中的 `from stock_daily_agent.tools import ...` 改为 `from stock_daily_agent.article_fetcher import ...`
   - 涉及的导入: `_validate_article_url`, `_is_public_ip`, `ArticleFetchSecurityError`, `_PinnedHTTPSConnection`, `_article_request_target`, `_article_host_header` 等

2. **添加拆分验证测试** (新文件 `tests/test_p2_article_fetcher_split.py`):
   ```
   - test_article_fetcher_module_exists: 确认 article_fetcher.py 存在且可导入
   - test_article_fetcher_exports_security_error: ArticleFetchSecurityError 可从 article_fetcher 导入
   - test_article_fetcher_exports_fetch_function: _fetch_article_text 可从 article_fetcher 导入
   - test_tools_still_imports_article_functions: tools.py 仍可通过自身导入使用这些函数
   - test_no_duplicate_definitions: _fetch_article_text 不在 tools.py 中重复定义
   - test_article_fetcher_only_stdlib: article_fetcher.py 不导入第三方包（qwen_agent 等）
   - test_existing_article_security_tests_pass: 原有 7 个安全测试全部通过
   ```

3. **全套回归测试**:
   ```
   pytest tests/ -v
   ```
   预期: 550+ passed, 3 skipped, ≤1 pre-existing failure

4. **导入链验证**:
   ```python
   # 验证 agent_runner.py 的导入不受影响
   from stock_daily_agent.agent_runner import *  # 应无报错
   # 验证 build_custom_tools() 返回 19 个工具
   from stock_daily_agent.tools import build_custom_tools
   assert len(build_custom_tools()) == 19
   ```

---

## 8. 总结

| 文件 | 行数 | 建议 | 理由 |
|------|------|------|------|
| `tools.py` | 3148 | **拆分 1 个子模块** | 文章抓取子系统独立、有测试覆盖、低风险 |
| `stock_watch_list_back_end.py` | 2992 | **不拆分** | Flask 路由与业务逻辑深度耦合，拆分风险高 |
| `app_streamlit_multiuser.py` | 2489 | **不拆分** | Streamlit 执行模型限制，状态耦合深 |
| `daily_report/jobs.py` | 989 | **不拆分** | 行数适中，高度内聚 |

**核心原则**: 不为了减少行数而拆分。只拆分有清晰边界、已有测试覆盖、低回归风险的子模块。

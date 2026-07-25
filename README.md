# Stock Watch List

[中文](#中文) · [English](#english)

> Research and monitoring software only. It is not investment advice. Market-data providers, search providers, model providers, and SMTP services can be delayed, rate-limited, unavailable, or change their responses.

## 中文

### 项目简介

Stock Watch List 是一个可部署的股票与跨市场监控应用，而不只是单页 AI 演示。项目提供 Tkinter 桌面端、单用户 Streamlit 和带账号隔离的多用户 Streamlit；后者通过 Flask 数据 API、SQLite 缓存和独立报告 Worker 支持 AI 股票日报、AI 组合报告与邮件计划任务。

### 主要功能

#### Watchlists、市场与图表

- 可编辑的股票 Watchlist、Market Dashboard 和 Portfolio 页面；支持按组管理标的、Yahoo Finance 搜索添加标的、暗色模式和本币/EUR 显示。
- 日线市场数据：价格、最近 20 根日线 K 线蜡烛图、`1D%` / `5D%` / `1M%` / `YTD%`、RSI、成交量比、EMA 偏离、布林带、相对 `^GSPC` 的收益/动量、估值、分析师评级、目标价、市值和 Beta。
- 顶部市场情绪卡片：CNN Fear & Greed、VIX 和 Crypto Fear & Greed。
- Market Breadth：S&P 500 与 Nasdaq 100 的 20/50/200 日均线上方比例、历史图和 treemap。Breadth 只在手动点击 **Refresh Breadth** 时重新下载与计算。
- K 线图：周期与 SMA/EMA 可配；支持 VWAP、MACD、RSI、KDJ、布林带、成交量、60 天筹码峰、Auto Fibonacci Retracement 与 Extension。
- 多用户版的 K 线自动刷新会恢复同一浏览器、同一 ticker/周期/间隔/币种下的 zoom/pan 与 Plotly 图例隐藏/显示偏好。
- 即使 ticker、周期与间隔未变，手动点击 **Plot** 也会强制重新拉取 K 线数据。

#### Short-term Watchlist（多用户登录后）

Short-term Watchlist 位于 **Market Dashboard** 与 **Market Breadth** 之间，仅对登录用户开放。每个标的在同一张表中占两行，分别显示 5m 与 15m 信号，便于直接对照：

- Price、`1D%`（最新价相对上一交易日复权收盘价）、`Bar Diff%`、15 根 K 线蜡烛图。
- 两条可配置的 SMA/EMA、以万分之一符号 `‱` 显示的 MA Spread、均线对比图。
- Volume Ratio、近 15 根成交量柱图、以万分之一符号 `‱` 显示的 MACD Diff、MACD/Signal 图（含 0 轴虚线）。
- 距离布林上轨、Close/布林带上下轨图（上下轨为虚线）、VWAP 距离、Close/VWAP 图（含 VWAP ±1σ 虚线带）、RSI 与 RSI 30/70 参考线图、可配置周期的 ATR 及其趋势图。
- 标的列沿用主 Watchlist 的 Beta 着色；价格、涨跌与指标列采用对应的市场状态或数值着色规则。
- 组、标的、指标参数和 10/20/30 秒自动刷新偏好按账号保存。自动刷新独立于侧边栏的 **Auto-refresh stocks**。
- 可选短线浏览器音频提醒：可分别监听 5m/15m 的 MACD、两条可配置 MA、Close 与布林带上下轨、Close 与 VWAP、Close 与 VWAP ±1σ 带的交叉，以及 RSI 穿越 30/70；每类指标均可独立启用并设置阈值，也可按 ticker 启用/关闭。需开启短线自动刷新，并在当前浏览器点击 **Enable sound & test** 授权声音。每次新提醒会循环短音并闪烁对应指标单元格，持续时间可设为 5/10/15/30/60 秒（默认 15 秒）。提醒偏好按账号保存，已触发提醒仅在当前浏览器会话去重。
- 主 Watchlist、Market Dashboard 与 Portfolios & Reports 的表格会在窄列中自动换行表头；将鼠标悬停在任一表头上可查看完整列名。

默认情况下它请求最近 2 天的 5m/15m K 线。所需历史窗口会按当前最大指标周期自动增加；例如更长的 MA/BB/RSI/MACD 设置会请求更多天数。短线 K 线只保存在当前 Streamlit 会话内，自动刷新会覆盖该会话数据，不写入账户数据库。

#### Portfolios & AI Portfolio Reports（多用户登录后）

- 每个用户可创建多个 Portfolio 页面，保存组、ticker、买入价、股数和买入币种。
- 显示持仓市值、绝对/百分比盈亏、期间盈亏、汇率换算、组合汇总、Beta 和持仓 treemap。
- **AI Portfolio Report** 是确定性工作流：Python 先计算组合权重、集中度、Beta、波动率、回撤、风险贡献和技术快照，再以受控的一次模型/搜索调用生成 HTML 报告。它不是由 Agent 自主决定工具调用的流程。
- 支持浏览器下载、一次性邮件和每周邮件计划。报告 Worker 在后台运行，页面关闭后任务仍可继续。

#### AI Stock Reports（多用户登录后）

- 为单个 ticker 生成可下载的 HTML 股票日报，包含行情、技术指标、新闻/搜索证据、评分、风险、评级和图表。
- 日报由基于 Qwen-Agent 的定制 Agent 执行。Agent 会通过 function calling 调用受限工具获取市场数据、搜索与证据；Python 负责最终评分、风险/评级计算、技术图、HTML、证据、图标和运行日志的确定性产出。
- 支持页面生成、一次性邮件和每周计划。搜索与文章抓取使用输入验证和 SSRF 防护。

### 架构

```text
Tkinter / Single-user Streamlit / Multi-user Streamlit
                         |
                         v
              Flask data API + SQLite caches
                         |
       yfinance / StockAnalysis / FX / breadth sources

Multi-user Streamlit (authenticated users)
   |-- account-scoped watchlists, portfolios, chart settings
   |-- Short-term Watchlist (5m / 15m session data)
   |-- synchronous report download
   `-- persisted email jobs and schedules
                         |
                         v
              Report Worker -> SMTP / LLM / Search
```

关键文件：

```text
app_tkinter.py                     Tkinter 桌面端
app_streamlit.py                   单用户 Streamlit
app_streamlit_multiuser.py         多用户 Streamlit、Portfolio、短线表与报告 UI
stock_watch_list_back_end.py       Flask API、SQLite 市场数据缓存
multiuser_store.py                 账号、密码哈希和每用户配置
short_term_watchlist.py            短线指标、VWAP 与内嵌 SVG sparkline
kline_indicators.py                可配置 K 线指标
kline_fibonacci.py                 Auto Fibonacci 计算与覆盖层
daily_report/                      股票日报、组合报告、任务队列、邮件与 Worker
portfolio_analysis/                组合快照、风险指标与建议验证
deploy/                            Worker 部署辅助文件
tests/                             pytest 测试
```

### 安装

要求：Python 3.10+。在项目根目录创建虚拟环境。

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
```

macOS / Linux：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -r requirements-dev.txt
cp .env.example .env
```

不要提交真实 `.env`、SQLite 数据库、`daily_report/runs/`、`outputs/`、`user_data/` 或日志。

### 配置

所有进程通过 `config_loader.load_project_env()` 加载配置。优先级从高到低为：现有进程环境变量、显式 env 文件、项目根目录 `.env`、代码默认值；除非显式要求覆盖，`.env` 不会覆盖已存在的进程变量。

常用 `.env` 字段：

```dotenv
# Frontend / backend
STOCK_API_BASE_URL=http://127.0.0.1:5000
STOCK_DEV_MODE=1                 # 仅本地开发；生产环境应为 0

# LLM for AI Stock Reports
LLM_PROVIDER=dashscope           # auto / dashscope / deepseek / openai_compatible
QWEN_MODEL=deepseek-v4-flash
QWEN_RESEARCH_MODEL=deepseek-v4-flash
DASHSCOPE_API_KEY=your_key
QWEN_AGENT_USE_RAW_API=true

# Search / article evidence
SEARCH_PROVIDER=both             # auto / priority / searxng / serper / both
SERPER_API_KEY=your_key
ARTICLE_FETCH_ENABLED=true

# SMTP (needed for email reports)
REPORT_SMTP_HOST=smtp.example.com
REPORT_SMTP_PORT=465
REPORT_SMTP_USE_SSL=true
REPORT_SMTP_USER=you@example.com
REPORT_SMTP_FROM=you@example.com
REPORT_SMTP_AUTH_CODE=your_smtp_authorization_code

# Portfolio AI Report model selection
PORTFOLIO_REPORT_PROVIDER=dashscope
PORTFOLIO_REPORT_MODEL=deepseek-v4-pro
PORTFOLIO_ENABLE_THINKING=true
PORTFOLIO_REASONING_EFFORT=high
```

模型名称、可用 provider 和费用由外部服务商决定；请使用自己账号当前可用的模型与 API Key。完整字段及安全说明见 [`.env.example`](.env.example)。

### 本地运行

推荐分别启动 Flask、需要的前端和邮件 Worker。

```bash
# Terminal 1: Flask API
python stock_watch_list_back_end.py

# Terminal 2: choose one frontend
streamlit run app_streamlit.py
# or
streamlit run app_streamlit_multiuser.py

# Optional desktop client
python app_tkinter.py

# Terminal 3: required for queued email jobs and weekly schedules
python -m daily_report.worker
```

健康检查：

```bash
curl -fsS http://127.0.0.1:5000/api/health
```

PowerShell 中请使用：

```powershell
Invoke-RestMethod http://127.0.0.1:5000/api/health
```

### 生产部署要点

- 生产环境使用独立 Flask 服务，设置 `STOCK_DEV_MODE=0`，并让 Streamlit 通过 `STOCK_API_BASE_URL` 访问它。
- 将 Streamlit、Flask API 和 `daily_report.worker` 作为独立 systemd 服务运行；Worker 不应以 root 运行。
- 先备份服务器 `.env`，再通过安全通道覆盖；权限建议为 `600`。
- 更新代码后安装 requirements、执行编译检查，再重启服务。服务名取决于你的部署；示例系统可使用 `stock-watchlist-api.service`、`stock-watchlist.service` 和 `stock-watchlist-report-worker.service`。

```bash
cd /opt/Stock_watch_list
git pull --ff-only origin main
./venv/bin/python -m pip install -r requirements.txt
./venv/bin/python -m py_compile app_streamlit_multiuser.py stock_watch_list_back_end.py
sudo systemctl restart stock-watchlist-api.service stock-watchlist.service stock-watchlist-report-worker.service
```

### 数据、缓存与刷新

- `/api/stock_data` 使用 SQLite `price_cache` 保存日线 `adj_close` 与成交量；历史保留期由后端清理策略控制。
- 主 Watchlist 数据在 Streamlit 侧有短期缓存；**Refresh Stocks**、全局自动刷新或缓存到期后会更新。
- Short-term Watchlist 的 5m/15m 数据调用 `/api/kline_data`，按 ticker、interval 和所需历史窗口在当前会话暂存。它不会每 10/20/30 秒重新下载上一交易日 `Adj Close`。
- Short-term `1D%` 由短线最新 Close 与共享日线缓存中的上一交易日复权收盘价计算；ticker 的 Beta 着色也复用共享日线数据。
- Market Breadth 不受自动刷新影响，只在手动刷新时重新计算。

### 安全与使用边界

- 真实 `.env`、密钥、SMTP 授权码、密码、数据库和报告输出不得提交。
- 登录密码采用哈希保存；用户 Watchlist、组合、短线参数和计划任务按账号隔离。
- 报告文章抓取拒绝 loopback、私有、link-local、multicast、reserved 与云元数据地址，并校验重定向目的地。
- 普通用户界面不应暴露完整子进程日志、API Key 或完整收件人地址。
- 报告内容和模型输出仅供研究；请自行核验来源、价格和风险。

### 测试

```bash
python -m py_compile app_streamlit_multiuser.py stock_watch_list_back_end.py short_term_watchlist.py
python -m pytest -q
```

Windows 终端若默认代码页导致 Python 读取 UTF-8 源文件失败，可使用：

```powershell
$env:PYTHONUTF8 = "1"
python -m pytest -q
```

---

## English

### Overview

Stock Watch List is a deployable market-monitoring and AI-reporting application, not a single-page AI demo. It includes a Tkinter desktop client, a single-user Streamlit app, and an account-isolated multi-user Streamlit app backed by a Flask data API, SQLite caches, and an independent report worker.

### Capabilities

#### Watchlists, market dashboard, and charts

- Editable grouped watchlists, market dashboards, and portfolio pages with Yahoo Finance ticker search, dark mode, and local/EUR display.
- Daily-market metrics including price, a 20-bar daily candlestick SVG, 1D/5D/1M/YTD returns, RSI, volume ratio, EMA and Bollinger distances, relative returns/momentum versus `^GSPC`, valuation, analyst data, targets, market cap, and beta.
- CNN Fear & Greed, VIX, and Crypto Fear & Greed cards; on-demand S&P 500/Nasdaq 100 market-breadth charts and treemaps.
- Configurable K-line charts with SMA/EMA, VWAP, MACD, RSI, KDJ, Bollinger Bands, volume, chip peak, and Auto Fibonacci retracement/extension.
- In the multi-user app, K-line auto-refresh restores browser-local zoom/pan and Plotly legend visibility for the same ticker, period, interval, and currency.
- Clicking **Plot** always refetches K-line data, even when the ticker, period, and interval are unchanged.

#### Short-term Watchlist (authenticated multi-user app)

The Short-term Watchlist sits between **Market Dashboard** and **Market Breadth**. Each ticker has adjacent 5m and 15m rows in one comparison table. It includes:

- Price, 1D% versus the prior trading day's adjusted close, bar change, and a 15-candle SVG.
- Two configurable SMA/EMA lines, MA spread in per-ten-thousand units (`‱`), and an MA comparison sparkline.
- Volume ratio, a 15-bar volume sparkline, MACD Diff in per-ten-thousand units (`‱`), and MACD/signal with a dashed zero reference.
- Bollinger-upper distance, a close/Bollinger upper-lower sparkline with dashed bands, VWAP distance, a close/VWAP sparkline with VWAP ±1σ dashed bands, RSI and RSI 30/70 references, plus configurable-period ATR and its trend sparkline.
- Main-watchlist beta coloring for ticker cells and matching numeric/status color semantics.
- Account-scoped groups, ticker search/add, indicator parameters, and independent 10/20/30-second refresh.
- Optional short-term browser audio alerts for near and confirmed MACD, configurable-MA, close/Bollinger upper-lower, close/VWAP, close/VWAP ±1σ-band, and RSI 30/70 crossovers. Each signal has its own enable switch and threshold, and alerts can be enabled per ticker. They require short-term auto-refresh plus a one-time **Enable sound & test** browser gesture. Each new alert repeats short tones and flashes its relevant table cell for a configurable 5/10/15/30/60 seconds (15 by default); preferences are account-scoped and de-duplication is browser-session-only.
- In the main Watchlist, Market Dashboard, and Portfolios & Reports tables, narrow headers wrap automatically; hover any header to see its full column title.

The default history request is two days. The request window scales automatically when the active MA, MACD, Bollinger, or RSI periods require more history. Intraday payloads are session-only; they are overwritten on refresh and are not stored in the account database.

#### Portfolios and AI Portfolio Reports

- Multiple per-account portfolio pages with holdings, cost basis, shares, currencies, current value, P/L, period P/L, FX conversion, beta, and treemaps.
- The AI Portfolio Report is a deterministic workflow: Python computes weights, concentration, beta, volatility, drawdown, risk contribution, and technical snapshots before a controlled model/search call produces a self-contained HTML report.
- Download, one-off email, and weekly schedules are supported. Queued work continues in the worker after the browser closes.

#### AI Stock Reports

- Generates downloadable HTML reports for individual tickers with market data, technicals, search/news evidence, scoring, risk, rating, and charts.
- Uses a customized Qwen-Agent workflow. The agent uses function calling for approved data/search/evidence tools; Python deterministically creates the final scoring, risk/rating, charts, HTML, evidence assets, and logs.
- Supports browser generation, one-off email, and weekly schedules. Article fetching includes SSRF protections.

### Architecture

```text
Tkinter / single-user Streamlit / multi-user Streamlit
                          |
                          v
                Flask data API + SQLite caches
                          |
          yfinance / StockAnalysis / FX / breadth sources

Authenticated multi-user Streamlit
   |-- per-account configuration and chart settings
   |-- short-term 5m/15m monitoring
   |-- report downloads
   `-- persisted email jobs and schedules
                          |
                          v
                Report Worker -> SMTP / LLM / Search
```

### Installation

Requirements: Python 3.10+.

```bash
python3 -m venv .venv
source .venv/bin/activate            # Windows: .\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -r requirements-dev.txt
cp .env.example .env                 # Windows PowerShell: Copy-Item .env.example .env
```

Never commit a real `.env`, SQLite databases, report outputs, user data, or logs.

### Configuration

All processes use `config_loader.load_project_env()`. Priority is: existing process environment, explicit env file, project-root `.env`, then code defaults. A `.env` file does not overwrite existing process variables unless explicitly requested.

Important settings include:

- `STOCK_API_BASE_URL` and `STOCK_DEV_MODE` for the frontend/API boundary.
- `LLM_PROVIDER`, `QWEN_MODEL`, `QWEN_RESEARCH_MODEL`, provider API keys, and `QWEN_AGENT_USE_RAW_API` for AI Stock Reports.
- `SEARCH_PROVIDER`, `SERPER_API_KEY`, `SEARXNG_*`, and `ARTICLE_FETCH_*` for evidence gathering.
- `REPORT_SMTP_*`, `REPORT_*`, and `PORTFOLIO_*` for email, queues, schedules, limits, and Portfolio AI Reports.

See [`.env.example`](.env.example) for the complete template. Use model names and API keys available to your own provider account.

### Run locally

```bash
# Terminal 1
python stock_watch_list_back_end.py

# Terminal 2: choose a frontend
streamlit run app_streamlit.py
# or
streamlit run app_streamlit_multiuser.py

# Optional desktop client
python app_tkinter.py

# Terminal 3: required for queued email reports and schedules
python -m daily_report.worker
```

Check the API with `curl -fsS http://127.0.0.1:5000/api/health`.

### Deployment, caching, and security

- Run Flask, Streamlit, and the report worker as separate services in production. Set `STOCK_DEV_MODE=0`; do not run an internet-facing worker as root.
- Daily adjusted close and volume data are retained in SQLite `price_cache`. The Streamlit client also keeps a short-lived shared market-data cache.
- Short-term refresh re-downloads only 5m/15m K-lines. Its 1D% prior-close basis and ticker beta come from the shared daily data, not from each 10/20/30-second refresh.
- The article fetcher validates URLs and redirects, rejecting loopback, private, link-local, multicast, reserved, and cloud-metadata destinations.
- Treat all market, web, and model output as untrusted research input.

Example production update:

```bash
cd /opt/Stock_watch_list
git pull --ff-only origin main
./venv/bin/python -m pip install -r requirements.txt
./venv/bin/python -m py_compile app_streamlit_multiuser.py stock_watch_list_back_end.py
sudo systemctl restart stock-watchlist-api.service stock-watchlist.service stock-watchlist-report-worker.service
```

### Tests

```bash
python -m py_compile app_streamlit_multiuser.py stock_watch_list_back_end.py short_term_watchlist.py
python -m pytest -q
```

On Windows, if the default code page causes UTF-8 source-reading failures:

```powershell
$env:PYTHONUTF8 = "1"
python -m pytest -q
```

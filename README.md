# Stock Watch List

[中文](#中文) · [English](#english)

> Research and monitoring software only. It is not investment advice. Market-data providers, search providers, model providers, and SMTP services can be delayed, rate-limited, unavailable, or change their responses.

## 中文

### 项目简介

Stock Watch List 是一个可部署的股票与跨市场监控应用，而不只是单页 AI 演示。项目提供 Tkinter 桌面端、单用户 Streamlit 和带账号隔离的多用户 Streamlit；后者通过 Flask 数据 API、SQLite 缓存和独立报告 Worker 支持 AI 股票日报、A 股/美股大盘复盘、AI 组合报告与邮件计划任务。

### 主要功能

#### Watchlists、市场与图表

- 可编辑的股票 Watchlist、Market Dashboard 和 Portfolio 页面；支持按组管理标的、Yahoo Finance 搜索添加标的、暗色模式和本币/EUR 显示。Ticker 列会完整显示长代码；悬停 Ticker 可查看标的名称及 Beta，悬停 Price 可查看该价格对应的交易日期，盘前/盘后估算价还会显示美国东部时间。
- 日线市场数据：价格、最近 20 根日线 K 线蜡烛图、`1D%` / `5D%` / `1M%` / `YTD%`、RSI、成交量比、EMA 偏离、布林带、相对 `^GSPC` 的收益/动量、估值、分析师评级、目标价、市值和 Beta；相对动量列可展开为各自最近 20 根日线的内嵌趋势曲线，灰色虚线为 0 轴。
- 顶部市场情绪卡片：CNN Fear & Greed、VIX 和 Crypto Fear & Greed。
- **Market Breadth & Screener**：S&P 500 与 Nasdaq 100 的 20/50/200 日均线上方比例、历史图和 treemap；登录用户还可在同一次 **Refresh Breadth & Screen** 中筛选“指数成分股 + 自己全部 Watchlist/Portfolio/Short-term 标的”的并集。选股会过滤 ETF、指数、期货、汇率和加密资产，并仅允许已验证的个股进入趋势质量、放量突破和超跌反转三套规则策略。评分按市场原币种处理：A 股使用沪指、港股使用恒指等本地基准；流动性采用 20 日平均成交额，不会把美元门槛直接用于非美元标的。
- Screener 复用 Breadth 的两年日线 SQLite 缓存：三套策略不重复下载行情；以动量、估值、流动性、活跃度、稳定性等透明因子构成基础分，再单列风险扣分。PE/PB/市值优先读取当日 SQLite 基本面缓存；为避免每次刷新全量爬取，最多只补全三套技术初筛候选的 75 个标的。结果按账户保存完整规则快照（90 天、每策略最多 100 次）。每个策略可手动对规则前 15 名执行一次无工具的 AI 复排；默认继承 `LLM_PROVIDER` / `QWEN_MODEL`，只发送已计算技术数据，不联网、不新增 ticker、不改写规则评分。
- K 线图：周期与 SMA/EMA/ATR/ADX 可配；支持 VWAP、MACD、RSI、KDJ、布林带、成交量、60 天筹码峰、Auto Fibonacci Retracement 与 Extension；多用户版还会在手动 Plot 时显示最近可用到期日及可配置未来 1–12 个月汇总的 Call/Put Open Interest 墙，以及 Dealer-GEX 墙（Call 为正、Put 为负的仓位代理）。调整 OI 覆盖月份会重新下载 OI/IV；K 线自动刷新不会下载期权链，但会使用缓存 OI/IV 与最新股价重算 GEX 并移动价格线。
- 多用户版的 K 线自动刷新会恢复同一浏览器、同一 ticker/周期/间隔/币种下的 zoom/pan 与 Plotly 图例隐藏/显示偏好。
- 即使 ticker、周期与间隔未变，手动点击 **Plot** 也会强制重新拉取 K 线数据。

#### Short-term Watchlist（多用户登录后）

Short-term Watchlist 位于 **Market Dashboard** 与 **Market Breadth** 之间，仅对登录用户开放。每个标的在同一张表中占两行，分别显示 5m 与 15m 信号，便于直接对照：

- Price、`1D%`（最新价相对上一交易日复权收盘价）、`Bar Diff%`、15 根 K 线蜡烛图。
- 两条可配置的 SMA/EMA、以万分之一符号 `‱` 显示的 MA Spread、均线对比图。
- Volume Ratio、近 15 根成交量柱图、以万分之一符号 `‱` 显示的 MACD Diff、MACD/Signal 图（含 0 轴虚线）。
- 距离布林上轨、Close/布林带上下轨图（上下轨为虚线）、VWAP 距离、Close/VWAP 图（含 VWAP ±1σ 虚线带）、RSI 与 RSI 30/70 参考线图、可配置周期的 ATR 与 ADX 及其趋势图。
- 标的列沿用主 Watchlist 的 Beta 着色；价格、涨跌与指标列采用对应的市场状态或数值着色规则。
- 组、标的、指标参数和 10/20/30 秒自动刷新偏好按账号保存。自动刷新独立于侧边栏的 **Auto-refresh stocks**。
- 可选短线浏览器音频提醒：可分别监听 5m/15m 的 MACD、两条可配置 MA、Close 与布林带上下轨、Close 与 VWAP、Close 与 VWAP ±1σ 带的交叉，以及 RSI 穿越 30/70；每类指标均可独立启用并设置阈值，也可按 ticker 启用/关闭。需开启短线自动刷新，并在当前浏览器点击 **Enable sound & test** 授权声音。每次新提醒会循环短音并闪烁对应指标单元格，持续时间可设为 5/10/15/30/60 秒（默认 15 秒）。提醒偏好按账号保存，已触发提醒仅在当前浏览器会话去重。
- 主 Watchlist、Market Dashboard 与 Portfolios & Reports 的表格会在窄列中自动换行表头；将鼠标悬停在任一表头上可查看完整列名。

默认情况下它请求最近 2 天的 5m/15m K 线。所需历史窗口会按当前最大指标周期自动增加；例如更长的 MA/BB/RSI/MACD 设置会请求更多天数。短线 K 线只保存在当前 Streamlit 会话内，自动刷新会覆盖该会话数据，不写入账户数据库。

#### Portfolios & AI Portfolio Reports（多用户登录后）

- 每个用户可创建多个 Portfolio 页面，保存组、ticker、买入价、股数和买入币种。
- 显示持仓市值、绝对/百分比盈亏、期间盈亏、汇率换算、组合汇总、Beta 和持仓 treemap。
- **DCA Backtest** 对当前 Portfolio 的 ticker 做等权定投仿真，并将组合累计定投收益与 `SPY`、`QQQ` 的同频定投收益用 Plotly 曲线对比；作为 ETF 的复权价格会纳入分红再投资。可选择任意可获取的历史起止日期、每月月初/第三个周五或每周周五；回测会直接从 yfinance 下载所选区间的日线，不写入 SQLite，网页只短暂缓存相同参数的结果。若短窗口内没有正常定投日，会在选定起始日执行一次初始投入。每个定投日只在已有有效历史且能交易的标的间重新等权；未上市标的不会占用现金，并从上市后的下一次定投加入。休市会顺延到下一可用交易日，图中的三角标记为每次计划定投日（不同交易所标的的实际成交可略有错开）。使用复权收盘价，未计入费用、税费和汇兑。
- **AI Portfolio Report** 是确定性工作流：Python 先计算组合权重、集中度、Beta、波动率、回撤、风险贡献和技术快照，再以受控的一次模型/搜索调用生成 HTML 报告。它不是由 Agent 自主决定工具调用的流程。
- 支持浏览器下载、一次性邮件和每周邮件计划。报告 Worker 在后台运行，页面关闭后任务仍可继续。
- **Trading 212 Position Monitor**（可选）允许登录用户保存各自的只读 Trading 212 API Key/API Secret 与 Telegram Bot 配置。独立 Worker 按 Europe/Berlin 的 5×24 窗口（周一 02:00 至周六 02:00）每分钟读取持仓当前价，按 1m/5m/15m（1/2/5/10%）、30m/60m（2/5/10%）、4h/24h（5/10%）的分级阈值发送 Telegram 消息；每级只在首次跨越时触发，回到阈值的 80% 内后重新布防。它还可提醒实时价上/下穿日线 EMA20/50/100/200/250、日线 MACD 金叉/死叉，以及 RSI(14) 上/下穿 30 或 70。技术计算使用最多两年的已完成 yfinance 日线（每天最多更新一次；新持仓首次可额外下载）和 Trading 212 最新价作为当天临时收盘价，绝不采用 yfinance 当日盘中 K 线。另有可关闭的放量提醒：在标的所属交易所的常规时段每 5 分钟读取 yfinance 当日累计成交量；默认当该量达到“20 日平均日成交量 × 已过常规交易时段比例”的 2 倍时，每个标的每天提醒一次。延长/隔夜时段不使用 yfinance 成交量，但价格与技术提醒继续运行。Trading 212 ticker 会以保守的交易所后缀规则映射并缓存为 yfinance ticker；网页会显示映射状态、失败原因，并允许持仓所属用户手动覆盖映射。**Refresh mappings now** 不受 5×24 监控窗口限制：它仅读取当前持仓并验证/更新映射与日线缓存，不保存价格快照、不评估告警也不会发送 Telegram。无法安全映射时仅跳过依赖 yfinance 的提醒，不影响价格涨跌提醒。分钟快照以 UTC 存储在独立 SQLite 数据库，并保留最近 48 个有效监控小时；冻结窗口不计入保留期，因此周一会接续周六凌晨最后的状态。节假日或无新报价不会产生告警。凭证经 `TRADING212_CREDENTIAL_ENCRYPTION_KEY` 加密后落库，页面永不回显原文。
- **Trading 212 Daily AI Account Brief**（可选）使用独立的非 root Worker，在每个用户设定的 Berlin 工作日时间最多发送一次 Telegram 日报。Python 先从 Trading 212 账户摘要、实时持仓、已有分钟快照和当天技术信号生成确定性账户事实，再选出最多五个重点持仓；若用户启用 AI 与新闻，模型只进行一次受控联网搜索和一次结构化中文解读。报告包含账户/现金概览、重点仓位、最大波动、技术信号、相关消息来源、风险与后续观察项。模型或搜索失败时仍会发送确定性模板；账户日报快照仅保存最近 90 天，凭证与原始密钥不会进入模型上下文。

#### AI Market Intelligence（多用户登录后）

- 为单个 ticker 生成可下载的 HTML 股票日报，包含行情、技术指标、新闻/搜索证据、评分、风险、评级和图表。
- 日报由基于 Qwen-Agent 的定制 Agent 执行。Agent 会通过 function calling 调用受限工具获取市场数据、搜索与证据；Python 负责最终评分、风险/评级计算、技术图、HTML、证据、图标和运行日志的确定性产出。
- 日报新闻默认使用质量驱动的 Provider fallback：`Serper → Anspire Open → SerpAPI Google News → DashScope source → SearXNG`。财报季会优先检索当季 `earnings_current` 结果和可用的公司 IR 来源；若无法核验当季结果，报告必须明确该数据缺口，不能将上一季表述为最新财报。每一层只在本地日期、相关性、准入、去重、正文质量和证据等级过滤后的累计证据仍不足时才会执行；运行目录中的 `*_search_quality_report.json` 会记录每层的调用、拒绝原因、贡献和停止原因。Anspire/SerpAPI 均为可选 Key，未配置会安全跳过。
- 默认在报告顶部加入**决策仪表盘**：Python 基于已有评分、技术点位、已验证 evidence 和市场阶段生成交易参考区间、无持仓/已有持仓建议、催化因素、风险警报与行动检查项。额外的模型综合最多一次、无工具调用；模型不能改写最终评分、编造价格或 evidence ID，失败时立即使用可见的确定性回退模板。
- 仪表盘明确区分 **Python 综合评级** 与按“无持仓 / 当前持仓”情境经风险护栏调整后的**决策行动**；例如综合评级为 Hold 时，无持仓行动可以是更保守的 Watch。
- 可选 P3 提供技术、消息/基本面、风险三类受限意见组件：它们只读取已构建的本地 Context，不联网、不调用工具、不改评分、不生成 HTML。Python 会显示分歧说明，并且只会在强烈风险/分歧时保守地下调乐观动作；功能默认关闭，开启后每份报告最多增加三次无工具模型调用。
- 决策仪表盘会按 ticker 推断 US / DE / HK / CN / JP / KR / TW / Crypto 的本地时区和市场阶段；P0 使用 weekday-only 交易日近似。下载表单可暂时关闭该仪表盘以兼容旧版报告。
- 支持页面生成、一次性邮件和每周计划。搜索与文章抓取使用输入验证和 SSRF 防护。
- **Market Recap** 不会进入个股 Agent 或改变个股评分。可选美股、A 股或合并复盘，支持 HTML 下载、一次性邮件和 Europe/Berlin 周期邮件。美股复用 yfinance 与共享两年日线缓存，使用 `ES=F`（S&P 500 E-mini）和 `NQ=F`（Nasdaq-100 E-mini）作为带成交量的核心市场代理，汇总其当日 OHLC/振幅、S&P 500/Nasdaq 100 成分股市场宽度及其日变化、行业轮动，以及 `^TNX`、`BZ=F`、`DX-Y.NYB` 的跨资产背景；A 股通过 efinance 优先、akshare 兜底获取指数、涨跌家数、涨跌停、成交额、行业与概念排行。市场新闻按收盘走势、板块主线、政策/宏观分组检索并去重；美国首个周五非农发布后会额外检索实际结果并拒绝发布前预告类来源。最高排名的少量文章会经既有 SSRF 防护提取器补充受控摘要。模型只对这些受控证据作一次无工具解读；无法调用时使用确定性模板，数据源失败会明确显示数据边界。

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
              Trading 212 Monitor Worker -> Trading 212 / Telegram
              Trading 212 Daily Summary Worker -> Trading 212 / LLM search / Telegram
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
trading212_monitor.py              账户隔离的加密凭证、分钟快照、告警状态与 Trading 212/Telegram 客户端
trading212_monitor_worker.py       独立的单写入持仓监控 Worker
trading212_daily_summary.py        每日账户快照、受控联网 AI 解读与 Telegram 日报
trading212_daily_summary_worker.py 独立的每日账户日报调度 Worker
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
QWEN_MODEL=deepseek-v4.1-flash-1017
QWEN_RESEARCH_MODEL=deepseek-v4.1-flash-1017
DASHSCOPE_API_KEY=your_key
QWEN_AGENT_USE_RAW_API=true
# deepseek-v4.1-flash-1017 is automatically routed to Model Studio Chat
# Completions; DashScope web research uses Responses API web_search. Set these
# when your Model Studio key belongs to a specific workspace/region:
# DASHSCOPE_WORKSPACE_ID=your_workspace_id
# DASHSCOPE_REGION=cn-beijing
# DASHSCOPE_COMPATIBLE_MODEL_SERVER=  # optional full endpoint override

# Optional manual AI rerank for Market Breadth & Screener
SCREENING_RERANK_PROVIDER=inherit # inherit / dashscope / deepseek / openai_compatible
SCREENING_RERANK_MODEL=            # empty = inherit QWEN_MODEL / provider default
SCREENING_RERANK_TIMEOUT_SECONDS=45
SCREENING_RERANK_TEMPERATURE=0.1

# Optional controlled decision dashboard for AI Stock Reports
DECISION_REPORT_ENABLED=true       # set false for the legacy report layout
DECISION_REPORT_PROVIDER=inherit   # inherit / dashscope / deepseek / openai_compatible
DECISION_REPORT_MODEL=             # empty = inherit the AI Stock Report model
DECISION_REPORT_TEMPERATURE=0.1
DECISION_REPORT_TIMEOUT_SECONDS=120
DECISION_REPORT_KEEP_CONTEXT=false # runner-level setting; web generation still cleans run folders by default
# Troubleshooting only: retain the complete run folder, including finalization_audit.json and research intermediates. Delete it manually afterwards.
DECISION_REPORT_KEEP_RUN_DIR=false

# Optional Market Recap (A-share / US market review)
MARKET_RECAP_ENABLED=true
MARKET_RECAP_LLM_PROVIDER=inherit  # inherits LLM_PROVIDER / QWEN_MODEL
MARKET_RECAP_LLM_MODEL=
MARKET_RECAP_LLM_TIMEOUT_SECONDS=60
MARKET_RECAP_LLM_TEMPERATURE=0.2
MARKET_RECAP_NEWS_MAX_ITEMS=6      # Serper-backed sources when configured
MARKET_RECAP_NEWS_MAX_AGE_DAYS=3   # reject undated/stale or low-relevance stories
MARKET_RECAP_NEWS_FETCH_MAX_URLS=3 # SSRF-safe article enrichment for top sources
MARKET_RECAP_NEWS_FETCH_TIMEOUT_SECONDS=8
MARKET_RECAP_NEWS_FETCH_MAX_CHARS=1800
MARKET_RECAP_A_SHARE_PROVIDER=auto # efinance first, akshare fallback
MARKET_RECAP_CACHE_TTL_SECONDS=900

# Optional P3 bounded specialist opinions; disabled by default (up to 3 extra model calls)
DECISION_OPINION_AGENTS_ENABLED=false
DECISION_OPINION_AGENTS_PROVIDER=inherit
DECISION_OPINION_AGENTS_MODEL=
DECISION_OPINION_AGENTS_TIMEOUT_SECONDS=45

# Search / article evidence
SEARCH_PROVIDER=priority         # priority / serper / anspire / serpapi / searxng / both / auto
# Production fallback chain: each later provider runs only when the filtered,
# cumulative evidence is still insufficient.
SEARCH_PROVIDER_PRIORITY=serper,anspire,serpapi,dashscope,searxng
SERPER_API_KEY=your_key          # Serper is not SerpAPI
ANSPIRE_API_KEY=                 # optional Anspire Open fallback
SERPAPI_API_KEY=                 # optional SerpAPI Google News fallback
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

# Terminal 4: optional, required for Trading 212 Telegram monitoring
python -m trading212_monitor_worker

# Terminal 5: optional, required for scheduled Trading 212 daily AI briefs
python -m trading212_daily_summary_worker
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
- 启用 Trading 212 监控前，生成并安全保存 `TRADING212_CREDENTIAL_ENCRYPTION_KEY`；安装 `deploy/stock-watchlist-trading212-monitor.service`。如启用每日 AI 日报，也安装 `deploy/stock-watchlist-trading212-daily-summary.service`；两项服务均以 `stockwatch` 非 root 用户运行。Trading 212 API Key 应限制为只读权限，并在 Trading 212 中限制为服务器公网 IP。
- 先备份服务器 `.env`，再通过安全通道覆盖；权限建议为 `600`。
- 更新代码后安装 requirements、执行编译检查，再重启服务。服务名取决于你的部署；示例系统可使用 `stock-watchlist-api.service`、`stock-watchlist.service` 和 `stock-watchlist-report-worker.service`。

```bash
cd /opt/Stock_watch_list
git pull --ff-only origin main
./venv/bin/python -m pip install -r requirements.txt
./venv/bin/python -m py_compile app_streamlit_multiuser.py stock_watch_list_back_end.py
sudo systemctl restart stock-watchlist-api.service stock-watchlist.service stock-watchlist-report-worker.service stock-watchlist-trading212-monitor.service
```

首次启用 Trading 212 监控时，在服务器 `.env` 设置一把稳定的加密主密钥，然后安装并启动独立服务：

```bash
cd /opt/Stock_watch_list
./venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# 将上一行输出粘贴为 .env 中的 TRADING212_CREDENTIAL_ENCRYPTION_KEY=...，不要更换或提交此值。
sudo cp deploy/stock-watchlist-trading212-monitor.service /etc/systemd/system/
sudo cp deploy/stock-watchlist-trading212-daily-summary.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now stock-watchlist-trading212-monitor.service stock-watchlist-trading212-daily-summary.service
sudo systemctl status stock-watchlist-trading212-monitor.service --no-pager
```

### 数据、缓存与刷新

- `/api/stock_data` 使用 SQLite `price_cache` 保存日线 `adj_close` 与成交量；历史保留期由后端清理策略控制。
- 主 Watchlist 数据在 Streamlit 侧有短期缓存；**Refresh Stocks**、全局自动刷新或缓存到期后会更新。
- Short-term Watchlist 的 5m/15m 数据调用 `/api/kline_data`，按 ticker、interval 和所需历史窗口在当前会话暂存。它不会每 10/20/30 秒重新下载上一交易日 `Adj Close`。
- Short-term `1D%` 由短线最新 Close 与共享日线缓存中的上一交易日复权收盘价计算；ticker 的 Beta 着色也复用共享日线数据。
- Market Breadth & Screener 不受自动刷新影响，只在手动点击 **Refresh Breadth & Screen** 时重新计算并保存筛选快照。未登录用户仍可查看 Breadth，但不能使用账户自选、Screener 或 Screening History。

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

- Editable grouped watchlists, market dashboards, and portfolio pages with Yahoo Finance ticker search, dark mode, and local/EUR display. Ticker cells wrap instead of truncating long symbols; hover a Ticker for its name and beta, or hover a Price cell for its trading date or the US Eastern timestamp of a pre-/after-hours estimate.
- Daily-market metrics including price, a 20-bar daily candlestick SVG, 1D/5D/1M/YTD returns, RSI, volume ratio, EMA and Bollinger distances, relative returns/momentum versus `^GSPC`, valuation, analyst data, targets, market cap, and beta. Expanded relative-momentum columns include an embedded 20-daily-bar trend sparkline per metric, with a gray dashed zero axis.
- CNN Fear & Greed, VIX, and Crypto Fear & Greed cards; on-demand S&P 500/Nasdaq 100 market-breadth charts and treemaps.
- **Market Breadth & Screener** (authenticated users): one **Refresh Breadth & Screen** action refreshes Breadth and screens the union of S&P 500, Nasdaq 100, and all account watchlists, portfolio holdings, and Short-term symbols. Only verified equities are eligible; indices, ETFs, futures, FX, crypto, and pseudo tickers are excluded. Three transparent daily-bar strategies are included: Trend Quality, Volume Breakout, and Oversold Reversal. Screening is market-aware: local-currency price/liquidity gates and local benchmarks are used for China A, Hong Kong, Japan, Germany, and US symbols. Factor base scores and separate risk deductions are shown; PE/PB/market-cap use same-day SQLite cache first, with a bounded 75-symbol enrichment shortlist rather than a whole-universe scrape. Their complete account-isolated snapshots are retained for 90 days (up to 100 runs per strategy).
- Each strategy can manually AI-rerank its rule-ranked top 15 with the inherited `LLM_PROVIDER` / `QWEN_MODEL` (or the `SCREENING_RERANK_*` overrides). The model is tool-free, sees only structured computed metrics, cannot add symbols or change rule scores, and failure safely keeps the deterministic ranking.
- Configurable K-line charts with SMA/EMA/ATR/ADX, VWAP, MACD, RSI, KDJ, Bollinger Bands, volume, chip peak, and Auto Fibonacci retracement/extension. In the multi-user app, a manual Plot also loads Call/Put open-interest walls and Dealer-GEX walls (positive calls, negative puts as a positioning proxy) for the nearest available expiry and an aggregate over a configurable 1–12-month horizon. Changing the OI horizon reloads OI/IV; K-line auto-refresh never reloads option chains, but recomputes GEX from cached OI/IV using the latest stock price and moves the price marker.
- In the multi-user app, K-line auto-refresh restores browser-local zoom/pan and Plotly legend visibility for the same ticker, period, interval, and currency.
- Clicking **Plot** always refetches K-line data, even when the ticker, period, and interval are unchanged.

#### Short-term Watchlist (authenticated multi-user app)

The Short-term Watchlist sits between **Market Dashboard** and **Market Breadth**. Each ticker has adjacent 5m and 15m rows in one comparison table. It includes:

- Price, 1D% versus the prior trading day's adjusted close, bar change, and a 15-candle SVG.
- Two configurable SMA/EMA lines, MA spread in per-ten-thousand units (`‱`), and an MA comparison sparkline.
- Volume ratio, a 15-bar volume sparkline, MACD Diff in per-ten-thousand units (`‱`), and MACD/signal with a dashed zero reference.
- Bollinger-upper distance, a close/Bollinger upper-lower sparkline with dashed bands, VWAP distance, a close/VWAP sparkline with VWAP ±1σ dashed bands, RSI and RSI 30/70 references, plus configurable-period ATR and ADX with their trend sparklines.
- Main-watchlist beta coloring for ticker cells and matching numeric/status color semantics.
- Account-scoped groups, ticker search/add, indicator parameters, and independent 10/20/30-second refresh.
- Optional short-term browser audio alerts for near and confirmed MACD, configurable-MA, close/Bollinger upper-lower, close/VWAP, close/VWAP ±1σ-band, and RSI 30/70 crossovers. Each signal has its own enable switch and threshold, and alerts can be enabled per ticker. They require short-term auto-refresh plus a one-time **Enable sound & test** browser gesture. Each new alert repeats short tones and flashes its relevant table cell for a configurable 5/10/15/30/60 seconds (15 by default); preferences are account-scoped and de-duplication is browser-session-only.
- In the main Watchlist, Market Dashboard, and Portfolios & Reports tables, narrow headers wrap automatically; hover any header to see its full column title.

The default history request is two days. The request window scales automatically when the active MA, MACD, Bollinger, or RSI periods require more history. Intraday payloads are session-only; they are overwritten on refresh and are not stored in the account database.

#### Portfolios and AI Portfolio Reports

- Multiple per-account portfolio pages with holdings, cost basis, shares, currencies, current value, P/L, period P/L, FX conversion, beta, and treemaps.
- **DCA Backtest** simulates equal-weight recurring contributions across the current portfolio tickers and compares the resulting cumulative-contribution return with `SPY` and `QQQ` DCA curves in Plotly. Their adjusted ETF closes include reinvested distributions. Choose any available historical start/end date, monthly purchases on the first or third Friday, or weekly Friday purchases. The selected daily range is downloaded directly from yfinance and is not written to SQLite; only the same-parameter browser response is cached briefly. If a short range contains no regular DCA date, one initial contribution is made on the selected start date. Each contribution is dynamically reweighted across holdings that already have usable history and can trade, so a later IPO does not consume cash and joins on its next eligible contribution. Holidays roll to the next available trading close; triangle markers show each scheduled portfolio contribution (individual local-market fills can be slightly staggered). Adjusted closes are used; fees, taxes, and FX conversion are excluded.
- The AI Portfolio Report is a deterministic workflow: Python computes weights, concentration, beta, volatility, drawdown, risk contribution, and technical snapshots before a controlled model/search call produces a self-contained HTML report.
- Download, one-off email, and weekly schedules are supported. Queued work continues in the worker after the browser closes.
- **Trading 212 Position Monitor** (optional) lets each signed-in user save a read-only Trading 212 API Key/API Secret and Telegram Bot connection. A dedicated worker polls positions once per minute in the Europe/Berlin 5×24 window (Monday 02:00 through Saturday 02:00) and sends tiered Telegram alerts for 1m/5m/15m (1/2/5/10%), 30m/60m (2/5/10%), and 4h/24h (5/10%) price moves. A tier fires only on its first crossing and rearms after the move returns inside 80% of that threshold. It can also alert when the live price crosses daily EMA20/50/100/200/250, daily MACD turns golden/death, or RSI(14) crosses up/down through 30 or 70. Technical calculations use up to two years of completed yfinance daily closes (updated at most daily, with an extra initial download for a new holding) plus the latest Trading 212 price as today's temporary close; yfinance intraday bars are never used. An optional volume-surge alert checks yfinance's current cumulative daily volume every five minutes during the ticker's regular exchange session. By default it fires once per ticker/exchange day when volume reaches 2× the expected cumulative volume, defined as the 20-day average daily volume multiplied by elapsed regular-session fraction. Extended/overnight sessions keep price and technical alerts but deliberately skip yfinance volume alerts. Trading 212 tickers are mapped and cached with conservative exchange-suffix rules; the UI displays status/failures and lets the holding owner save a manual override. **Refresh mappings now** works outside the 5×24 monitor window: it only reads current positions and verifies/updates mapping and daily-history cache metadata, without saving price snapshots, evaluating alerts, or sending Telegram. An unsafe/unavailable mapping skips only yfinance-dependent alerts, never price-move alerts. UTC snapshots live in a dedicated SQLite database and retain the most recent 48 active-monitor hours; the frozen interval is excluded so Monday resumes from the final early-Saturday state. Holidays or unchanged broker prices generate no alerts. Credentials are encrypted at rest with `TRADING212_CREDENTIAL_ENCRYPTION_KEY` and are never rendered back to the browser.
- **Trading 212 Daily AI Account Brief** (optional) is delivered by a separate non-root worker once at most per Berlin weekday at each user's selected time. Python first creates deterministic account facts from the Trading 212 account summary, live positions, retained snapshots, and same-day technical signals, then selects up to five focus holdings. When the user enables AI and news, one bounded web-search/model synthesis produces a structured Chinese interpretation. The Telegram brief covers account/cash summary, focus holdings, largest moves, signals, relevant linked news, risks, and next observations. A deterministic brief is still sent if AI/search fails; only the last 90 days of structured daily snapshots are retained, and credentials/raw keys never enter the model context.

#### AI Market Intelligence

- Generates downloadable HTML reports for individual tickers with market data, technicals, search/news evidence, scoring, risk, rating, and charts.
- Uses a customized Qwen-Agent workflow. The agent uses function calling for approved data/search/evidence tools; Python deterministically creates the final scoring, risk/rating, charts, HTML, evidence assets, and logs.
- News evidence uses a quality-driven fallback chain by default: `Serper → Anspire Open → SerpAPI Google News → DashScope source → SearXNG`. During earnings season, a current-quarter `earnings_current` search and any available company-IR query run before generic earnings recall; if current results cannot be verified, the report must disclose that gap rather than call the prior quarter the latest. A later provider runs only when the locally freshness-filtered, relevance-filtered, admitted, deduplicated and graded cumulative evidence remains insufficient. Each run's `*_search_quality_report.json` records provider attempts, rejected-result reasons, contribution and stop/fallback reasons. Anspire and SerpAPI are optional and are skipped safely when their keys are absent.
- Adds an opt-in-by-default **Decision Dashboard** at the top of the report. Python derives reference ranges, market phase, position-aware advice, evidence-backed catalysts/risks, and a checklist from existing artifacts. A single optional, tool-free synthesis call may improve wording, but it cannot alter the Python final score or invent prices/evidence; failures visibly fall back to a deterministic template.
- The dashboard separates the Python **rating band** from the guarded **decision action** for the no-position or current-position scenario. A `Hold` rating can therefore correctly produce a more conservative no-position `Watch` action.
- Optional P3 adds bounded technical, news/fundamental, and risk opinions. They only receive the built local context: no browsing, tools, score changes, or HTML generation. Python renders any disagreement and can only conservatively downgrade an optimistic action on strong risk/conflict. It is off by default and adds at most three tool-free model calls per report when enabled.
- Market-phase display infers US / DE / HK / CN / JP / KR / TW / Crypto local sessions. P0 uses a weekday-only calendar approximation. The download form can disable the dashboard for a legacy-layout report.
- Supports browser generation, one-off email, and weekly schedules. Article fetching includes SSRF protections.
- **Market Recap** is a separate authenticated subpage, not part of the individual-stock Agent or score. It can generate/download and email US, A-share, or combined recaps. US data reuses yfinance and the shared two-year SQLite price cache, using `ES=F` (S&P 500 E-mini) and `NQ=F` (Nasdaq-100 E-mini) as volume-bearing market proxies, alongside their OHLC/range, S&P 500/Nasdaq 100 constituent breadth, sector rotation, `^TNX`, `BZ=F`, and `DX-Y.NYB`; A-share aggregate data uses efinance first and AkShare as a fallback. Market news is diversified across close/sector/macro evidence; after the first-Friday US payrolls release, the result itself is queried and pre-release previews are excluded. One tool-free LLM call may interpret the computed snapshot, while a deterministic template is used on failure. Berlin-time schedules avoid empty repeats when neither market has a newer completed session.

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
                Trading 212 Monitor Worker -> Trading 212 / Telegram
                Trading 212 Daily Summary Worker -> Trading 212 / LLM search / Telegram
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
- `SCREENING_RERANK_PROVIDER`, `SCREENING_RERANK_MODEL`, `SCREENING_RERANK_TIMEOUT_SECONDS`, and `SCREENING_RERANK_TEMPERATURE` for the manual, tool-free Screener rerank. Empty provider/model settings inherit the main report LLM configuration.
- `DECISION_REPORT_ENABLED`, `DECISION_REPORT_PROVIDER`, `DECISION_REPORT_MODEL`, `DECISION_REPORT_TEMPERATURE`, `DECISION_REPORT_TIMEOUT_SECONDS`, and `DECISION_REPORT_KEEP_CONTEXT` for the controlled decision dashboard. Set `DECISION_REPORT_KEEP_RUN_DIR=true` only while troubleshooting: it retains the complete per-run folder (including `*_finalization_audit.json` and research intermediates) instead of automatically deleting it.
- `DECISION_OPINION_AGENTS_ENABLED`, `DECISION_OPINION_AGENTS_PROVIDER`, `DECISION_OPINION_AGENTS_MODEL`, and `DECISION_OPINION_AGENTS_TIMEOUT_SECONDS` for optional P3 bounded specialist opinions.
- `MARKET_RECAP_*` controls the Market Recap capability in AI Market Intelligence. It uses one tool-free LLM interpretation of deterministic public-market data, shares the existing US daily-price cache, and stores only short-lived global aggregate snapshots—not account-specific recap history or full news articles. The snapshot includes cached US index session ranges and breadth changes, plus available A-share industry and concept rankings. Serper news is queried separately for each selected market, diversified across close/sector/policy themes, filtered by a usable publication date, recency, market relevance, and URL safety; after the first-Friday payrolls release, an actual-results query is added and preview articles are rejected. The selected sources are optionally enriched through the existing SSRF-protected article extractor before they reach the report or LLM. Scheduled delivery runs in Europe/Berlin time and skips sending when neither selected market has a newer completed trading date.
- `SEARCH_PROVIDER=priority`, `SEARCH_PROVIDER_PRIORITY`, `SERPER_API_KEY`, `ANSPIRE_*`, `SERPAPI_*`, `SEARXNG_*`, and `ARTICLE_FETCH_*` for evidence gathering. `SERPER_API_KEY` and `SERPAPI_API_KEY` belong to different providers.
- `REPORT_SMTP_*`, `REPORT_*`, and `PORTFOLIO_*` for email, queues, schedules, limits, and Portfolio AI Reports.
- `TRADING212_CREDENTIAL_ENCRYPTION_KEY`, `TRADING212_MONITOR_*`, and `TELEGRAM_REQUEST_TIMEOUT_SECONDS` for the optional per-account Trading 212 monitor. The encryption key is mandatory before the UI accepts any broker or Telegram secret; preserve it securely because rotating it makes saved secrets undecryptable.

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

# Terminal 4: optional, required for Trading 212 Telegram monitoring
python -m trading212_monitor_worker

# Terminal 5: optional, required for scheduled Trading 212 daily AI briefs
python -m trading212_daily_summary_worker
```

Check the API with `curl -fsS http://127.0.0.1:5000/api/health`.

### Deployment, caching, and security

- Run Flask, Streamlit, and the report worker as separate services in production. Set `STOCK_DEV_MODE=0`; do not run an internet-facing worker as root.
- Before enabling Trading 212 monitoring, create and securely retain `TRADING212_CREDENTIAL_ENCRYPTION_KEY`, then install `deploy/stock-watchlist-trading212-monitor.service`. To enable daily AI briefs, also install `deploy/stock-watchlist-trading212-daily-summary.service`; both run as the same non-root `stockwatch` account. Grant the Trading 212 key read-only permissions and restrict it to the server's public IP in Trading 212.
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
sudo systemctl restart stock-watchlist-api.service stock-watchlist.service stock-watchlist-report-worker.service stock-watchlist-trading212-monitor.service
```

To enable Trading 212 monitoring for the first time, create one stable encryption key in the server `.env`, then install its separate worker:

```bash
cd /opt/Stock_watch_list
./venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Put the output in .env as TRADING212_CREDENTIAL_ENCRYPTION_KEY=... . Do not rotate or commit it.
sudo cp deploy/stock-watchlist-trading212-monitor.service /etc/systemd/system/
sudo cp deploy/stock-watchlist-trading212-daily-summary.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now stock-watchlist-trading212-monitor.service stock-watchlist-trading212-daily-summary.service
sudo systemctl status stock-watchlist-trading212-monitor.service --no-pager
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

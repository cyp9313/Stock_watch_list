# Stock Watch List

Stock Watch List 是一个本地优先的股票观察、市场仪表盘、个人持仓监控和 AI 日报工具。项目包含 Tkinter 桌面端、单用户 Streamlit、多用户 Streamlit、独立 Flask 数据后端，以及用于定时邮件日报的后台 worker。

本项目用于研究和数据观察，不构成投资建议。Yahoo Finance、StockAnalysis、搜索服务、模型服务和 SMTP 服务都可能限流、延迟、变更字段或返回空数据。

## 功能概览

- 股票和跨市场 watchlist：价格、1D/5D/1M/YTD、RSI、相对 `^GSPC` 的 20D/60D/120D 超额收益、3/6/12M 相对动量、EMA 偏离、布林带、成交量、估值、分析师评级、目标价、市值和 Beta。
- 顶部市场情绪 gauge：CNN Fear & Greed、`^VIX` volatility gauge、Crypto Fear & Greed。
- 市场宽度：S&P 500 和 Nasdaq 100 成分股位于 20/50/200 日均线上方的比例、历史曲线和 treemap；该重计算只在点击 sidebar 的 `Refresh Breadth` 时触发，页面启动、切换 tab 或刷新 watchlist 不会自动下载和重算。
- K 线图：K 线、6 条可分别设置周期和 SMA/EMA 的均线、VWAP、MACD、RSI、KDJ、布林带、成交量、Fibonacci 和 60d 筹码峰。VWAP 仅累计有成交量的 K 线，日内图按交易日重置；单用户版将参数保留在当前浏览器会话，多用户版登录后会按账号保存。
- 多用户配置：账号登录、每个用户独立 watchlist、market dashboard、K 线指标参数、portfolio pages 和 AI 日报任务。
- Portfolios & AI Reports：用户可以录入个人持仓，按现有 watchlist 表格模板展示市场数据，并额外显示买入价、股数、持仓现价、绝对盈亏和盈亏百分比；登录后可直接生成、下载、邮件发送或定时发送 AI Portfolio Report。
- AI Agent Reports（多用户版登录后可用）：基于行情、搜索证据、可选文章正文、技术指标、评分和图表生成单标的 HTML 报告；支持下载、一次性邮件任务和每周邮件计划。
- 邮件日报：一次性邮件、按周计划、后台 worker、失败重试、队列容量控制和过期控制。

## 当前架构关系

```text
Tkinter desktop          Single-user Streamlit          Multi-user Streamlit
app_tkinter.py           app_streamlit.py               app_streamlit_multiuser.py
      \                         |                                |
       \------------------------+------------- HTTP -------------+
                                      |
                                      v
                    Flask API: stock_watch_list_back_end.py
                                      |
                 SQLite market cache / user cache / external APIs
                                      |
              yfinance / StockAnalysis / market breadth data / FX

Multi-user Streamlit, logged-in users only
      |
      +-- synchronous AI report download:
      |      ticker: daily_report.service.generate_report()
      |      portfolio: daily_report.portfolio_service.generate_portfolio_report()
      |
      +-- email jobs and weekly schedules:
             daily_report.jobs -> daily_report.worker -> SMTP
                                      |
                                      v
                         AI Agent / search / SSRF-protected article fetch
```

- `app_tkinter.py` 是桌面端，通过 Flask API 获取市场数据。
- `app_streamlit.py` 是单用户网页端，不包含多用户账号、邮件日报计划和 portfolio 配置管理。
- `app_streamlit_multiuser.py` 是多用户网页端，包含登录、每用户配置、可保存的 K 线指标参数、Portfolios & AI Reports、单标的 AI 报告下载、邮件任务和周计划 UI。
- `stock_watch_list_back_end.py` 是 Flask 市场数据 API。开发模式下前端可以尝试启动本地 Flask；生产环境建议把 Flask 作为独立 systemd 服务运行。
- `daily_report.worker` 是独立后台进程，只处理已经持久化的邮件日报任务和周计划物化，不依赖浏览器会话。

## Watchlist 和 Portfolio 表格

Watchlist 表格的主要指标：

- `20D Rel%`、`60D Rel%`、`120D Rel%`：标的过去 20/60/120 个交易日收益率减去 `^GSPC` 同窗口收益率，单位是百分比。
- `3/6/12M Rel%`：3/6/12 个月相对 `^GSPC` 的加权超额收益，权重为 `0.2 / 0.3 / 0.5`，单位是百分比。
- `RSI`：14 日 RSI。50 为中性白色，高于 50 越多越红，低于 50 越多越绿。
- `Ticker`：按市场适配后的 Beta 染色。美股和默认标的使用 `^GSPC`，欧洲股票/ETF 使用 `SXR8.DE`，A股股票/ETF 使用 `000001.SS`；Beta 仍写入原有按 ticker/date 组织的 SQLite 缓存。
- `Price`：如果后端拿到盘前/盘后价格，会显示同一份最新价格，并通过 `Price Source` 使用蓝色/黄色提示盘前或盘后来源。

多用户版表格提供列组开关：

- `Show Name column next to Ticker`：显示或隐藏名称列。长名称会截断显示，鼠标悬停可以看到完整名称。
- `Show relative momentum columns`：统一显示或折叠 `20D Rel%`、`60D Rel%`、`120D Rel%` 和 `3/6/12M Rel%`，默认折叠。
- `Show financial columns`：统一显示或折叠 `Next Earnings`、`Trailing PE`、`Forward PE`、`PEG Ratio`、`Analysts`、`Price Target` 和 `Market Cap`，默认折叠。
- `Show EMA deviation columns`：显示或折叠 EMA 偏离列，默认折叠。

Watchlist、Market Dashboard、Market Breadth 和 Portfolio 表格的第一列 `Ticker` 会固定在左侧，横向滚动时不会离开可视区域。

Sidebar 为多用户版登录用户提供可选的无人值守自动刷新模式；游客视图不会显示或启动自动刷新，以降低公共入口负载：

- `Auto-refresh stocks`：按所选间隔自动刷新 watchlist、market dashboard 和 portfolio 使用的 `/api/stock_data` 数据。
- `Auto-refresh K-line chart`：按所选间隔自动刷新当前已经绘制的 K 线图；同一 ticker/period/interval/currency 下会通过浏览器端状态保存恢复用户当前的 zoom/pan 视野。
- `Auto-refresh interval`：多用户版登录用户可选 1、5、15、30 或 60 分钟。

自动刷新不会触发 Market Breadth；Market Breadth 始终只在点击 sidebar 的 `Refresh Breadth` 时下载和重算。

`Portfolios & AI Reports` 位于多用户版的 `Market Breadth` 和 `AI Agent Reports` 之间。每个用户可以在 Customize Pages 中添加多个 portfolio page；每个页面同时提供持仓监控和 AI Portfolio Report。Portfolio 数据会进入同一个 `/api/stock_data` 请求集合，尽量复用 watchlist、market dashboard 和 market breadth 已有缓存。

Portfolio editor 采用稳定的文本格式，每行一个持仓：

```text
Group | Ticker | Buy Price | Shares | Buy Currency
```

示例：

```text
Chips | TSM | 165.00 | 5 | USD
Chips | MU | 95.50 | 10 | USD
MegaCap | AAPL | 180.50 | 10 | USD
HK | 0700.HK | 380 | 100 | HKD
```

也可以省略 Group，此时自动归入 `Portfolio` 组：

```text
AAPL | 180.50 | 10 | USD
```

常用货币代码包括 `USD`、`EUR`、`CNY`、`CNH`、`HKD`、`JPY`、`GBP`、`GBX`、`CAD`、`AUD`、`CHF`、`SEK`、`NOK`、`DKK`。伦敦 `.L` 标的在 Yahoo Finance 中常以便士计价，通常使用 `GBX`。

Portfolio 额外列：

- `Buy Price`：用户录入的单股买入价，使用买入货币显示，默认底色。
- `Shares`：用户录入股数，默认底色。
- `Market Value`：股数乘以最新价格。如果 ticker 货币和买入货币不同，会用最新 FX 折算到买入货币；按 `P/L%` 染色。
- `P/L`：绝对历史盈亏，按 `P/L%` 染色。
- `P/L 1D`、`P/L 5D`、`P/L 1M`：用当前持仓市值和对应 `1D%`、`5D%`、`1M%` 反推的区间绝对变化，使用买入货币显示；如涉及货币不一致，使用最新 FX 折算。
- `P/L%`：历史盈亏百分比，盈利越多越绿，0 附近白色，亏损越多越红。

Portfolio 底部会显示 treemap：面积按持仓现价，颜色按对应标的的 `1D%`。如果同一个 portfolio page 中所有买入货币一致，会显示总持仓现价、总绝对盈亏、总盈亏百分比、1D/5D/1M 组合绝对变化，并在总结行的 `1D%`、`5D%`、`1M%` 列显示按买入货币结算且考虑各标的持仓比例的组合变化率；总结行的 `Ticker` 单元格显示按当前持仓市值加权的综合 beta。如果买入货币混用，则隐藏合计数字并提示用户。

每个 portfolio page 还包含 `AI Portfolio Report` 区域。登录用户可以为该 portfolio 生成自包含 HTML 组合报告、下载报告、提交一次性邮件任务，或创建 weekly portfolio schedule。Portfolio AI 报告固定采用 Top-risk news 模式：所有持仓都会进入组合定量分析和技术面快照，新闻研究优先覆盖风险贡献、权重和技术风险最高的 Top-risk 持仓，并补充最大 group 和宏观查询。报告中的权重、HHI、组合 beta、波动率、回撤、风险贡献和操作建议基础数据由 Python 计算；第一阶段不输出精确买卖股数，只输出 `add`、`hold`、`trim`、`reduce`、`exit`、`watch` 这类方向性建议、目标权重区间和触发条件。

Portfolio page 会自动保存稳定 `id` 和 `analysis_settings`。定期任务绑定该稳定 ID，而不是 page 名称或下标；weekly portfolio schedule 触发时会重新读取用户最新持仓，不会永久复用创建 schedule 时的旧 snapshot。

## 当前目录结构

```text
.
├── app_tkinter.py                    # Tkinter 桌面前端
├── app_streamlit.py                  # 单用户 Streamlit 前端
├── app_streamlit_multiuser.py        # 多用户 Streamlit、Portfolio 和日报 UI
├── kline_indicators.py               # 可配置的均线、VWAP、MACD、RSI 和 KDJ 计算
├── kline_indicator_controls.py       # 单/多用户 Streamlit 共用的 K 线参数面板
├── stock_watch_list_back_end.py      # Flask 市场数据 API
├── market_data_service.py            # 共享市场数据访问层
├── multiuser_store.py                # 用户、密码哈希、watchlist 和 portfolio 配置
├── portfolio_analysis/               # Portfolio snapshot、metrics、risk ranking 和 advice validation
├── ticker_mapping.py                 # ticker 格式与映射
├── config_loader.py                  # 统一 .env 加载器
├── assets/                           # 页面图标等静态资源
├── daily_report/
│   ├── service.py                    # 同步报告子进程封装
│   ├── portfolio_service.py          # Portfolio AI HTML 报告生成封装
│   ├── jobs.py                       # 邮件队列、计划任务、限流和容量控制
│   ├── worker.py                     # 后台邮件 worker
│   ├── mailer.py                     # SMTP 投递
│   ├── run_report.py                 # AI 日报 CLI 入口
│   ├── run_portfolio_report.py       # Portfolio AI 报告 CLI 入口
│   ├── scripts/                      # 行情、图表和 HTML 报告脚本
│   └── src/stock_daily_agent/
│       ├── cli.py                    # Agent CLI
│       ├── tools.py                  # Agent 工具、证据和评分流程
│       └── article_fetcher.py        # SSRF 防护的正文抓取模块
├── deploy/
│   ├── setup-worker-user.sh          # worker 专用用户部署辅助脚本
│   └── stock-watchlist-report-worker.service
├── docs/                             # Review、部署或设计文档
├── tests/                            # pytest 测试
├── requirements.in                   # 顶层运行时依赖输入
├── requirements.txt                  # 锁定后的运行时依赖
├── requirements-dev.in               # 开发/测试依赖输入
├── requirements-dev.txt              # 锁定后的开发/测试依赖
└── .env.example                      # 配置模板；复制为本地 .env
```

运行时会产生 SQLite、缓存、日志和用户数据文件，例如 `stock_cache.db`、`watchlist_users.db`、`daily_report_jobs.db`、`user_data/`、`daily_report/runs/`、`logs/`。这些文件不应提交。

## 安装依赖

要求 Python 3.10+。建议在项目根目录创建虚拟环境。

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -r requirements-dev.txt
```

macOS / Linux：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -r requirements-dev.txt
```

`requirements.txt` 是运行时锁定文件。只部署运行服务时可以不安装 `requirements-dev.txt`。依赖升级应先修改 `.in` 文件，再在受控环境中重新生成锁定文件并运行测试。

## `.env` 统一加载规则

先复制模板，再只在本机或部署环境填写真实值：

```bash
cp .env.example .env
```

Windows 命令提示符也可以使用：

```cmd
copy .env.example .env
```

不要把 `.env` 提交到 Git。

统一加载规则由 `config_loader.load_project_env()` 定义：

1. 已存在的进程环境变量优先。
2. 调用方显式传入的 env 文件其次。
3. 项目根目录的 `.env` 再次。
4. 最后使用代码中的默认值。

除非调用方显式传入 `override=True`，`.env` 不会覆盖已有进程环境变量。项目根目录由代码位置确定，不依赖当前 shell 工作目录。Flask 后端、日报 worker 和日报 CLI 都遵循同一规则。

常用配置：

- `STOCK_API_BASE_URL`：前端访问 Flask API 的地址，默认 `http://127.0.0.1:5000`。
- `STOCK_DEV_MODE`：`1` 允许 Streamlit 在开发时尝试启动本地 Flask；`0` 表示只连接独立后端。
- `STOCK_CACHE_DB_PATH`：市场缓存 SQLite 覆盖路径。
- `REPORT_JOB_DB`：日报队列 SQLite 覆盖路径。
- `REPORT_*`：单标的日报队列、下载、计划、邮件和 worker 限制。
- `PORTFOLIO_*`：Portfolio AI 报告的同步下载、邮件提交、计划数量、模型、提供商和推理设置；与单标的日报共享全局运行并发限制。
- `DASHSCOPE_API_KEY`、`DEEPSEEK_API_KEY`、`OPENAI_API_KEY`、`SERPER_API_KEY`：按所选模型和搜索服务配置。模板中的 `your_...` 仅为占位符。

## 本地开发启动顺序

推荐把 Flask 独立运行，尤其是调试多用户和日报功能时。

1. 激活虚拟环境并配置本地 `.env`。
2. 启动 Flask 后端：

   ```bash
   python stock_watch_list_back_end.py
   ```

3. 验证健康检查：

   ```bash
   curl http://127.0.0.1:5000/api/health
   ```

   成功时返回类似：

   ```json
   {"service":"stock-watchlist-api","status":"ok","version":"1.0"}
   ```

4. 另开终端启动所需前端之一：

   ```bash
   streamlit run app_streamlit.py
   streamlit run app_streamlit_multiuser.py
   python app_tkinter.py
   ```

5. 如果要处理邮件日报任务，再开一个终端启动 worker：

   ```bash
   python -m daily_report.worker
   ```

生产环境建议设置 `STOCK_DEV_MODE=0`，由独立进程管理 Flask API、Streamlit 和 worker。

## Flask API

### `GET /api/health`

用于负载均衡、部署探针和前端连接确认。它不返回市场数据或密钥。

### `POST /api/stock_data`

当前推荐接口。请求体为 JSON，`groups` 必填，`broad_market_tickers` 和 `cache_key` 可选：

```json
{
  "groups": {
    "Core": ["AAPL", "MSFT"]
  },
  "broad_market_tickers": ["^GSPC"],
  "cache_key": "optional-user-cache-key"
}
```

响应数据包含 watchlist 表格需要的价格、收益率、RSI、相对动量、估值、财报日期、分析师评级、市值、Beta 和 `Price Source`。后端会在价格请求中隐式加入 `^GSPC` 用于相对收益计算，并按 ticker 市场为 Beta 额外加入 `^GSPC`、`SXR8.DE` 或 `000001.SS` 等 benchmark；这些 benchmark 不会额外显示为用户 watchlist 行，除非用户自己配置。

Ticker 会先通过 `ticker_mapping.py` 规范化，再用于 yfinance 和 StockAnalysis。StockAnalysis 已覆盖美股、港股、A 股、常见欧洲市场以及韩国 `.KS` 主板股票；例如 `005930.KS` 会映射为 `https://stockanalysis.com/quote/krx/005930/`。`.KQ` 暂未单独映射。

`GET /api/stock_data` 仅为兼容旧客户端保留，并在响应中标记为弃用。市场宽度接口使用 `POST /api/breadth_data`，K 线接口为 `GET /api/kline_data?ticker=...&period=...&interval=...`。

## SQLite 数据位置

- 市场缓存默认位于项目根目录的 `stock_cache.db`；使用 `STOCK_CACHE_DB_PATH` 可覆盖。
- 多用户数据默认位于项目根目录的 `watchlist_users.db`。
- 报告队列默认位于项目根目录的 `daily_report_jobs.db`；使用 `REPORT_JOB_DB` 可覆盖。
- 多用户市场缓存位于 `user_data/`，由受限 cache key 派生。
- 每次 AI 日报生成会使用 `daily_report/runs/` 下的运行目录，服务完成后会清理运行产物。

数据库、缓存、WAL/SHM 文件、日志和用户生成内容都属于运行时数据，不应提交。

## AI 日报、worker、计划任务和邮件流程

### CLI

从项目根目录运行：

```bash
python daily_report/run_report.py AAPL --months 3 --search-provider auto
```

常用选项包括 `--provider`、`--model`、`--search-provider`、`--no-article-fetch`、`--run-dir` 和 `--output`。CLI 会创建独立 run directory，依次获取行情、搜索证据、可选正文、技术说明、图表和最终 HTML。

### 多用户 UI

多用户 Streamlit 的 `AI Agent Reports` tab 提供：

- 登录用户同步生成并下载报告；
- 一次性生成并发送邮件任务；
- 一个 ticker 对应一个 weekly schedule；
- 每个 schedule 可以一次性勾选周一到周日的多个发送日；
- 一个 schedule 使用统一的 Europe/Berlin 发送时间；
- 每个账号最多 7 个 ticker schedule；
- 暂停、恢复和删除 schedule。

多用户 Streamlit 的每个 `Portfolios & AI Reports` 子页面都提供 Portfolio AI 报告：

- 登录用户同步生成并下载 portfolio HTML 报告；
- 一次性生成并发送 portfolio 邮件任务；
- 每个 portfolio page 可以创建 weekly schedule，周一到周日可多选，发送时间统一使用 Europe/Berlin；
- schedule 绑定 portfolio stable ID，portfolio 改名或页面顺序变化后仍能找到最新配置；
- Portfolio 删除后，worker 不会发送旧持仓快照，相关 job 会失败并显示错误；
- Portfolio 报告和 ticker 日报共享 `daily_report_jobs.db`、worker、SMTP、Message-ID 幂等、重试和队列容量控制。

Portfolio AI Analyst v3 采用“Python 量化事实 + 单次 AI 综合分析”架构：Python 先计算组合权重、风险贡献、Beta、波动率、回撤与技术广度，再由 `deepseek-v4-pro` 使用 DashScope 内置联网搜索和 high 深度思考完成一次完整投资组合分析。用户可在每个 Portfolio 页面的 AI report settings 中设置投资期限、风险偏好、报告风格、分析重点、建议模式、新闻窗口与自定义要求；这些设置会动态改变 Prompt。价格、权重和技术指标仍由 Python 固定，模型不能改写。该路径最多调用一次模型/联网搜索；不会调用 Serper、Gap Search、补搜或模型重试。

AI Analyst 直接生成组合观点、技术面解释、消息面分析、持仓结论和条件式建议；消息链接质量不会决定整份分析是否存在。Python 只做必要的结构与安全校验：Ticker 必须属于持仓，当前权重与量化指标不可被模型改写，私有 URL 和未来日期会被移除，用户禁用的加仓/减仓方向会被本地规则覆盖。消息链接会标记为“与 DashScope 来源匹配”或“AI 返回、未独立核验”；链接不完整不会删除其余分析。运行目录会输出 Analyst 设置、诊断、消息分析、搜索来源、最终 JSON、reasoning 内容和 Token 用量。

相关环境变量为：

```dotenv
PORTFOLIO_REPORT_MODEL=deepseek-v4-pro
PORTFOLIO_REPORT_PROVIDER=dashscope
PORTFOLIO_ENABLE_THINKING=true
PORTFOLIO_REASONING_EFFORT=high
```

产品约束不是可配置的软目标：`search_strategy=turbo`、联网搜索调用上限为 1、外部搜索调用为 0、重试与 Gap Search 为 0。报告会把这些计数和 DashScope 输入/输出 Token 直接显示在“数据质量与限制”中。

报告中的历史组合 Beta、风险评分、目标权重区间、预计释放权重和风险变化均由 Python 确定性计算。风险评分会显示分项、缺失项和评分可信度；历史 Beta 与累计收益目前仍采用本地货币收益近似，并在 HTML 中明确披露未进行逐日历史 FX 对齐。

### worker

worker 必须独立启动。关闭浏览器不会取消已经入队的邮件任务。流程如下：

```text
weekly schedule due
        |
        v
daily_report.jobs creates persistent email job
        |
        v
daily_report.worker claims job
        |
        v
daily_report.service generates HTML report in subprocess
        |  (ticker)
        |  or
daily_report.portfolio_service generates Portfolio HTML report in subprocess
        |
        v
daily_report.mailer sends via SMTP
        |
        v
job state is updated in SQLite
```

队列会执行每账号、全局 pending/running、重试次数和过期时间限制；ticker 报告主要由 `REPORT_*` 配置控制，portfolio 报告可额外使用 `PORTFOLIO_*` 配置控制同步下载、邮件和 schedule 限额。两类报告共享 `REPORT_MAX_GLOBAL_RUNNING`，避免多个重型 Agent 同时运行。

SMTP 的成功确认和本地状态写入不是同一个原子事务，因此系统无法严格保证 exactly-once 投递。固定 Message-ID 和任务状态记录只能降低重复邮件概率。

## systemd 专用用户部署

生产环境不要用 root 身份运行 worker。仓库提供：

- `deploy/setup-worker-user.sh`：创建专用系统用户、数据目录权限和队列数据库迁移的辅助脚本。
- `deploy/stock-watchlist-report-worker.service`：worker 的 systemd unit 模板。

部署前应根据实际安装位置和 Python 虚拟环境审查并替换 unit 中的：

- `WorkingDirectory`
- `ExecStart`
- `EnvironmentFile`
- `ReadWritePaths`

然后由管理员安装 unit，执行 daemon reload，启用服务，并通过 `systemd-analyze security` 检查隔离设置。worker 的目标边界是：专用非 root 用户、最小文件写入范围、无新权限、受限 capabilities、私有临时目录、资源上限和受限地址族。

Flask API 和 Streamlit 也建议分别使用独立 systemd service 管理。反向代理层可以使用 Nginx，把公网请求转发到本机 `127.0.0.1:8501` 和内部 API。

## 安全边界

### HTML 转义

yfinance 元数据、搜索摘要、文章正文和 LLM notes 都是不可信输入。日报 HTML 生成器会转义文本，并对白名单 CSS class、颜色和标的类型做约束。只有本地生成的图表片段被视为受信 HTML。

### SSRF

文章正文抓取只允许 HTTP/HTTPS，拒绝 URL 凭据、非允许端口、loopback、private、link-local、multicast、reserved、unspecified 和云 metadata 地址。初始 URL 与每一个重定向目标都会重新验证；请求会固定连接到已验证 IP，并限制重定向数和响应大小。

这不替代网络层出站控制。公网部署时仍建议使用防火墙、隔离的 outbound proxy 或等价网络策略。

### 报告权限和限流

AI 报告下载和邮件任务要求登录。系统对账号每日任务、活动生成和全局运行数应用数据库限制。公网部署还应在反向代理层配置 IP 限流、认证、日志和容量监控。

### 登录保护

密码使用 PBKDF2-SHA256、随机 salt 和常量时间比较。登录失败会按用户名计数并临时锁定。管理员创建用户时应使用交互式密码输入，不要把密码放入 shell history、命令行参数或文档。

### worker 最小权限

worker 应以专用低权限用户运行，只授予读取项目代码、读取必要 `.env`、写入报告队列数据库、运行目录和日志目录的权限。不要让 worker 拥有整个服务器或用户 home 的写权限。

### SMTP exactly-once 限制

SMTP 无法提供严格 exactly-once 语义。网络中断、SMTP 超时或进程崩溃可能发生在邮件已被服务端接收但本地还未写入成功状态的窗口。UI 和运维流程应把“可能已发送”的状态视为需要人工确认。

## 数据隐私和禁止提交的文件

以下内容不得提交：

- `.env`、API key、SMTP 凭据、密码、真实邮箱和真实服务器路径；
- SQLite 数据库、WAL/SHM 文件、缓存、日志和日报输出；
- `user_data/`、`daily_report/runs/` 以及任何用户生成内容；
- 二维码、截图或导出的报告，如果它们包含真实域名、账号、邮箱、ticker 组合或其他私人信息。

只提交 `.env.example` 中的占位符。报告、搜索证据和邮件任务可能包含用户 ticker、收件人或研究内容，应按所在环境的数据保留策略处理。

## 测试命令

运行完整测试：

```bash
python -m pytest -q
```

常用安全和日报测试：

```bash
python -m pytest tests/test_report_html_escape.py tests/test_article_url_security.py -q
python -m pytest tests/test_weekly_schedule_multiday.py tests/test_queue_capacity.py -q
python -m pytest tests/test_kline_indicators.py tests/test_portfolio_html_report.py -q
```

Portfolio 和多用户隔离相关测试：

```bash
python -m pytest tests/test_portfolio_config.py tests/test_p2_watchlist_isolation.py -q
```

修改 Python 文件后，至少运行语法检查和相关测试：

```bash
python -m compileall -q app_streamlit_multiuser.py multiuser_store.py stock_watch_list_back_end.py daily_report
python -m pytest path/to/relevant_test.py -q
```

## 当前已知限制

- 外部数据源、模型、搜索 API 和 SMTP 服务受网络、费用、配额和供应商行为影响。
- 大范围 market breadth 和大量 ticker 的 watchlist 请求可能较慢；Market Breadth 已改为手动 `Refresh Breadth` 触发，避免页面启动和切换 tab 时自动重算。
- yfinance 的盘前/盘后数据依赖供应商返回质量；非美股或不支持盘外交易的 ticker 不会有盘外价格提示。
- Portfolio 使用最新 FX 做显示和盈亏折算，不重算历史汇率成本；这适合当前持仓监控，不适合作为税务或会计报表。
- Portfolio 合计行要求同一个 portfolio page 内买入货币一致；混合货币时会隐藏合计数字。
- Portfolio AI 报告会用基础货币计算组合指标，因此混合买入货币不会阻止报告生成；但 FX 使用最新汇率，不重建历史 FX。
- Portfolio AI 第一阶段是组合风险与操作方向建议，不知道现金余额、税务、交易费用或 tax lots，因此不会给出精确买卖股数。
- AI 报告仍支持同步下载生成；虽然要求登录并有限流，公网部署仍必须补充反向代理限流和容量监控。
- SMTP 无法提供严格 exactly-once 投递，极端崩溃窗口下仍可能重复投递。
- 日报 HTML 可以离线查看，但文件体积可能较大，因为图表脚本会嵌入报告。
- 项目不提供账号自助注册、支付、投资建议、交易执行或数据源 SLA。

## 许可说明

如无另行说明，请将本项目视为个人研究工具；在分发或部署前请自行确认所使用数据源、模型服务和邮件服务的许可与条款。

---

# Stock Watch List — English Version

Stock Watch List is a local-first stock monitoring, market dashboard, portfolio tracking, and AI daily report application. It includes a Tkinter desktop app, a single-user Streamlit app, a multi-user Streamlit app, a standalone Flask data API, and a background worker for scheduled email reports.

This project is for research and data observation only. It is not investment advice. Yahoo Finance, StockAnalysis, search providers, model providers, and SMTP providers may rate-limit, delay, change fields, or return incomplete data.

## Feature overview

- Stock and cross-market watchlists: price, 1D/5D/1M/YTD returns, RSI, 20D/60D/120D excess returns versus `^GSPC`, weighted 3/6/12M relative momentum, EMA deviation, Bollinger Band deviation, volume ratio, valuation metrics, analyst ratings, price targets, market cap, and beta.
- Top sentiment gauges: CNN Fear & Greed, `^VIX` volatility gauge, and Crypto Fear & Greed.
- Market breadth: S&P 500 and Nasdaq 100 constituent ratios above their 20/50/200-day moving averages, with charts and treemaps. This heavy recalculation only runs when the sidebar `Refresh Breadth` button is clicked; app startup, tab switching, and watchlist refreshes do not automatically download or recalculate breadth data.
- K-line charts: candlesticks, six independently configurable SMA/EMA moving averages, VWAP, MACD, RSI, KDJ, Bollinger Bands, volume, Fibonacci tools, and a 60d volume-by-price profile. VWAP uses only bars with volume and resets by trading day for intraday charts. Settings are kept for the browser session in the single-user app and persisted per account in the multi-user app.
- Multi-user configuration: account login, per-user watchlists, market dashboards, K-line indicator settings, portfolio pages, and AI report jobs.
- Portfolios & AI Reports: users can enter and monitor holdings with the watchlist table style plus buy price, shares, market value, absolute P/L, P/L%, and 1D/5D/1M holding-level changes. Signed-in users can generate, download, email, and schedule an AI Portfolio Report for each portfolio page.
- AI Agent Reports (available to signed-in multi-user accounts): generate single-ticker HTML reports from market data, search evidence, optional article text, technical indicators, scoring, and charts; download them, queue one-off email delivery, or create weekly email schedules.
- Email reports: one-off email jobs, weekly schedules, a background worker, retries, queue capacity controls, and expiration controls.

## Architecture

```text
Tkinter desktop          Single-user Streamlit          Multi-user Streamlit
app_tkinter.py           app_streamlit.py               app_streamlit_multiuser.py
      \                         |                                |
       \------------------------+------------- HTTP -------------+
                                      |
                                      v
                    Flask API: stock_watch_list_back_end.py
                                      |
                 SQLite market cache / user cache / external APIs
                                      |
              yfinance / StockAnalysis / market breadth data / FX

Multi-user Streamlit, logged-in users only
      |
      +-- synchronous AI report download:
      |      ticker: daily_report.service.generate_report()
      |      portfolio: daily_report.portfolio_service.generate_portfolio_report()
      |
      +-- email jobs and weekly schedules:
             daily_report.jobs -> daily_report.worker -> SMTP
                                      |
                                      v
                         AI Agent / search / SSRF-protected article fetch
```

- `app_tkinter.py` is the desktop client and reads market data through the Flask API.
- `app_streamlit.py` is the single-user web client. It does not manage multi-user accounts, email schedules, or portfolio pages.
- `app_streamlit_multiuser.py` is the multi-user web client. It includes login, per-user configuration, saved K-line indicator settings, Portfolios & AI Reports, single-ticker AI report downloads, email jobs, and weekly schedule UI.
- `stock_watch_list_back_end.py` is the Flask market data API. In development, the frontend can try to start a local Flask backend; in production, Flask should run as a standalone service.
- `daily_report.worker` is an independent process for persistent email report jobs and weekly schedules. It does not depend on browser sessions.

## Watchlist and Portfolio tables

Main watchlist indicators:

- `20D Rel%`, `60D Rel%`, `120D Rel%`: the ticker return over the last 20/60/120 trading days minus the `^GSPC` return over the same window.
- `3/6/12M Rel%`: weighted 3/6/12-month excess return versus `^GSPC`, using weights `0.2 / 0.3 / 0.5`.
- `RSI`: 14-day RSI. 50 is neutral white; values above 50 become redder, and values below 50 become greener.
- `Ticker`: colored by market-adapted beta. US/default tickers use `^GSPC`, European stocks/ETFs use `SXR8.DE`, and China A-share stocks/ETFs use `000001.SS`. Beta still uses the existing ticker/date SQLite cache.
- `Price`: if extended-hours data is available, the same latest price is used and `Price Source` marks pre-market or after-hours values with a blue/yellow cue.

The multi-user tables include column group toggles:

- `Show Name column next to Ticker`.
- `Show relative momentum columns`.
- `Show financial columns`, collapsed by default.
- `Show EMA deviation columns`.

The first `Ticker` column is frozen on the left side for Watchlist, Market Dashboard, Market Breadth, and Portfolio tables, so it stays visible while horizontally scrolling.

The sidebar also provides optional unattended auto-refresh for signed-in multi-user sessions. Guest views do not show or start auto-refresh, which reduces public-entry load:

- `Auto-refresh stocks`: refreshes `/api/stock_data` for watchlists, market dashboard, and portfolio pages at the selected interval.
- `Auto-refresh K-line chart`: refreshes the currently plotted K-line chart at the selected interval. For the same ticker/period/interval/currency, browser-side state restores the current Plotly zoom and pan range.
- `Auto-refresh interval`: signed-in multi-user sessions can choose 1, 5, 15, 30, or 60 minutes.

Auto-refresh never triggers Market Breadth. Market Breadth is downloaded and recalculated only when the sidebar `Refresh Breadth` button is clicked.

`Portfolios & AI Reports` is placed between `Market Breadth` and `AI Agent Reports` in the multi-user app. Users can add multiple portfolio pages from Customize Pages; each page combines holding monitoring with an AI Portfolio Report. Portfolio tickers are included in the same `/api/stock_data` request so they can reuse the existing market data cache where possible.

Portfolio editor format:

```text
Group | Ticker | Buy Price | Shares | Buy Currency
```

Example:

```text
Chips | TSM | 165.00 | 5 | USD
Chips | MU | 95.50 | 10 | USD
MegaCap | AAPL | 180.50 | 10 | USD
HK | 0700.HK | 380 | 100 | HKD
```

The group can be omitted:

```text
AAPL | 180.50 | 10 | USD
```

Common currency codes include `USD`, `EUR`, `CNY`, `CNH`, `HKD`, `JPY`, `GBP`, `GBX`, `CAD`, `AUD`, `CHF`, `SEK`, `NOK`, and `DKK`. London `.L` tickers are often quoted in pence in Yahoo Finance and usually use `GBX`.

Portfolio-specific columns:

- `Buy Price`: user-entered per-share buy price in the buy currency; default background.
- `Shares`: user-entered share count; default background.
- `Market Value`: shares multiplied by the latest price. If the ticker currency differs from the buy currency, the latest FX rate is used to convert into the buy currency. The cell is colored by `P/L%`.
- `P/L`: absolute historical P/L, colored by `P/L%`.
- `P/L 1D`, `P/L 5D`, `P/L 1M`: absolute holding value change inferred from current market value and the corresponding `1D%`, `5D%`, and `1M%`; displayed in the buy currency and converted with the latest FX rate when needed.
- `P/L%`: historical P/L percentage.

The Portfolio treemap uses current market value for tile area and `1D%` for color. If all holdings in a portfolio page use the same buy currency, the final summary row shows total market value, total absolute P/L, total P/L%, and total 1D/5D/1M absolute changes. The summary-row `1D%`, `5D%`, and `1M%` cells show portfolio-level returns calculated in the buy currency with latest FX conversion and current holding weights. The `Ticker` cell shows market-value-weighted portfolio beta. If buy currencies are mixed, total figures are hidden and a warning is shown.

Each portfolio page also includes an `AI Portfolio Report` section. Signed-in users can generate a self-contained HTML portfolio report, download it, queue a one-off email job, or create a weekly portfolio schedule. Portfolio AI uses a fixed Top-risk news mode: every holding is included in quantitative portfolio analysis and the technical snapshot, while news research prioritizes the holdings with the highest risk contribution, weight, and technical risk, plus the largest groups and macro context. Python calculates weights, HHI, portfolio beta, volatility, drawdown, risk contribution, and the deterministic basis for action suggestions. The first implementation does not output exact buy/sell share counts; it provides directional actions such as `add`, `hold`, `trim`, `reduce`, `exit`, and `watch`, target weight ranges, and trigger conditions.

Portfolio pages are automatically upgraded with a stable `id` and `analysis_settings`. Weekly portfolio schedules bind to that stable ID rather than the page name or index. When a scheduled report runs, the worker reloads the user's latest holdings instead of reusing the snapshot from schedule creation time.

## Directory structure

```text
.
├── app_tkinter.py                    # Tkinter desktop frontend
├── app_streamlit.py                  # Single-user Streamlit frontend
├── app_streamlit_multiuser.py        # Multi-user Streamlit, Portfolio, and report UI
├── kline_indicators.py               # Configurable MA, VWAP, MACD, RSI, and KDJ calculations
├── kline_indicator_controls.py       # Shared Streamlit K-line indicator controls
├── stock_watch_list_back_end.py      # Flask market data API
├── market_data_service.py            # Shared market data access layer
├── multiuser_store.py                # Users, password hashes, watchlist and portfolio config
├── portfolio_analysis/               # Portfolio snapshot, metrics, risk ranking, and advice validation
├── ticker_mapping.py                 # Ticker normalization and mapping
├── config_loader.py                  # Unified .env loader
├── assets/                           # Static assets such as page icons
├── daily_report/
│   ├── service.py                    # Synchronous report subprocess wrapper
│   ├── portfolio_service.py          # Portfolio AI HTML report wrapper
│   ├── jobs.py                       # Email queue, schedules, limits, and capacity controls
│   ├── worker.py                     # Background email worker
│   ├── mailer.py                     # SMTP delivery
│   ├── run_report.py                 # AI report CLI entry point
│   ├── run_portfolio_report.py       # Portfolio AI report CLI entry point
│   ├── scripts/                      # Market data, chart, and HTML report scripts
│   └── src/stock_daily_agent/
│       ├── cli.py                    # Agent CLI
│       ├── tools.py                  # Agent tools, evidence, and scoring flow
│       └── article_fetcher.py        # SSRF-protected article fetcher
├── deploy/
│   ├── setup-worker-user.sh          # Dedicated worker-user helper script
│   └── stock-watchlist-report-worker.service
├── docs/
├── tests/
├── requirements.in
├── requirements.txt
├── requirements-dev.in
├── requirements-dev.txt
└── .env.example
```

Runtime files such as SQLite databases, caches, logs, `user_data/`, and `daily_report/runs/` should not be committed.

## Dependency installation

Python 3.10+ is required.

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -r requirements-dev.txt
```

macOS / Linux:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -r requirements-dev.txt
```

`requirements.txt` is the locked runtime dependency file. Production deployments can skip `requirements-dev.txt` unless tests are run there.

## `.env` loading

Copy the template and fill real values only in the local or deployment environment:

```bash
cp .env.example .env
```

Do not commit `.env`.

`config_loader.load_project_env()` applies this precedence:

1. Existing process environment variables.
2. Explicit env file passed by the caller.
3. Project-root `.env`.
4. Code defaults.

Unless `override=True` is explicitly used, `.env` does not override existing process environment variables. Flask, the report worker, and the report CLI follow the same rule.

Common settings:

- `STOCK_API_BASE_URL`: frontend-to-Flask API URL, default `http://127.0.0.1:5000`.
- `STOCK_DEV_MODE`: `1` allows Streamlit to try starting a local Flask backend in development; `0` expects a standalone backend.
- `STOCK_CACHE_DB_PATH`: override path for the market cache SQLite database.
- `REPORT_JOB_DB`: override path for the report queue SQLite database.
- `REPORT_*`: queue, download, schedule, email, and worker limits.
- `DASHSCOPE_API_KEY`, `DEEPSEEK_API_KEY`, `OPENAI_API_KEY`, `SERPER_API_KEY`: provider-specific credentials. Placeholder values in `.env.example` are not real credentials.

## Local development startup order

1. Activate the virtual environment and configure `.env`.
2. Start the Flask backend:

   ```bash
   python stock_watch_list_back_end.py
   ```

3. Verify health:

   ```bash
   curl http://127.0.0.1:5000/api/health
   ```

4. Start one frontend in another terminal:

   ```bash
   streamlit run app_streamlit.py
   streamlit run app_streamlit_multiuser.py
   python app_tkinter.py
   ```

5. Start the email report worker if needed:

   ```bash
   python -m daily_report.worker
   ```

In production, set `STOCK_DEV_MODE=0` and manage Flask, Streamlit, and the worker as separate processes.

## Flask API

### `GET /api/health`

Health check endpoint for deployment probes and frontend connection validation. It does not return market data or secrets.

### `POST /api/stock_data`

Recommended market data endpoint. JSON body:

```json
{
  "groups": {
    "Core": ["AAPL", "MSFT"]
  },
  "broad_market_tickers": ["^GSPC"],
  "cache_key": "optional-user-cache-key"
}
```

The response includes price, returns, RSI, relative momentum, valuation fields, earnings date, analyst rating, market cap, beta, and `Price Source`. The backend implicitly adds `^GSPC` for relative-return calculations and adds the market-adapted beta benchmark, such as `^GSPC`, `SXR8.DE`, or `000001.SS`, when needed. These benchmarks are not displayed as user rows unless explicitly configured by the user.

Tickers are normalized through `ticker_mapping.py` before yfinance and StockAnalysis are queried. StockAnalysis mapping covers US stocks, Hong Kong stocks, China A-shares, common European markets, and Korean `.KS` main-board stocks; for example, `005930.KS` maps to `https://stockanalysis.com/quote/krx/005930/`. `.KQ` is not mapped separately yet.

`GET /api/stock_data` is retained only for legacy compatibility. Market breadth uses `POST /api/breadth_data`; K-line data uses `GET /api/kline_data?ticker=...&period=...&interval=...`.

## SQLite data locations

- Market cache: `stock_cache.db` by default; override with `STOCK_CACHE_DB_PATH`.
- Multi-user data: `watchlist_users.db` by default.
- Report queue: `daily_report_jobs.db` by default; override with `REPORT_JOB_DB`.
- Per-user market cache: `user_data/`.
- Report run directories: `daily_report/runs/`.

Databases, cache files, WAL/SHM files, logs, and user-generated data are runtime data and should not be committed.

## AI reports, worker, schedules, and email flow

CLI example:

```bash
python daily_report/run_report.py AAPL --months 3 --search-provider auto
```

Useful options include `--provider`, `--model`, `--search-provider`, `--no-article-fetch`, `--run-dir`, and `--output`.

The multi-user `AI Agent Reports` tab supports:

- logged-in synchronous report generation and download;
- one-off email report jobs;
- weekly schedules, one ticker per schedule;
- selecting any combination of Monday through Sunday in one schedule;
- one shared Europe/Berlin send time per schedule;
- up to 7 ticker schedules per account;
- pause, resume, and delete.

Each `Portfolios & AI Reports` subpage supports Portfolio AI reports:

- signed-in synchronous portfolio HTML generation and download;
- one-off portfolio email jobs;
- weekly portfolio schedules with multi-day selection and one Europe/Berlin send time;
- schedules bind to the stable portfolio page ID, so renames and page reordering keep working;
- if the portfolio is deleted, the worker does not send an old snapshot and the job fails visibly;
- portfolio reports and ticker reports share `daily_report_jobs.db`, the worker, SMTP delivery, deterministic Message-ID deduplication, retries, and queue capacity controls.

Portfolio AI Analyst v3 uses a Python-facts-plus-one-analyst-call architecture. Python calculates weights, risk contributions, beta, volatility, drawdown, and technical breadth first. Then `deepseek-v4-pro` performs one DashScope built-in web-search analyst call with thinking enabled at `high`. Per-portfolio AI report settings dynamically shape the prompt, including investment horizon, risk profile, report style, analysis focus, advice mode, news window, and custom instructions. Quantitative values remain immutable. The Portfolio path does not call Serper, Gap Search, follow-up search, or model retry.

The analyst directly produces the portfolio view, technical interpretation, news analysis, holding conclusions, and conditional recommendations. News-link completeness no longer acts as a switch that can erase the entire AI report. Python performs only structural and safety enforcement: tickers must belong to the portfolio, current weights and quantitative metrics are immutable, private URLs and future dates are removed, and user-disabled add/reduce directions are overridden locally. Links are labelled as either matched to a DashScope source or AI-returned without independent verification; an imperfect link does not delete the surrounding analysis. Run artifacts include settings, diagnostics, news analysis, search sources, final JSON, reasoning content, elapsed time, and token usage.

```dotenv
PORTFOLIO_REPORT_MODEL=deepseek-v4-pro
PORTFOLIO_REPORT_PROVIDER=dashscope
PORTFOLIO_ENABLE_THINKING=true
PORTFOLIO_REASONING_EFFORT=high
```

The product invariants are hard-coded rather than configurable: `search_strategy=turbo`, a maximum of one built-in search call, zero external search calls, and zero retries or Gap Search calls.

Historical portfolio beta, the component risk score, target-weight ranges, released weight, and risk-change estimates are calculated deterministically in Python. The report exposes missing score components and score confidence. Historical beta and cumulative-return simulation still use local-currency returns as an approximation and explicitly disclose that daily historical FX alignment is not implemented.

The worker flow:

```text
weekly schedule due
        |
        v
daily_report.jobs creates persistent email job
        |
        v
daily_report.worker claims job
        |
        v
daily_report.service generates HTML report in subprocess
        |  (ticker)
        |  or
daily_report.portfolio_service generates Portfolio HTML report in subprocess
        |
        v
daily_report.mailer sends via SMTP
        |
        v
job state is updated in SQLite
```

SMTP success and local state updates are not one atomic transaction, so strict exactly-once delivery cannot be guaranteed.

Ticker reports are controlled mainly by `REPORT_*` settings. Portfolio reports add `PORTFOLIO_*` limits for synchronous downloads, email submissions, schedule counts, model/provider selection, and reasoning settings. Both kinds share `REPORT_MAX_GLOBAL_RUNNING` so multiple heavy report jobs do not run at the same time by default.

## systemd deployment with a dedicated user

Do not run the worker as root in production. The repository provides:

- `deploy/setup-worker-user.sh`
- `deploy/stock-watchlist-report-worker.service`

Before installation, review and adapt `WorkingDirectory`, `ExecStart`, `EnvironmentFile`, and `ReadWritePaths`. Use a dedicated low-privilege user, minimal writable paths, restricted capabilities, private temp directories, and resource limits.

Flask and Streamlit should also be managed as separate services. A reverse proxy such as Nginx can expose Streamlit while keeping local services bound to `127.0.0.1`.

## Security boundaries

### HTML escaping

yfinance metadata, search snippets, article text, and LLM notes are untrusted. The report builder escapes text and constrains CSS classes, colors, and ticker-type values. Only locally generated chart fragments are treated as trusted HTML.

### SSRF

Article fetching only allows HTTP/HTTPS and rejects URL credentials, disallowed ports, loopback, private, link-local, multicast, reserved, unspecified, and cloud metadata addresses. Initial URLs and every redirect target are revalidated. Requests are pinned to validated IPs and constrained by redirect and response-size limits.

This does not replace network-level egress controls.

### Report authorization and rate limits

AI report downloads and email jobs require login. The system applies per-account, active-generation, and global running limits in the database. Public deployments should also use reverse-proxy IP rate limits, authentication, logging, and capacity monitoring.

### Login protection

Passwords use PBKDF2-SHA256, random salts, and constant-time comparison. Failed logins are counted per username and can trigger temporary lockout. Admins should create users with interactive password prompts and avoid putting passwords in shell history, command-line arguments, or documentation.

### Worker least privilege

The worker should run as a dedicated low-privilege user with only the read/write access it needs: project code, required env file, report queue database, run directories, and logs.

### SMTP exactly-once limitation

SMTP cannot provide strict exactly-once semantics. A network timeout or crash can happen after the mail server accepts a message but before local state is updated. UI and operational procedures should treat “possibly sent” states as requiring confirmation.

## Data privacy and files that must not be committed

Do not commit:

- `.env`, API keys, SMTP credentials, passwords, real email addresses, or real server paths;
- SQLite databases, WAL/SHM files, caches, logs, and report outputs;
- `user_data/`, `daily_report/runs/`, or any user-generated content;
- QR codes, screenshots, or exported reports containing private domains, accounts, emails, ticker sets, or other private information.

Only placeholder values should appear in `.env.example`.

## Test commands

Full test suite:

```bash
python -m pytest -q
```

Security and report tests:

```bash
python -m pytest tests/test_report_html_escape.py tests/test_article_url_security.py -q
python -m pytest tests/test_weekly_schedule_multiday.py tests/test_queue_capacity.py -q
python -m pytest tests/test_kline_indicators.py tests/test_portfolio_html_report.py -q
```

Portfolio and multi-user isolation tests:

```bash
python -m pytest tests/test_portfolio_config.py tests/test_p2_watchlist_isolation.py -q
```

After Python changes, run at least syntax checks and relevant tests:

```bash
python -m compileall -q app_streamlit_multiuser.py multiuser_store.py stock_watch_list_back_end.py daily_report
python -m pytest path/to/relevant_test.py -q
```

## Current known limitations

- External data sources, model providers, search APIs, and SMTP services are affected by network conditions, cost, quotas, and provider behavior.
- Large market breadth calculations and large ticker lists can be slow. Market Breadth is manually triggered with `Refresh Breadth` to avoid automatic recalculation during app startup or tab switching.
- yfinance extended-hours data depends on provider availability; non-US or unsupported tickers may not show extended-hours cues.
- Portfolio uses latest FX rates for display and P/L conversion. It does not reconstruct historical FX cost basis and should not be treated as accounting or tax reporting.
- The Portfolio total row requires one buy currency per portfolio page. Mixed buy currencies hide total figures.
- Portfolio AI reports use the base currency for portfolio metrics, so mixed buy currencies do not block report generation; FX still uses latest rates rather than historical rates.
- Portfolio AI does not know cash balance, taxes, fees, or tax lots, so it does not provide exact share counts for trades.
- Synchronous AI report generation still exists; public deployments must add reverse-proxy rate limiting and capacity monitoring.
- SMTP cannot guarantee exactly-once delivery.
- HTML reports can be viewed offline but may be large because chart scripts are embedded.
- The project does not provide self-service signup, payments, investment advice, trade execution, or data-source SLA.

## License note

Unless otherwise stated, treat this project as a personal research tool. Before redistribution or deployment, verify the terms of the data sources, model providers, and email providers you use.

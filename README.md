# Stock Watch List

一个面向美股与跨市场观察的本地股票看板。项目提供两个前端：

- `app_tkinter.py`：Windows 桌面版，使用 Tkinter + tksheet + Matplotlib。
- `app_streamlit.py`：网页看板版，使用 Streamlit + Plotly。

两个前端都会在本地自动启动同一个 Flask 后端线程，后端通过 Yahoo Finance、StockAnalysis.com、CNN Fear & Greed、Alternative.me 等数据源获取数据，并用 SQLite 缓存价格数据（增量更新）、基本面数据与 Beta 数据。

> 本项目用于个人投资研究和数据观察，不构成投资建议。外部数据源可能限流、延迟、字段变更或返回空值。

## 主要功能

### 1. 股票观察列表

默认股票分组包括：

- `Mag7`
- `Chips/AI`
- `Fin/Crypto`
- `Health`
- `Energy`
- `Defense`
- `Consumer`
- `China`
- `Themes`

表格展示字段：

- `Ticker`
- `Price`
- `1D%`
- `5D%`
- `1M%`
- `YTD%`
- `Rel. Momentum`
- `Diff_EMA5%`
- `Diff_EMA10%`
- `Diff_EMA20%`
- `Diff_EMA50%`
- `Diff_EMA100%`
- `Diff_EMA200%`
- `Diff_BB_Up%`
- `Diff_BB_Low%`
- `Volume_Ratio`
- `Next Earnings`
- `Trailing PE`
- `Forward PE`
- `PEG Ratio`
- `Analysts`
- `Price Target`
- `Market Cap`

表格支持按分组插入标题行，并对涨跌幅、EMA 偏离、布林带位置、成交量比率、财报日期、分析师评级、目标价等字段进行颜色标记。后端返回的 `Beta` 不作为独立列显示，而是用于给 `Ticker` 单元格着色：Beta 高于 1 偏红，低于 1 偏绿。Tkinter 版使用 `tksheet`，Streamlit 版渲染自定义 HTML 表格。

### 2. 大盘与跨市场仪表盘

默认大盘分组包括：

- `Dashboard`：核心市场概览。
- `US Mkt Dir`：标普 500、纳指 100、道指、罗素 2000。
- `Breadth`：等权指数与纳指等权 ETF。
- `AI/Tech Risk`：科技风险偏好相关标的。
- `China Beta`：A 股、港股、中国科技相关标的。
- `Rates/FX`：美债收益率与外汇。
- `Fear/Vol`：波动率指数。
- `Safe Haven`：黄金、白银。
- `Oil/Geopol`：布伦特原油。
- `Crypto`：BTC、ETH。
- `Strat Resources`：战略资源相关标的。

### 3. 市场宽度

市场宽度模块会从 Wikipedia 获取最新 S&P 500 成分股列表，然后通过 Yahoo Finance 下载两年日线数据（确保 MA200 在一年图表范围内有效），计算：

- `20MA_Ratio`：复权收盘价高于 20 日均线的成分股比例。
- `50MA_Ratio`：复权收盘价高于 50 日均线的成分股比例。
- `200MA_Ratio`：复权收盘价高于 200 日均线的成分股比例。

结果会以表格和折线图展示，便于观察指数内部强弱。

### 4. K 线与技术分析

支持输入任意 Yahoo Finance 可识别的 ticker，绘制 K 线图。

支持周期（单位：天，决定 K 线图向前获取多少天的数据）：Tkinter 版和 Streamlit 版的“时间周期（天）”都是输入框，可以填写自定义整数天数。Streamlit 版当前限制为 `1` 到 `3650` 天，例如 `30`、`365`、`730` 等。

注意：对于 `5m`、`15m`、`1h`、`4h` 等日内间隔，后端也会按输入天数取数，但受 Yahoo Finance 可用范围限制，最多取最近 `60` 天。比如周期填 `10` 且间隔选 `15m`，会绘制最近约 10 天的 15 分钟 K 线；周期填 `120` 时则按 60 天封顶。

支持间隔：

- `1d`
- `1wk`
- `1h`
- `4h`
- `15m`
- `5m`

图表包含：

- K 线。
- 成交量。
- MA5、MA10、MA20、MA50、MA100、MA200。
- Bollinger Upper / Lower。
- MACD、Signal、Histogram。
- KDJ。
- RSI。
- 神奇九转（TD Sequential 简化版）。
- 最近约 30 天、4 小时间隔数据估算的筹码分布和筹码峰。
- StockAnalysis.com 快速链接。

Streamlit 版额外提供 Fibonacci Retracement / Extension 表单，可输入 A、B、C 三个价格点并在主图上绘制常用回撤与扩展水平。

Tkinter 版在 K 线窗口中使用 Matplotlib 工具栏，并支持在图上交互选择 Fibonacci 点位。

### 5. 恐惧与贪婪指数

应用顶部会展示：

- CNN Fear & Greed Index。
- Crypto Fear & Greed Index，数据来自 Alternative.me。

### 6. 基本面与分析师数据

后端优先从 StockAnalysis.com 抓取以下字段，并在缺失时回退到 yfinance 可用字段：

- Forward PE
- PEG Ratio
- Trailing PE
- Market Cap
- Earnings Date
- P/S Ratio
- P/B Ratio
- Analyst Consensus
- Price Target

注意：P/S 和 P/B 当前主要用于 K 线图标题中的财务信息展示，不在主表格列中显示。

### 7. 相对动量与 Beta

`Rel. Momentum` 使用 S&P 500 作为基准。后端从价格缓存中获取 `^GSPC` 过去两年日线数据，取约 3 个月、6 个月、12 个月对应的参考交易日，再将每只标的与这些参考日期对齐计算相对收益差：

```text
Rel. Momentum = 0.2 * M3M + 0.3 * M6M + 0.5 * M12M
```

其中 `M3M`、`M6M`、`M12M` 分别是标的相对 S&P 500 的 3 个月、6 个月、12 个月收益差。

Beta 使用标的与 `^GSPC` 的共同交易日收益率计算，窗口最多取最近 252 个交易日，并按美东日期缓存到 SQLite。标的与基准的价格数据均来自 `price_cache` 表；刷新时会批量读取和写入 Beta 缓存，避免逐 ticker 重复初始化 SQLite。

## 项目结构

```text
Stock_watch_list/
|-- app_tkinter.py
|   |-- Tkinter 桌面前端
|   |-- 自动启动本地 Flask 后端线程
|   |-- tksheet 三个表格页：Stocks、Broad Market、Market Breadth
|   |-- Matplotlib / mplfinance K 线图
|   |-- K 线窗口包含成交量、MACD、KDJ、RSI、神奇九转（TD Sequential 简化版）、筹码分布、Fibonacci
|
|-- app_streamlit.py
|   |-- Streamlit 网页前端
|   |-- 自动启动本地 Flask 后端线程
|   |-- 三个主标签页：Stocks、Broad Market、Market Breadth
|   |-- Plotly K 线图与市场宽度图
|   |-- st.cache_data 缓存前端 API 请求
|
|-- stock_watch_list_back_end.py
|   |-- Flask API 后端
|   |-- yfinance 数据下载（增量更新 + SQLite 缓存）
|   |-- StockAnalysis.com 数据缓存与回退逻辑
|   |-- SQLite 表：price_cache、stock_analysis_data、beta_cache
|   |-- 市场宽度、相对动量、Beta、K 线指标、恐惧贪婪指数接口
|
|-- stockanalysis_scraper.py
|   |-- StockAnalysis.com 抓取模块
|   |-- 并发抓取 Forward PE、PEG、Trailing PE、Market Cap、Earnings Date、P/S、P/B、Analyst、Price Target
|   |-- 纯抓取逻辑，不负责缓存
|
|-- qwen_forward_pe.py
|   |-- 旧版/备用 Forward PE 抓取与缓存脚本
|   |-- 使用 forward_pe_cache.db
|   |-- 当前主流程已由 stockanalysis_scraper.py + stock_watch_list_back_end.py 接管
|
|-- launch_tkinter.bat
|   |-- Windows Tkinter 一键启动脚本
|
|-- launch_streamlit.bat
|   |-- Windows Streamlit 一键启动脚本
|
|-- requirements.txt
|   |-- Python 依赖列表
|
|-- stock_cache.db
|   |-- SQLite 运行期缓存文件，首次运行后生成或更新
|
|-- .env
|   |-- 可选环境变量文件
|
|-- .gitignore
|-- README.md
```

## 技术架构

```text
Tkinter UI              Streamlit UI
app_tkinter.py          app_streamlit.py
     |                       |
     | local HTTP API        | local HTTP API
     +-----------+-----------+
                 |
                 v
      Flask Backend: stock_watch_list_back_end.py
                 |
     +-----------+-----------+----------------+
     |                       |                |
     v                       v                v
Yahoo Finance       StockAnalysis.com       External APIs
yfinance            scraper + SQLite        CNN F&G / Alternative.me
     |
     v
Price, volume, K-line, market breadth, technical indicators
```

两个前端默认访问：

```text
http://127.0.0.1:5000
```

Streamlit 默认运行在：

```text
http://localhost:8501
```

## 安装

### 1. 克隆项目

```bash
git clone https://github.com/cyp9313/Stock_watch_list.git
cd Stock_watch_list
```

### 2. 创建虚拟环境

Windows:

```bash
python -m venv .venv
.venv\Scripts\activate
```

macOS / Linux:

```bash
python -m venv .venv
source .venv/bin/activate
```

### 3. 安装依赖

```bash
pip install -r requirements.txt
```

依赖包括：

- `fear_and_greed`
- `Flask`
- `matplotlib`
- `mplfinance`
- `numpy`
- `pandas`
- `plotly`
- `python-dotenv`
- `pytz`
- `Requests`
- `requests_cache`
- `streamlit`
- `tksheet`
- `yfinance`
- `lxml`

## 启动方式

### Windows 一键启动

启动 Tkinter 桌面版：

```text
双击 launch_tkinter.bat
```

启动 Streamlit 网页版：

```text
双击 launch_streamlit.bat
```

`launch_streamlit.bat` 会在缺少 `.venv` 时自动创建虚拟环境、升级 pip、安装 `requirements.txt`，然后启动 Streamlit。

`launch_tkinter.bat` 会在缺少 `.venv` 时自动创建虚拟环境并安装依赖，然后启动桌面应用。

### 手动启动 Tkinter 版

```bash
python app_tkinter.py
```

### 手动启动 Streamlit 版

```bash
python -m streamlit run app_streamlit.py --server.port 8501 --server.address localhost --browser.gatherUsageStats false
```

### 单独启动后端

在开发模式下（默认），Tkinter 和 Streamlit 前端会自动启动 Flask 后端线程。若只想调试 API，可以单独运行：

```bash
python stock_watch_list_back_end.py
```

注意：如果你已经启动了 Tkinter 或 Streamlit 前端，它们通常会占用 `127.0.0.1:5000`。此时再单独启动后端可能出现端口占用。

### 生产部署

生产环境下应将前端和后端分别启动：

```bash
# 1. 启动后端
python stock_watch_list_back_end.py

# 2. 启动前端（不在自身进程中启动 Flask）
STOCK_DEV_MODE=0 STOCK_API_BASE_URL=http://127.0.0.1:5000 streamlit run app_streamlit_multiuser.py --server.port 8502 --server.headless true
```

环境变量：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `STOCK_API_BASE_URL` | `http://127.0.0.1:5000` | 后端 API 地址 |
| `STOCK_DEV_MODE` | `1` | `1` = 开发模式（前端自动启动 Flask）；`0` = 生产模式（前端仅连接已有后端） |

后端启动后可通过 `/api/health` 验证服务状态：

```bash
curl http://127.0.0.1:5000/api/health
# {"service":"stock-watchlist-api","status":"ok","version":"1.0"}
```

## 使用说明

### Tkinter 桌面版

启动后会自动加载：

- 股票观察列表。
- 大盘指标。
- 市场宽度。
- CNN Fear & Greed Index。
- Crypto Fear & Greed Index。

底部按钮：

- `Refresh Stocks`：刷新股票与大盘数据。
- `Refresh Breadth`：刷新市场宽度数据。

Tkinter 版刷新会在后台线程中执行，按钮会在对应刷新任务运行期间临时禁用，数据返回后再回到主线程更新表格；启动时会先刷新股票与大盘数据，完成后自动触发市场宽度刷新。

K 线输入区：

- 股票代码：例如 `AAPL`。
- 时间周期：例如 `365`。
- 时间间隔：例如 `1d`、`1wk`、`1h`、`4h`、`15m`、`5m`。
- 点击“绘制K线图”打开图表窗口。

### Streamlit 网页版

侧边栏：

- `Refresh Stocks`：刷新股票与大盘数据。
- `Refresh Breadth`：刷新市场宽度数据。
- 显示当前页面刷新时间。

主页面：

- 顶部显示 CNN 与 Crypto Fear & Greed。
- `Stocks` 标签页显示股票观察列表。
- `Broad Market` 标签页显示大盘与跨市场指标。
- `Market Breadth` 标签页显示 S&P 500 与 Nasdaq 100 的 6 行宽度表格、两张宽度曲线图，以及两张按市值加权的 treemap。
- 页面下方 `K-Line Chart` 区域用于绘制单个 ticker 的 K 线图。

Fibonacci：

1. 先输入 ticker、周期、间隔并点击 `Plot`。
2. 在 `Fibonacci Retracement / Extension` 区域输入 A、B、C 价格点。
3. 点击 `Calculate Fibonacci` 后，图表会显示回撤与扩展水平。
4. 点击 `Clear Fibonacci` 清除线条。

## 自定义 Watch List

项目当前没有单独的配置文件。股票分组与大盘分组分别写在两个前端文件中。若你想让两个前端显示一致，需要同时修改两个文件。

### Tkinter 版

编辑 `app_tkinter.py`：

```python
stock_groups = {
    "Mag7": ["AAPL", "MSFT", "GOOG", "AMZN", "META", "TSLA", "NVDA", "SPCX"],
    "Chips/AI": ["MU", "ORCL", "AMD", "INTC", "AVGO", "SMCI", "PLTR", "RGTI", "DXYZ", "SNPS", "APP"],
}
```

大盘分组：

```python
broad_market_groups = {
    "Dashboard": ["^GSPC", "^NDX", "RSP", "QQQE", "^TNX", "EURUSD=X", "^VIX", "GC=F", "BZ=F", "BTC-USD", "510300.SS"],
}
```

### Streamlit 版

编辑 `app_streamlit.py`：

```python
STOCK_GROUPS = {
    "Mag7": ["AAPL", "MSFT", "GOOG", "AMZN", "META", "TSLA", "NVDA", "SPCX"],
    "Chips/AI": ["MU", "ORCL", "AMD", "INTC", "AVGO", "SMCI", "PLTR", "RGTI", "DXYZ", "SNPS", "APP"],
}
```

大盘分组：

```python
BROAD_MARKET_GROUPS = {
    "Dashboard": ["^GSPC", "^NDX", "RSP", "QQQE", "^TNX", "EURUSD=X", "^VIX", "GC=F", "BZ=F", "BTC-USD", "510300.SS"],
}
```

### 修改示例

新增一个新能源分组：

```python
"New Energy": ["TSLA", "NIO", "LI", "XPEV"],
```

修改后需要重启应用才能生效。

## Ticker 格式

项目内部使用 Yahoo Finance ticker 格式，并会在访问 StockAnalysis.com 时自动转换为对应的 StockAnalysis URL 格式。

常见示例：

- 美股：`AAPL`、`MSFT`、`NVDA`
- 美股特殊代码：`BRK-B`
- 指数：`^GSPC`、`^NDX`、`^DJI`、`^RUT`
- 外汇：`EURUSD=X`、`EURCNY=X`
- 商品期货：`GC=F`、`SI=F`、`BZ=F`
- 加密货币：`BTC-USD`、`ETH-USD`
- A 股 / ETF：`510300.SS`、`159915.SZ`
- 港股：`3033.HK`
- 欧洲市场：`WNUC.DE`

StockAnalysis.com 的代码格式与 yfinance 不同，后端会自动转换常见市场：

- `600519.SS` → `https://stockanalysis.com/quote/sha/600519/`
- `300750.SZ` → `https://stockanalysis.com/quote/she/300750/`
- `0700.HK` / `700.HK` → `https://stockanalysis.com/quote/hkg/0700/`
- `SAP.DE` → `https://stockanalysis.com/quote/etr/SAP/`

K 线输入也支持部分 StockAnalysis 风格代码，例如 `HKG:0700`、`SHA:600519`、`SHE:300750`、`ETR:SAP`，后端会先转换为 yfinance 代码再取价。

StockAnalysis.com 抓取会优先使用普通股票的 `statistics` 页；ETF 或没有 `statistics` 页的标的会回退到 Overview 页读取 `PE Ratio` 等字段。后端会跳过指数、加密货币、商品、外汇和市场宽度伪 ticker。

## 后端 API

后端默认监听：

```text
http://127.0.0.1:5000
```

### GET `/api/stock_data`

获取股票、大盘和跨市场指标表格数据。

查询参数：

- `groups`：JSON 字符串，格式为 `{group_name: [ticker, ...]}`。
- `broad_market_tickers`：JSON 字符串，用于告诉后端哪些 ticker 属于大盘/跨市场指标，避免对它们抓取 StockAnalysis 基本面数据。

返回：

- `success`
- `data`

### POST `/api/breadth_data`

获取市场宽度数据。

表单参数：

- `sp500_symbols`：JSON 字符串，S&P 500 成分股 ticker 列表。
- `nasdaq100_symbols`：JSON 字符串，Nasdaq 100 成分股 ticker 列表。后端会将两组 ticker 合并去重后只下载一次价格数据。

返回：

- `success`
- `data`
- `breadth_chart_data`
- `breadth_treemap_data`
- `nasdaq100_data`
- `nasdaq100_breadth_chart_data`
- `nasdaq100_breadth_treemap_data`
- `breadth_universe_counts`

### GET `/api/kline_data`

获取单个 ticker 的 K 线、技术指标、筹码分布和财务信息。

查询参数：

- `ticker`
- `period`
- `interval`

返回：

- `success`
- `ticker`
- `dates`
- `ohlc`
- `indicators`
- `financials`

### GET `/api/fear_greed`

获取 CNN Fear & Greed Index。

返回：

- `success`
- `value`
- `description`

### GET `/api/fear_greed_crypto`

获取 Crypto Fear & Greed Index。

返回：

- `success`
- `value`
- `description`

### POST `/api/sp500_symbols`

当前代码中保留的实验/辅助接口，读取表单中的 `sp500_symbols` JSON。主流程市场宽度使用 `/api/breadth_data`。

## 缓存与生成文件

### `stock_cache.db`

主后端使用的 SQLite 缓存文件，包含三张表：

- `price_cache`：统一存储所有标的（watchlist 股票 + 宽基指数 + S&P 500 breadth 成分股）的日线 **Adj Close 和 Volume** 数据（OHL/Close 为冗余数据不保存）。主键为 `(ticker, date)`，使用 `INSERT OR REPLACE` 合并增量数据。
- `stock_analysis_data`：按 ticker 和美东日期缓存 StockAnalysis.com 基本面与分析师数据。
- `beta_cache`：按 ticker 和美东日期缓存 Beta。

#### 增量更新机制

价格数据采用增量更新策略，避免每次刷新都全量下载 2 年历史数据：

1. **3-way delta reconciliation**：每次请求时，将 DB 中已有的标的与当前请求的标的做集合比较：
   - `existing`（DB ∩ 请求）→ 增量下载最近几天，与缓存合并
   - `new`（请求 - DB）→ 全量下载 2 年数据
   - `stale`（DB - 请求）→ 默认不删除（避免 `/api/stock_data` 和 `/api/breadth_data` 互删对方数据）；旧数据由 750 天滚动窗口自动清理

2. **增量下载周期由 gap 决定**（均为自然日，用 `gap+2` 留 2 天缓冲，防止周末缺口）：
   - gap ≤ 0 天 → 下载 `2d`，用于同一交易日内再次刷新时获取盘中最新价
   - 1 ≤ gap ≤ 30 天 → 下载 `{gap+2}d`（例：gap=1→3d，gap=6→8d，确保覆盖最新交易日）
   - > 30 天 → 全量重新下载 `2y`

   **注意**：早期版本对 `gap ≤ 7` 天固定用 `5d`，但当 gap=6（今天周四，缓存最新上周五）时，`5d` 只取最近 5 个自然日（周日~周四），上周五的数据（距今天 6 天）会被遗漏。现统一为 `{gap+2}d` 修复此问题。

   对大量标的（例如 S&P 500 市场宽度）进行增量更新时，后端会将 yfinance 下载拆成批次执行并逐批写入缓存，避免一次性请求过多 ticker 导致响应长时间卡住。

3. **滚动窗口清理**：每次 `init_db()` 时自动执行：
   - `price_cache`：保留最近 750 个自然日（≈517 交易日）。MA200 需 200 交易日 warmup + 1 年图表 252 交易日 = 452 交易日最低需求；750 自然日 ≈ 517 交易日，留有 ~65 交易日余量
   - `stock_analysis_data` / `beta_cache`：保留最近 90 天

4. **SQLite 优化**：启用 WAL 模式提升并发读写；设置 `auto_vacuum = INCREMENTAL` 让被删除的页面可复用。稳态下插入与删除平衡，数据库文件大小保持恒定。

删除 `stock_cache.db` 后重启应用，会重新抓取和计算缓存数据。

### Streamlit 前端缓存

`app_streamlit.py` 使用 `st.cache_data`：

- 股票数据缓存约 300 秒。
- Fear & Greed 缓存约 600 秒。
- 市场宽度缓存约 600 秒。
- K 线数据缓存约 60 秒。
- S&P 500 成分股列表缓存约 3600 秒。

点击侧边栏 `Refresh Stocks` 可清空股票数据缓存，点击 `Refresh Breadth` 可清空市场宽度数据缓存。

### `forward_pe_cache.db`

这是 `qwen_forward_pe.py` 旧版/备用脚本使用的缓存文件。当前主应用流程不依赖它。

## 环境变量

项目会加载 `.env`，但当前主流程没有强制要求配置环境变量。README 旧版中提到的 `FLASK_ENV`、`FLASK_PORT` 不会自动改变前端内置的后端端口；当前两个前端都硬编码访问 `127.0.0.1:5000`。

如需修改端口，需要同步修改：

- `app_tkinter.py` 中的 Flask 启动端口和 `API_BASE_URL`。
- `app_streamlit.py` 中的 Flask 启动端口和 `API_BASE`。

## 常见问题

### 首次启动很慢

首次启动（或 `stock_cache.db` 不存在时）会全量下载所有标的的 2 年价格数据、S&P 500 市场宽度数据，并抓取 StockAnalysis.com 基本面字段。等待时间取决于网络和外部数据源响应速度。

后续启动时，已缓存的标的只需增量下载最近几天数据，响应速度会显著提升。仅当某标的距上次缓存超过 30 天（如长时间未启动程序）时，才会触发该标的的全量重新下载。

### Streamlit 或 Tkinter 报后端连接失败

检查 `127.0.0.1:5000` 是否被其他进程占用。两个前端都会自动启动 Flask 后端，如果同一时间重复启动多个实例，可能出现端口冲突。

### 市场宽度加载失败

市场宽度依赖 Wikipedia 的 S&P 500 成分股表和 Yahoo Finance 批量下载。如果其中任一数据源不可访问，可能返回空数据或失败。

### 某些 ticker 没有基本面数据

StockAnalysis.com 不一定支持所有 ticker。后端会尽量回退到 yfinance；如果两个来源都缺失，对应字段会为空。

### 如何清空缓存

关闭应用后删除：

```text
stock_cache.db
```

然后重新启动应用。这会清空所有价格缓存、基本面缓存和 Beta 缓存，下次启动时重新全量下载。

### 是否支持美股以外的市场

支持 Yahoo Finance 可识别的 ticker，例如 A 股 ETF、港股、外汇、商品、加密货币等。但基本面数据抓取主要面向普通股票，部分跨市场 ticker 会跳过 StockAnalysis.com 查询。

## 开发注意事项

- 两个前端的分组配置是重复维护的；修改 watch list 时建议同步修改。
- `requests_cache.uninstall_cache()` 当前会禁用 requests_cache 的全局缓存；主缓存逻辑以 SQLite 为准。
- `stockanalysis_scraper.py` 使用正则从 StockAnalysis.com 页面提取字段，页面结构变化时可能需要更新解析规则。
- `stock_cache.db` 是运行期数据，不建议作为代码变更提交。
- 源码中部分历史中文注释存在编码乱码，但运行逻辑以 Python 代码为准。
- 价格数据使用 `price_cache` 表统一缓存并增量更新；新增标的自动全量下载，旧标的由 750 天滚动窗口自动清理，无需手动干预。

## License

MIT License

## Author

cyp9313

---

# English Version

# Stock Watch List

A local stock watchlist and market dashboard for US equities and cross-market monitoring. The project provides two frontends:

- `app_tkinter.py`: desktop app built with Tkinter, tksheet, and Matplotlib.
- `app_streamlit.py`: web dashboard built with Streamlit and Plotly.

Both frontends automatically start the same local Flask backend thread. The backend fetches data from Yahoo Finance, StockAnalysis.com, CNN Fear & Greed, and Alternative.me, and caches price data (with incremental updates), fundamental data, and Beta data in SQLite.

> This project is intended for personal market research and data observation only. It is not investment advice. External data sources may be rate-limited, delayed, structurally changed, or return missing values.

## Features

### 1. Stock Watchlist

Default stock groups:

- `Mag7`
- `Chips/AI`
- `Fin/Crypto`
- `Health`
- `Energy`
- `Defense`
- `Consumer`
- `China`
- `Themes`

Displayed table columns:

- `Ticker`
- `Price`
- `1D%`
- `5D%`
- `1M%`
- `YTD%`
- `Rel. Momentum`
- `Diff_EMA5%`
- `Diff_EMA10%`
- `Diff_EMA20%`
- `Diff_EMA50%`
- `Diff_EMA100%`
- `Diff_EMA200%`
- `Diff_BB_Up%`
- `Diff_BB_Low%`
- `Volume_Ratio`
- `Next Earnings`
- `Trailing PE`
- `Forward PE`
- `PEG Ratio`
- `Analysts`
- `Price Target`
- `Market Cap`

The table inserts group header rows and applies color highlights to price changes, EMA deviations, Bollinger Band position, volume ratio, earnings date, analyst rating, price target, and related fields. The backend also returns `Beta`; it is not displayed as a standalone column, but is used to color the `Ticker` cell: Beta above 1 leans red, and Beta below 1 leans green.

### 2. Broad Market Dashboard

Default broad-market groups:

- `Dashboard`: core market overview.
- `US Mkt Dir`: S&P 500, Nasdaq 100, Dow Jones, and Russell 2000.
- `Breadth`: equal-weight market proxies.
- `AI/Tech Risk`: technology risk-appetite tickers.
- `China Beta`: China-related ETFs and market proxies.
- `Rates/FX`: yields and foreign exchange.
- `Fear/Vol`: volatility indexes.
- `Safe Haven`: gold and silver.
- `Oil/Geopol`: Brent crude oil.
- `Crypto`: BTC and ETH.
- `Strat Resources`: strategic-resource-related tickers.

### 3. Market Breadth

The market breadth module fetches the latest S&P 500 and Nasdaq 100 constituents, downloads two years of daily data from Yahoo Finance with a de-duplicated combined ticker universe (ensuring MA200 is valid across the one-year chart range), and calculates:

- `20MA_Ratio`: percentage of constituents with adjusted closing price above their 20-day moving average.
- `50MA_Ratio`: percentage of constituents with adjusted closing price above their 50-day moving average.
- `200MA_Ratio`: percentage of constituents with adjusted closing price above their 200-day moving average.

The Streamlit Market Breadth page keeps both universes on one page: a single grouped table with six rows, two side-by-side breadth charts, then S&P 500 and Nasdaq 100 treemaps. Nasdaq 100 constituents use Wikipedia first and a GitHub raw CSV fallback if Wikipedia is unavailable; sector metadata falls back to S&P 500 overlap metadata and yfinance when needed.

Results are displayed as both a table and a line chart.

### 4. Candlestick And Technical Analysis

You can input any ticker recognized by Yahoo Finance and plot a candlestick chart.

Supported periods (unit: days; controls how far back the K-line chart fetches data): in both the Tkinter and Streamlit versions, `Period (days)` is an input field where you can enter a custom integer day count. The Streamlit version currently limits the value to `1` through `3650` days, for example `30`, `365`, or `730`.

Note: for intraday intervals such as `5m`, `15m`, `1h`, and `4h`, the backend also uses the entered day count, but caps it at the most recent `60` days because of Yahoo Finance availability. For example, period `10` with interval `15m` plots about 10 days of 15-minute candles; period `120` is capped to 60 days.

Supported intervals:

- `1d`
- `1wk`
- `1h`
- `4h`
- `15m`
- `5m`

The chart includes:

- Candlesticks.
- Volume.
- MA5, MA10, MA20, MA50, MA100, MA200.
- Bollinger Upper / Lower.
- MACD, Signal, Histogram.
- KDJ.
- RSI.
- Simplified TD Sequential, also known in Chinese trading contexts as Shenqi Jiuzhuan / 神奇九转.
- Chip distribution and chip peak estimated from recent 30-day, 4-hour data.
- StockAnalysis.com quick link.

The Streamlit version also provides a Fibonacci Retracement / Extension form where you can enter A, B, and C price points and draw common retracement and extension levels on the main chart.

The Tkinter version uses the Matplotlib toolbar in the K-line window and supports interactive Fibonacci point selection on the chart.

### 5. Fear And Greed Indexes

The top area displays:

- CNN Fear & Greed Index.
- Crypto Fear & Greed Index from Alternative.me.

### 6. Fundamentals And Analyst Data

The backend first attempts to scrape the following fields from StockAnalysis.com, and falls back to yfinance when possible:

- Forward PE
- PEG Ratio
- Trailing PE
- Market Cap
- Earnings Date
- P/S Ratio
- P/B Ratio
- Analyst Consensus
- Price Target

Note: P/S and P/B are currently used mainly in the K-line chart title and are not displayed as main table columns.

### 7. Relative Momentum And Beta

`Rel. Momentum` uses the S&P 500 as the benchmark. The backend retrieves two years of daily `^GSPC` data from the price cache, picks reference trading dates for approximately 3 months, 6 months, and 12 months, and aligns each ticker to those dates to calculate relative return differences:

```text
Rel. Momentum = 0.2 * M3M + 0.3 * M6M + 0.5 * M12M
```

`M3M`, `M6M`, and `M12M` are the ticker's return differences relative to the S&P 500 over the 3-month, 6-month, and 12-month windows.

Beta is calculated from common trading-day returns between the ticker and `^GSPC`, using up to the most recent 252 trading days, and is cached in SQLite by US Eastern date. Both ticker and benchmark price data come from the `price_cache` table; refreshes batch-read and batch-write the Beta cache to avoid repeated SQLite initialization per ticker.

## Project Structure

```text
Stock_watch_list/
|-- app_tkinter.py
|   |-- Tkinter desktop frontend
|   |-- Automatically starts the local Flask backend thread
|   |-- Three tksheet tabs: Stocks, Broad Market, Market Breadth
|   |-- Matplotlib / mplfinance K-line chart
|   |-- K-line window with volume, MACD, KDJ, RSI, simplified TD Sequential / Shenqi Jiuzhuan, chip distribution, Fibonacci
|
|-- app_streamlit.py
|   |-- Streamlit web frontend
|   |-- Automatically starts the local Flask backend thread
|   |-- Three main tabs: Stocks, Broad Market, Market Breadth
|   |-- Plotly K-line chart and market breadth chart
|   |-- Uses st.cache_data for frontend API caching
|
|-- stock_watch_list_back_end.py
|   |-- Flask API backend
|   |-- yfinance data download (incremental update + SQLite cache)
|   |-- StockAnalysis.com data cache and fallback logic
|   |-- SQLite tables: price_cache, stock_analysis_data, beta_cache
|   |-- Market breadth, relative momentum, Beta, K-line indicators, and Fear & Greed APIs
|
|-- stockanalysis_scraper.py
|   |-- StockAnalysis.com scraper
|   |-- Concurrently fetches Forward PE, PEG, Trailing PE, Market Cap, Earnings Date, P/S, P/B, Analyst, Price Target
|   |-- Scraping only; caching is handled by the backend
|
|-- qwen_forward_pe.py
|   |-- Legacy / backup Forward PE scraper and cache script
|   |-- Uses forward_pe_cache.db
|   |-- Current main flow is handled by stockanalysis_scraper.py + stock_watch_list_back_end.py
|
|-- launch_tkinter.bat
|   |-- Windows one-click Tkinter launcher
|
|-- launch_streamlit.bat
|   |-- Windows one-click Streamlit launcher
|
|-- requirements.txt
|   |-- Python dependencies
|
|-- stock_cache.db
|   |-- Runtime SQLite cache file, generated or updated after launch
|
|-- .env
|   |-- Optional environment variable file
|
|-- .gitignore
|-- README.md
```

## Architecture

```text
Tkinter UI              Streamlit UI
app_tkinter.py          app_streamlit.py
     |                       |
     | local HTTP API        | local HTTP API
     +-----------+-----------+
                 |
                 v
      Flask Backend: stock_watch_list_back_end.py
                 |
     +-----------+-----------+----------------+
     |                       |                |
     v                       v                v
Yahoo Finance       StockAnalysis.com       External APIs
yfinance            scraper + SQLite        CNN F&G / Alternative.me
     |
     v
Price, volume, K-line, market breadth, technical indicators
```

Both frontends access the backend at:

```text
http://127.0.0.1:5000
```

Streamlit runs at:

```text
http://localhost:8501
```

## Installation

### 1. Clone The Project

```bash
git clone https://github.com/cyp9313/Stock_watch_list.git
cd Stock_watch_list
```

### 2. Create A Virtual Environment

Windows:

```bash
python -m venv .venv
.venv\Scripts\activate
```

macOS / Linux:

```bash
python -m venv .venv
source .venv/bin/activate
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

Dependencies include:

- `fear_and_greed`
- `Flask`
- `matplotlib`
- `mplfinance`
- `numpy`
- `pandas`
- `plotly`
- `python-dotenv`
- `pytz`
- `Requests`
- `requests_cache`
- `streamlit`
- `tksheet`
- `yfinance`
- `lxml`

## Launch

### Windows One-Click Launch

Launch the Tkinter desktop version:

```text
Double-click launch_tkinter.bat
```

Launch the Streamlit web version:

```text
Double-click launch_streamlit.bat
```

`launch_streamlit.bat` creates `.venv`, upgrades pip, installs `requirements.txt`, and starts Streamlit when the virtual environment is missing.

`launch_tkinter.bat` creates `.venv`, installs dependencies, and starts the desktop app when the virtual environment is missing.

### Manual Tkinter Launch

```bash
python app_tkinter.py
```

### Manual Streamlit Launch

```bash
python -m streamlit run app_streamlit.py --server.port 8501 --server.address localhost --browser.gatherUsageStats false
```

### Start Backend Separately

Usually this is not needed because both frontends automatically start the Flask backend thread. For API debugging only, run:

```bash
python stock_watch_list_back_end.py
```

If Tkinter or Streamlit is already running, `127.0.0.1:5000` may already be occupied.

## Usage

### Tkinter Desktop Version

After launch, the app automatically loads:

- Stock watchlist.
- Broad market indicators.
- Market breadth.
- CNN Fear & Greed Index.
- Crypto Fear & Greed Index.

Bottom buttons:

- `Refresh Stocks`: refresh stock and broad-market data.
- `Refresh Breadth`: refresh market breadth data.

In the Tkinter version, refresh tasks run in background threads. The corresponding button is temporarily disabled while its task is running, and table updates are applied back on the Tkinter main thread. On startup, the app refreshes stock and broad-market data first, then automatically starts the market breadth refresh.

K-line input area:

- Ticker: for example `AAPL`.
- Period: for example `365`.
- Interval: for example `1d`, `1wk`, `1h`, `4h`, `15m`, `5m`.
- Click the chart button to open the K-line window.

### Streamlit Web Version

Sidebar:

- `Refresh Stocks`: refresh stock and broad-market data.
- `Refresh Breadth`: refresh market breadth data.
- Shows current page refresh time.

Main page:

- Top area shows CNN and Crypto Fear & Greed.
- `Stocks` tab shows the stock watchlist.
- `Broad Market` tab shows broad-market and cross-market indicators.
- `Market Breadth` tab shows the S&P 500 and Nasdaq 100 breadth table, two breadth charts, and two market-cap weighted treemaps.
- The `K-Line Chart` section at the bottom plots a single ticker.

Fibonacci:

1. Enter ticker, period, interval, and click `Plot`.
2. Enter A, B, and C price points in `Fibonacci Retracement / Extension`.
3. Click `Calculate Fibonacci` to draw retracement and extension levels.
4. Click `Clear Fibonacci` to remove them.

## Customize Watch List

There is no separate configuration file at the moment. Stock groups and broad-market groups are defined in both frontend files. To keep both versions consistent, update both files.

### Tkinter Version

Edit `app_tkinter.py`:

```python
stock_groups = {
    "Mag7": ["AAPL", "MSFT", "GOOG", "AMZN", "META", "TSLA", "NVDA", "SPCX"],
    "Chips/AI": ["MU", "ORCL", "AMD", "INTC", "AVGO", "SMCI", "PLTR", "RGTI", "DXYZ", "SNPS", "APP"],
}
```

Broad-market groups:

```python
broad_market_groups = {
    "Dashboard": ["^GSPC", "^NDX", "RSP", "QQQE", "^TNX", "EURUSD=X", "^VIX", "GC=F", "BZ=F", "BTC-USD", "510300.SS"],
}
```

### Streamlit Version

Edit `app_streamlit.py`:

```python
STOCK_GROUPS = {
    "Mag7": ["AAPL", "MSFT", "GOOG", "AMZN", "META", "TSLA", "NVDA", "SPCX"],
    "Chips/AI": ["MU", "ORCL", "AMD", "INTC", "AVGO", "SMCI", "PLTR", "RGTI", "DXYZ", "SNPS", "APP"],
}
```

Broad-market groups:

```python
BROAD_MARKET_GROUPS = {
    "Dashboard": ["^GSPC", "^NDX", "RSP", "QQQE", "^TNX", "EURUSD=X", "^VIX", "GC=F", "BZ=F", "BTC-USD", "510300.SS"],
}
```

### Example

Add a new energy group:

```python
"New Energy": ["TSLA", "NIO", "LI", "XPEV"],
```

Restart the app after editing groups.

## Ticker Format

The project uses Yahoo Finance ticker format internally, and automatically converts tickers to the corresponding StockAnalysis.com URL format when scraping fundamentals.

Common examples:

- US stocks: `AAPL`, `MSFT`, `NVDA`
- Special US tickers: `BRK-B`
- Indexes: `^GSPC`, `^NDX`, `^DJI`, `^RUT`
- FX: `EURUSD=X`, `EURCNY=X`
- Commodity futures: `GC=F`, `SI=F`, `BZ=F`
- Crypto: `BTC-USD`, `ETH-USD`
- China A-share / ETF: `510300.SS`, `159915.SZ`
- Hong Kong stocks: `3033.HK`
- European market: `WNUC.DE`

Common StockAnalysis.com conversions:

- `600519.SS` → `https://stockanalysis.com/quote/sha/600519/`
- `300750.SZ` → `https://stockanalysis.com/quote/she/300750/`
- `0700.HK` / `700.HK` → `https://stockanalysis.com/quote/hkg/0700/`
- `SAP.DE` → `https://stockanalysis.com/quote/etr/SAP/`

The K-line input also accepts some StockAnalysis-style tickers, such as `HKG:0700`, `SHA:600519`, `SHE:300750`, and `ETR:SAP`; the backend converts them to yfinance format before fetching prices.

StockAnalysis.com scraping is mainly suitable for ordinary stock tickers. The backend skips indexes, cryptocurrencies, commodities, FX, and market-breadth pseudo tickers.

## Backend API

The backend listens at:

```text
http://127.0.0.1:5000
```

### GET `/api/stock_data`

Fetch stock, broad-market, and cross-market table data.

Query parameters:

- `groups`: JSON string in `{group_name: [ticker, ...]}` format.
- `broad_market_tickers`: JSON string used to tell the backend which tickers belong to broad-market / cross-market groups, so it can skip StockAnalysis fundamental scraping for them.

Returns:

- `success`
- `data`

### POST `/api/breadth_data`

Fetch market breadth data.

Form parameter:

- `sp500_symbols`: JSON string containing S&P 500 constituent tickers.
- `nasdaq100_symbols`: JSON string containing Nasdaq 100 constituent tickers. The backend merges both universes and downloads each unique ticker only once.

Returns:

- `success`
- `data`
- `breadth_chart_data`
- `breadth_treemap_data`
- `nasdaq100_data`
- `nasdaq100_breadth_chart_data`
- `nasdaq100_breadth_treemap_data`
- `breadth_universe_counts`

### GET `/api/kline_data`

Fetch candlestick data, technical indicators, chip distribution, and financial information for one ticker.

Query parameters:

- `ticker`
- `period`
- `interval`

Returns:

- `success`
- `ticker`
- `dates`
- `ohlc`
- `indicators`
- `financials`

### GET `/api/fear_greed`

Fetch CNN Fear & Greed Index.

Returns:

- `success`
- `value`
- `description`

### GET `/api/fear_greed_crypto`

Fetch Crypto Fear & Greed Index.

Returns:

- `success`
- `value`
- `description`

### POST `/api/sp500_symbols`

An experimental / auxiliary endpoint retained in the current code. It reads `sp500_symbols` JSON from the form body. The main market breadth flow uses `/api/breadth_data`.

## Cache And Generated Files

### `stock_cache.db`

Main SQLite cache used by the backend. It contains three tables:

- `price_cache`: unified storage for daily **Adj Close and Volume** data of all tickers (watchlist stocks + broad-market indexes + S&P 500 breadth constituents). OHL/Close are redundant and not stored. Primary key is `(ticker, date)`; uses `INSERT OR REPLACE` to merge incremental data.
- `stock_analysis_data`: caches StockAnalysis.com fundamentals and analyst data by ticker and US Eastern date.
- `beta_cache`: caches Beta by ticker and US Eastern date.

#### Incremental Update Mechanism

Price data uses an incremental update strategy to avoid re-downloading 2 years of history on every refresh:

1. **3-way delta reconciliation**: On each request, the backend compares DB tickers with the currently requested tickers:
   - `existing` (DB ∩ request) → download only the latest few days and merge with cache
   - `new` (request - DB) → full 2-year download
   - `stale` (DB - request) → not deleted by default (avoids `/api/stock_data` and `/api/breadth_data` deleting each other's data); old data is cleaned up by the 750-day rolling window

2. **Incremental download period is determined by the gap** (both are calendar days, using `gap+2` to add a 2-day buffer to prevent weekend gaps):
   - gap ≤ 0 days → download `2d`, so same-day refreshes can still capture intraday price updates
   - 1 ≤ gap ≤ 30 days → download `{gap+2}d` (e.g., gap=1→3d, gap=6→8d, ensuring the latest trading day is covered)
   - > 30 days → full re-download with `2y`

   **Note**: An earlier version used a fixed `5d` for `gap ≤ 7 days`. When gap=6 (today is Thursday, cache latest is last Friday), `5d` only fetches the last 5 calendar days (Sunday~Thursday), and last Friday's data (6 days ago) would be missed. Now unified to `{gap+2}d` to fix this issue.

   For large ticker sets such as S&P 500 market breadth, the backend splits yfinance downloads into batches and writes each batch into SQLite, avoiding long stalls from sending too many tickers in one request.

3. **Rolling window cleanup**: Executed automatically on each `init_db()` call:
   - `price_cache`: retains the most recent 750 calendar days (≈517 trading days). MA200 requires 200 trading days warmup + 1-year chart needs 252 trading days = 452 trading days minimum; 750 calendar days ≈ 517 trading days, providing ~65 trading days of buffer
   - `stock_analysis_data` / `beta_cache`: retains the most recent 90 days

4. **SQLite optimization**: WAL mode enabled for concurrent read/write; `auto_vacuum = INCREMENTAL` allows freed pages to be reused. In steady state, insert/delete is balanced and the database file size remains constant.

Delete `stock_cache.db` and restart the app to refetch and recalculate cached data.

### Streamlit Frontend Cache

`app_streamlit.py` uses `st.cache_data`:

- Stock data: about 300 seconds.
- Fear & Greed: about 600 seconds.
- Market breadth: about 600 seconds.
- K-line data: about 60 seconds.
- S&P 500 constituent list: about 3600 seconds.

Click `Refresh Stocks` in the sidebar to clear stock data cache, or `Refresh Breadth` to clear market breadth data cache.

### `forward_pe_cache.db`

This is the cache file used by the legacy / backup `qwen_forward_pe.py` script. The current main application flow does not depend on it.

## Environment Variables

The project loads `.env`, but the current main flow does not require environment variables. The previously mentioned `FLASK_ENV` and `FLASK_PORT` do not automatically change the frontend's built-in backend port. Both frontends currently access `127.0.0.1:5000` directly in code.

To change the port, update both:

- Flask startup port and `API_BASE_URL` in `app_tkinter.py`.
- Flask startup port and `API_BASE` in `app_streamlit.py`.

## FAQ

### First Launch Is Slow

The first launch (or when `stock_cache.db` does not exist) downloads 2 years of price data for all tickers, S&P 500 market breadth data, and StockAnalysis.com fundamental fields. Waiting time depends on network quality and external data-source response speed.

On subsequent launches, cached tickers only need an incremental download of the latest few days, significantly improving response speed. A full re-download for a specific ticker is only triggered when it has been more than 30 days since the last cache update (e.g., after a long period without launching the app).

### Streamlit Or Tkinter Cannot Connect To Backend

Check whether `127.0.0.1:5000` is occupied by another process. Both frontends automatically start the Flask backend, so launching multiple instances at the same time can cause port conflicts.

### Market Breadth Fails To Load

Market breadth depends on constituent-list sources and Yahoo Finance batch download. S&P 500 uses Wikipedia with a DataHub GitHub CSV fallback; Nasdaq 100 uses Wikipedia with a GitHub raw CSV fallback. If Yahoo Finance is unavailable, breadth calculation may return empty data or fail.

### Some Tickers Have No Fundamental Data

StockAnalysis.com does not support every ticker. The backend tries to fall back to yfinance. If both sources are missing data, the corresponding fields remain empty.

### Clear Cache

Close the app and delete:

```text
stock_cache.db
```

Then restart the app. This clears all price cache, fundamental cache, and Beta cache; the next launch will perform a full re-download.

### Markets Outside US Stocks

The app supports tickers recognized by Yahoo Finance, such as China A-share ETFs, Hong Kong stocks, FX, commodities, and cryptocurrencies. Fundamental scraping tries the StockAnalysis `statistics` page first for ordinary stocks; ETFs or tickers without a `statistics` page fall back to the Overview page for fields such as `PE Ratio`. Some cross-market tickers are skipped for StockAnalysis.com queries.

## Development Notes

- Group configuration is duplicated across the two frontends; update both when changing the watch list.
- `requests_cache.uninstall_cache()` currently disables global requests_cache behavior; the main cache logic is SQLite-based.
- `stockanalysis_scraper.py` extracts fields from StockAnalysis.com pages with regular expressions; parsing rules may need updates if the page structure changes.
- `stock_cache.db` is runtime data and should not be committed as a code change.
- Some historical Chinese comments in source files may show encoding artifacts, but runtime behavior follows the Python code.
- Price data is cached and incrementally updated in the `price_cache` table; new tickers are automatically downloaded in full, and old tickers are cleaned up by the 750-day rolling window without manual intervention.

## Multi-user Streamlit Frontend

`app_streamlit_multiuser.py` is an optional multi-user web frontend. It keeps the existing single-user `app_streamlit.py` unchanged.

Create or reset a user account (password is read securely via `getpass`; the `--password` flag is deprecated and insecure):

```bash
python multiuser_store.py create-user alice
python multiuser_store.py create-user alice --overwrite
# Deprecated insecure option (visible in shell history):
python multiuser_store.py create-user alice --password "your-password"
```

Run the multi-user frontend:

```bash
python -m streamlit run app_streamlit_multiuser.py --server.address 127.0.0.1 --server.port 8502 --server.headless true --browser.gatherUsageStats false
```

Behavior:

- Logged-in users can edit their own Stocks and Broad Market pages, group names, and ticker lists.
- The watch list editor supports multi-line bulk editing and one-click save for multiple group changes.
- Guest users see the default watch list in read-only mode and cannot save changes.
- Each logged-in user's market-data cache is stored separately under `user_data/<username>_stock_cache.db`.
- Accounts are created by the administrator with the command line; there is no public self-registration UI.

## Recent Behavior Notes

### Price Units And Display Currency

The table price display uses Yahoo Finance ticker symbols as the canonical format. In local-currency mode, the frontend guesses the display unit from yfinance metadata when available and falls back to ticker suffix rules:

- US tickers: `$`
- China A-share / ETF tickers such as `.SS` and `.SZ`: `￥`
- Hong Kong tickers such as `.HK`: `HK$`
- Euro-market tickers such as `.DE`, `.PA`, `.AS`, `.MI`, `.MC`, `.BR`: `€`
- UK pence tickers such as `.L`: `p`

Non-price tickers such as indexes (`^VIX`, `^GSPC`), rates (`^TNX`), FX pairs (`EURUSD=X`), and market-breadth pseudo tickers do not receive a default dollar sign.

The multi-user Streamlit frontend also offers a display-only EUR mode. This mode converts visible price-like values with the latest available FX rate for easier reading by European users. It does not change the stored original market data.

### Price Source Colors

The `Price` cell color indicates the source of the latest displayed price:

- Green: regular/latest close.
- Blue: pre-market estimate.
- Yellow: after-hours estimate.

The separate `Price Source` field is kept as backend metadata and is not shown as a separate table column in the Streamlit UI.

### Extended-Hours Prices

Outside US regular trading hours, the backend keeps the normal daily-history update logic and additionally tries to fetch recent extended-hours data with yfinance `prepost=True` using a lightweight `4h` interval. When a valid US pre-market or after-hours price is newer than the latest regular close, it can be used as the latest watchlist price and written into `price_cache` for the affected ticker/date.

Market breadth calculations intentionally do not use extended-hours prices. They continue to use regular daily S&P 500 constituent data.

For K-line charts:

- Daily charts can overlay the latest extended-hours price when it is newer or on the same date as the latest regular daily bar.
- Weekly charts ignore extended-hours overlays.
- Intraday charts request yfinance data with `prepost=True`.

### Cache Files By Frontend Mode

- Tkinter, single-user Streamlit, and guest mode use the shared `stock_cache.db`.
- Logged-in multi-user Streamlit accounts use separate price caches under `user_data/<username>_stock_cache.db`.
- S&P 500 and Nasdaq 100 market-breadth data and market-cap cache are shared rather than duplicated per user.
- Ticker display-name cache follows the active price cache: logged-in users keep names in their own `user_data/<username>_stock_cache.db`, while Tkinter, single-user Streamlit, and guest mode use `stock_cache.db`. Existing ticker names are reused permanently; only tickers without a cached name are queried again.

### Integrated AI Agent Daily Reports

The multi-user frontend includes the v5.8 stock daily-report generator under the `AI Agent Reports` tab. Its code and report resources are stored in `daily_report/`, so deployment no longer depends on a separate project directory or `STOCK_DAILY_REPORT_PROJECT` setting.

Install the root `requirements.txt` after updating the project. Configure the required model and search credentials in the root `.env` or the systemd service environment, for example `DASHSCOPE_API_KEY`, `DEEPSEEK_API_KEY`, and `SERPER_API_KEY` according to the selected providers.

Each generation runs in an isolated directory under `daily_report/runs/`. The completed HTML is loaded into the Streamlit session for downloading, then all server-side files from that run are deleted immediately. Failed and timed-out runs are cleaned in the same way, and `daily_report/runs/` is excluded from Git.

For DashScope models, tool-calling mode is selected by model: the original `deepseek-v4-flash` setup uses native/raw tool calling, while `qwen-plus` uses Qwen-Agent's local parser by default. Raw responses are normalized to strict JSON before being added to API history. `QWEN_AGENT_USE_RAW_API=true` or `false` can explicitly override this selection.

Logged-in users can also submit a background `Generate & Email` job. Guest users retain the direct download workflow but cannot submit email jobs. Email jobs are persisted in `daily_report_jobs.db` and processed by a separate worker, so closing the browser or restarting Streamlit does not cancel them. The worker processes one report at a time, recovers interrupted jobs after restart, and retries failures with backoff.

The `Weekly Schedule` view lets a logged-in user select a ticker, recipient, weekday, and time in `Europe/Berlin`. Schedules continue without a browser session and automatically follow CET/CEST daylight-saving changes. A missed occurrence caused by server downtime creates one fresh report after the worker returns; older missed weeks are skipped. Schedules can be paused, resumed, or deleted, with a default maximum of ten schedules per account.

The generated HTML is temporarily stored as a SQLite BLOB only while delivery is pending. After successful delivery, or after the final failed attempt, both the complete recipient address and HTML BLOB are cleared. Final status rows are retained for seven days by default and then pruned.

Configure 163 Mail with its SMTP authorization code, not the mailbox login password:

```env
REPORT_SMTP_HOST=smtp.163.com
REPORT_SMTP_PORT=465
REPORT_SMTP_USE_SSL=true
REPORT_SMTP_USER=your_account@163.com
REPORT_SMTP_FROM=your_account@163.com
REPORT_SMTP_AUTH_CODE=your_163_smtp_authorization_code
REPORT_DAILY_LIMIT_PER_USER=3
REPORT_MAX_SCHEDULES_PER_USER=10
REPORT_EMAIL_MAX_ATTEMPTS=3
```

For local development, start the worker in a second terminal:

```bash
python -m daily_report.worker
```

On the cloud server, the worker runs as a dedicated non-root system user
(`stockwatch`) with systemd security hardening. Run the setup script first to
create the user, prepare the `data/` directory, and migrate the job database:

```bash
sudo bash deploy/setup-worker-user.sh
```

Then install and start the systemd unit:

```bash
sudo cp deploy/stock-watchlist-report-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now stock-watchlist-report-worker
sudo systemctl status stock-watchlist-report-worker --no-pager
```

Verify the security posture of the service:

```bash
systemd-analyze security stock-watchlist-report-worker
```

The hardened service file includes:

- **Non-root execution** as the `stockwatch` system user with `/usr/sbin/nologin`
- **NoNewPrivileges** — prevents gaining new capabilities
- **ProtectSystem=strict** — entire filesystem read-only except `ReadWritePaths`
- **ProtectHome** — hides `/home`, `/root`, `/run/user`
- **PrivateTmp / PrivateDevices** — isolated `/tmp` and no device access
- **CapabilityBoundingSet=** — zero Linux capabilities granted
- **SystemCallFilter** — only `@system-service` syscalls; `@privileged` and `@resources` denied
- **RestrictAddressFamilies** — only `AF_INET`, `AF_INET6`, `AF_UNIX`
- **ProtectKernel\***, **ProtectControlGroups**, **ProtectClock**, **ProtectHostname**
- **RestrictNamespaces**, **RestrictRealtime**, **RestrictSUIDSGID**, **LockPersonality**
- **MemoryMax=4G**, **TasksMax=512** — resource limits
- **EnvironmentFile** — loads `/opt/Stock_watch_list/.env` explicitly
- **REPORT_JOB_DB** — job database stored in `data/daily_report_jobs.db`

Worker logs are available through:

```bash
sudo journalctl -u stock-watchlist-report-worker -f
```

## License

MIT License

## Author

cyp9313

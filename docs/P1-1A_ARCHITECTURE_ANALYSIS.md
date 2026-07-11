# P1-1A: 重复数据管线架构确认

> 阶段: 分析 only — 不修改产品代码
> 日期: 2026-07-11
> 状态: 待确认

---

## 目录

1. [Watchlist 后端数据获取调用链](#1-watchlist-后端数据获取调用链)
2. [日报系统数据获取调用链](#2-日报系统数据获取调用链)
3. [两套 StockAnalysis scraper 对比](#3-两套-stockanalysis-scraper-对比)
4. [入口维度矩阵](#4-入口维度矩阵)
5. [语义不同但看似重复的部分](#5-语义不同但看似重复的部分)
6. [同一份报告中的数据快照时序确认](#6-同一份报告中的数据快照时序确认)
7. [P0/P1 修改引入的新约束](#7-p0p1-修改引入的新约束)
8. [最小风险的共享接口设计](#8-最小风险的共享接口设计)
9. [迁移步骤和回滚边界](#9-迁移步骤和回滚边界)
10. [Characterization Tests 计划](#10-characterization-tests-计划)

---

## 1. Watchlist 后端数据获取调用链

### 1.1 整体架构

```
Flask API 请求
  |
  +-- /api/stock_data (POST/GET)
  |     |-- normalize_yfinance_ticker()         ← ticker 标准化
  |     |-- get_cached_ticker_names()           ← ticker_name_cache + yf.Ticker().info
  |     |-- get_cached_stock_analysis()         ← stock_analysis_data + scrape_batch()
  |     |-- _fetch_yf_data()                    ← yf.Ticker().calendar / .info
  |     |-- get_prices_with_cache()             ← price_cache + yf.download(auto_adjust=False)
  |     |-- update_extended_hours_price_cache() ← yf.download(4h, prepost)
  |     |-- get_cached_betas()                  ← beta_cache
  |     +-- Beta 计算 (np.cov, 252天)           ← save_betas()
  |
  +-- /api/kline_data (GET)
  |     |-- yf.Ticker().history()               ← OHLCV K线 (未缓存!)
  |     |-- calculate_chip_distribution()       ← yf.Ticker().history(4h, 30d) (未缓存!)
  |     +-- get_cached_stock_analysis()         ← stock_analysis_data
  |
  +-- /api/breadth_data (POST)
  |     |-- get_sp500_symbols()                 ← JSON cache + Wikipedia
  |     |-- get_prices_with_cache()             ← price_cache
  |     +-- get_cached_market_caps()            ← market_cap_cache (全局DB)
  |
  +-- /api/fear_greed, /api/fear_greed_crypto
```

### 1.2 核心函数: `get_prices_with_cache`

| 维度 | 值 |
|------|-----|
| **文件** | `stock_watch_list_back_end.py` |
| **行号** | 1124-1238 |
| **输入** | `tickers: list[str]`, `period="2y"`, `delete_stale=False` |
| **输出** | MultiIndex DataFrame: `('Adj Close', ticker)`, `('Volume', ticker)` |
| **时间范围** | 默认 2 年 |
| **auto_adjust** | `False` (显式) |
| **数据源** | `yf.download(tickers, period, interval="1d", auto_adjust=False, group_by="column")` |
| **缓存** | SQLite `price_cache` 表 (per-user DB), 增量更新 (3-way delta) |
| **错误处理** | 无 try/except, 由调用方处理 |
| **ticker 映射** | 调用方负责 |
| **货币** | 原始货币, 无转换 |
| **时区** | `America/New_York` (get_market_date) |
| **调用频率** | 每次 API 请求触发; 增量下载仅缺失天数 |

### 1.3 Beta 计算

| 维度 | 值 |
|------|-----|
| **行号** | 2513-2538 (内嵌于 get_stock_data) |
| **基准** | `^GSPC` (S&P 500) |
| **窗口** | 252 个交易日 |
| **方法** | `np.cov(stock_ret, sp500_ret)`, Beta = cov[0,1] / var_sp500 |
| **缓存** | SQLite `beta_cache` 表 (per-user DB) |
| **时区** | `tz_localize(None)` 去时区后取交集 |

### 1.4 StockAnalysis 基本面

| 维度 | 值 |
|------|-----|
| **函数** | `get_cached_stock_analysis()` (328-411) |
| **字段** | forward_pe, peg_ratio, trailing_pe, market_cap, earnings_date, ps_ratio, pb_ratio, analyst_rating, price_target (9 个) |
| **缓存** | SQLite `stock_analysis_data` 表 (per-user DB), 当日缓存 |
| **数据源** | `scrape_batch()` from `stockanalysis_scraper.py` |
| **并发** | ThreadPoolExecutor(max_workers=5) |

### 1.5 K线数据 (独立路径)

| 维度 | 值 |
|------|-----|
| **函数** | `get_kline_data()` (2763-2907) |
| **数据源** | `yf.Ticker(ticker).history()` — **未指定 auto_adjust** |
| **缓存** | **无** — 每次请求都重新下载 |
| **筹码分布** | `calculate_chip_distribution()` — `yf.Ticker().history(interval="4h", period=days)` — **无缓存** |

### 1.6 缓存体系

| 表/文件 | DB 范围 | 内容 | 保留期 |
|---------|---------|------|--------|
| `price_cache` | Per-user | ticker, date, adj_close, volume | 750 天 |
| `stock_analysis_data` | Per-user | 9 个基本面字段 | 90 天 |
| `beta_cache` | Per-user | ticker, date, beta, data_points | 90 天 |
| `market_cap_cache` | **全局** | ticker, market_cap | 730 天 |
| `ticker_name_cache` | Per-user | ticker, name | 无自动清理 |
| `*.json` | 全局文件 | SP500/Nasdaq100 成分股 | 7 天 |

---

## 2. 日报系统数据获取调用链

### 2.1 整体架构

```
worker.py / CLI
  |
  +-- service.py::generate_report(ticker)
        |
        +-- subprocess.run(run_report.py, ticker, --months, --date, --run-dir)
              |
              +-- cli.py::main()
                    |
                    +-- agent_runner.py::run_agent()
                          |
                          +-- Qwen-Agent LLM 自主调用工具:
                                |
                                +-- [Tool 1] fetch_technical_data
                                |     +-- subprocess: python fetch_and_calc.py TICKER output.json
                                |           |-- yf.download(TICKER, period='1y', auto_adjust=False)  ← 第1次下载
                                |           |-- yf.Ticker(TICKER).info
                                |           +-- scrape_stock_analysis(TICKER)
                                |
                                +-- [Tool 5] generate_technical_chart
                                      +-- subprocess: python gen_chart.py TICKER output.html --months 3
                                            +-- yf.download(TICKER, period='1y', auto_adjust=False)  ← 第2次下载 (重复!)
```

### 2.2 fetch_and_calc.py

| 维度 | 值 |
|------|-----|
| **文件** | `daily_report/scripts/fetch_and_calc.py` |
| **行号** | 50 (yf.download), 62-67 (yf.Ticker().info), 135-230 (StockAnalysis) |
| **输入** | `sys.argv[1]` (ticker, 大写化), 环境变量 `STOCKANALYSIS_ENABLED` |
| **输出** | JSON 文件 (`{TICKER}_data.json`) — 技术指标 + 基本面 |
| **时间范围** | `period='1y'` (1 年) |
| **auto_adjust** | `False` (显式) |
| **缓存** | **无** — 每次运行全量下载 |
| **错误处理** | try/except for .info; 数据不足 30 条时 exit(1) |
| **ticker 映射** | 不使用 `normalize_yfinance_ticker()`; 仅 `is_known_us_etf` + `should_query_forward_pe` |
| **货币** | 从 `yf.Ticker().info.get('currency', 'USD')` |
| **时区** | 无显式处理 |
| **调用频率** | 每次报告生成 1 次 |
| **StockAnalysis** | `scrape_stock_analysis(TICKER)` from 根目录 `stockanalysis_scraper.py` |
| **字段** | 后端 9 个 + daily_report 版额外 11 个 (ev_sales, ev_ebitda, 等) |

### 2.3 gen_chart.py

| 维度 | 值 |
|------|-----|
| **文件** | `daily_report/scripts/gen_chart.py` |
| **行号** | 38 (yf.download) |
| **输入** | `sys.argv[1]` (ticker, 大写化), `--months` (默认 3) |
| **输出** | HTML 文件 (`{TICKER}_chart.html`) — Plotly K线图 |
| **时间范围** | `period='1y'` (下载 1 年), 截取近 `MONTHS` 个月绘图 |
| **auto_adjust** | `False` (显式) |
| **缓存** | **无** |
| **错误处理** | MultiIndex 扁平化 + dropna; 无数据量检查 |
| **ticker 映射** | 不使用 |
| **货币** | 无 |
| **时区** | 无 |
| **调用频率** | 每次报告生成 1 次 |

### 2.4 日报系统缓存

| 存储 | 用途 |
|------|------|
| `daily_report_jobs.db` | 仅存储作业队列/调度/邮件状态 — **不存储价格数据** |
| 无 price_cache | **不复用**后端 `stock_cache.db` |
| 无 SA cache | **不复用**后端 `stock_analysis_data` 表 |

---

## 3. 两套 StockAnalysis scraper 对比

### 3.1 三个实现

| 文件 | 行数 | 字段数 | 状态 |
|------|------|--------|------|
| `stockanalysis_scraper.py` (根目录) | 267 | 9 | 主流程 (后端 + fetch_and_calc.py) |
| `daily_report/scripts/stockanalysis_scraper.py` | 214 | 20 | V5.8 增强版 (daily_report 专用副本) |
| `qwen_forward_pe.py` | 228 | 3 | 旧版/备用, 不被导入 |

### 3.2 根目录版 vs daily_report 版

| 维度 | 根目录版 (267行) | daily_report版 (214行, V5.8) |
|------|------------------|------------------------------|
| **字段数** | 9 | 20 (+11: ev_sales, ev_ebitda, ev_fcf, p_fcf, p_ocf, forward_ps, fcf_yield, debt_equity, debt_ebitda, debt_fcf, interest_coverage) |
| **JS 正则模式** | 1 种 | 2 种 + IGNORECASE |
| **字段别名** | 无 (硬编码标签) | FIELD_ALIASES 字典 (1-4 个别名/字段) |
| **N/A 检测** | `"n/a"` | `{"n/a", "na", "-", "—"}` |
| **HTML 清理** | 仅 table 值 | JS 值 + table 值 |
| **FCF Yield 推导** | 无 | 从 P/FCF 计算 |
| **代码结构** | 逐字段硬编码 | FIELD_ALIASES 循环 (DRY) |
| **日志** | 逐字段详细打印 | 紧凑摘要 |
| **函数签名** | 相同 | 相同 |
| **URL 生成** | ticker_mapping | ticker_mapping |
| **并发** | ThreadPoolExecutor(5) | ThreadPoolExecutor(5) |
| **缓存** | 无 (调用方实现) | 无 (调用方实现) |

### 3.3 qwen_forward_pe.py (旧版)

| 维度 | 值 |
|------|-----|
| **字段数** | 3 (forward_pe, analyst_rating, price_target) |
| **URL** | 硬编码美股 URL, 不支持国际市场 |
| **正则** | 依赖特定 JS 组件名 (analystRatings, priceTarget) |
| **缓存** | **内置 SQLite** (`forward_pe_cache.db`) |
| **ticker 映射** | 自行实现 (简单), 不用 ticker_mapping.py |
| **当前状态** | README 明确标注 "旧版/备用", 不被任何模块导入 |

### 3.4 被调用关系

```
stock_watch_list_back_end.py
  └── from stockanalysis_scraper import scrape_batch, should_query_forward_pe  ← 根目录版

daily_report/scripts/fetch_and_calc.py
  └── from stockanalysis_scraper import scrape_stock_analysis, should_query_forward_pe  ← 根目录版
      (通过 sys.path.insert 导入根目录)

qwen_forward_pe.py
  └── 独立运行, 不被导入
```

**关键发现**: fetch_and_calc.py 导入的是**根目录版** (9 字段), 而非 daily_report 版 (20 字段)。daily_report 版虽然更功能完整, 但实际上没有被 fetch_and_calc.py 使用。

### 3.5 测试覆盖

| 爬虫 | 专用测试 | 间接测试 | 逻辑覆盖 |
|------|---------|---------|---------|
| 根目录版 | 无 | 3 个文件 Mock 它 | 0% |
| daily_report版 | 无 | 无 | 0% |
| qwen_forward_pe.py | 无 | 无 | 0% |

---

## 4. 入口维度矩阵

### 4.1 价格数据下载入口

| 入口 | 函数 | API | period | interval | auto_adjust | 缓存 | ticker映射 | 货币 | 时区 |
|------|------|-----|--------|----------|-------------|------|-----------|------|------|
| 后端 stock_data | `get_prices_with_cache` | `yf.download` | 2y | 1d | False | SQLite price_cache | 调用方 | 原始 | ET |
| 后端 extended_hours | `update_extended_hours_price_cache` | `yf.download` | 1d | 4h | False | SQLite price_cache | 调用方 | 原始 | ET |
| 后端 kline | `get_kline_data` | `yf.Ticker().history()` | 自定义 | 1d/5m/15m/1h/4h/1wk | **默认(True)** | **无** | 无 | 原始 | 无 |
| 后端 chip_dist | `calculate_chip_distribution` | `yf.Ticker().history()` | 30d | 4h | **默认(True)** | **无** | 无 | 原始 | 无 |
| 日报 fetch_calc | 脚本顶层 | `yf.download` | 1y | 1d | False | **无** | 部分 | info | 无 |
| 日报 gen_chart | 脚本顶层 | `yf.download` | 1y | 1d | False | **无** | 无 | 无 | 无 |

### 4.2 基本面数据入口

| 入口 | 函数/脚本 | 数据源 | 字段数 | 缓存 | ticker映射 |
|------|----------|--------|--------|------|-----------|
| 后端 stock_data | `get_cached_stock_analysis` | 根目录 scraper | 9 | SQLite SA表 | normalize |
| 后端 stock_data | `_fetch_yf_data` | yf.Ticker().info | ~5 | 无 | 无 |
| 后端 kline | `get_cached_stock_analysis` | 根目录 scraper | 9 | SQLite SA表 | normalize |
| 日报 fetch_calc | 脚本顶层 | 根目录 scraper | 9 | **无** | 部分 |
| 日报 fetch_calc | 脚本顶层 | yf.Ticker().info | ~17 | **无** | 无 |

### 4.3 Beta 数据入口

| 入口 | 函数 | 基准 | 窗口 | 缓存 |
|------|------|------|------|------|
| 后端 stock_data | 内嵌计算 | ^GSPC | 252天 | SQLite beta_cache |
| 日报 fetch_calc | yf.Ticker().info['beta'] | yfinance 内部 | 未知 | 无 |

---

## 5. 语义不同但看似重复的部分

### 5.1 不能强行合并的语义差异

| # | 表面重复 | 实际差异 | 能否合并 |
|---|---------|---------|---------|
| 1 | 后端 `get_prices_with_cache` (2y) vs 日报 `yf.download` (1y) | 时间范围不同: 后端需要 2 年用于 MA200; 日报只需 1 年 | 可合并为参数化 period |
| 2 | 后端 `yf.download(auto_adjust=False)` vs 后端 `yf.Ticker().history()` (kline) | auto_adjust 不同: download=False 保留 Adj Close; history()=默认True 用 Close | **不能合并** — K线图需要 OHLC, 缓存只需要 Adj Close+Volume |
| 3 | 后端 StockAnalysis (9字段) vs daily_report版 (20字段) | 字段集不同: daily_report版多11个估值/债务字段 | 可合并为统一接口, 返回超集 |
| 4 | 后端 Beta 计算 (np.cov, 252天) vs 日报 Beta (yf.Ticker().info['beta']) | 算法不同: 后端自算 vs yfinance 内部值 | **不能合并** — 后端自算更可控 |
| 5 | 后端 price_cache (SQLite) vs 日报无缓存 | 缓存策略不同: 后端增量更新; 日报全量下载 | 可共享缓存层 |
| 6 | 后端 per-user DB vs 日报全局 | 隔离粒度不同: 后端每用户独立; 日报无用户概念 | 可用全局DB作为日报的缓存 |
| 7 | fetch_and_calc.py `yf.download` vs gen_chart.py `yf.download` | **完全相同** — 同 ticker, 同 period, 同 auto_adjust | **可合并** — 这是真正的重复 |
| 8 | 根目录 scraper vs daily_report scraper | 功能不对等: 根目录版9字段 vs daily_report版20字段 | 可合并为统一版 (保留超集) |

### 5.2 关键区分: "实际值为 0" vs "数据缺失" vs "不适用" vs "provider 失败"

当前代码中**没有明确区分这四种状态**:

| 状态 | 当前表示 | 问题 |
|------|---------|------|
| 实际值为 0 | `0.0` 或 `0` | 与"数据缺失"混淆 (None 也可能被转为 0) |
| 数据缺失 | `None` / `null` | 与"不适用"混淆 |
| 不适用 (N/A) | 字符串 `"n/a"` (仅 scraper 中检测) | 与 provider 失败混淆 |
| provider 失败 | `empty_result("request_error: ...")` | `raw_answer` 字段区分, 但数值字段为 None |

**P1-1B 需要引入明确的枚举或 sentinel 值来区分这四种状态。**

---

## 6. 同一份报告中的数据快照时序确认

### 6.1 确认: 同一份报告使用不同下载时刻

**是的, 同一份报告中的主数据和图表使用不同时刻下载的数据。**

时序:

```
T1: fetch_technical_data 调用 fetch_and_calc.py
    └── yf.download(TICKER, period='1y', auto_adjust=False)  ← 快照 #1
    └── yf.Ticker(TICKER).info
    └── scrape_stock_analysis(TICKER)
    └── 写入 data.json

    ... LLM 处理时间 (数秒到数分钟) ...
    ... priority_market_research (搜索新闻) ...
    ... generate_technical_note_items ...
    ... save_news_notes ...

T2: generate_technical_chart 调用 gen_chart.py
    └── yf.download(TICKER, period='1y', auto_adjust=False)  ← 快照 #2 (可能不同!)
    └── 写入 chart.html
```

**时间差**: T1 和 T2 之间可能有数秒到数分钟的间隔 (取决于 LLM 处理时间和搜索耗时)。

**影响**:
- 如果在 T1 和 T2 之间市场数据发生变化 (盘中生成报告时), K线图和技术指标可能基于不同的价格
- 对于盘中报告, 这意味着报告正文中的 MA/RSI/MACD 值可能与 K线图上的不完全一致
- 对于收盘后报告, 影响较小 (yfinance 数据通常不变), 但仍非确定性

### 6.2 重复下载确认

| 下载 | fetch_and_calc.py | gen_chart.py | 重复? |
|------|-------------------|--------------|-------|
| yf.download(TICKER, period='1y', interval='1d', auto_adjust=False) | 是 (行 50) | 是 (行 38) | **是** |
| yf.Ticker(TICKER).info | 是 (行 62) | 否 | 否 |
| scrape_stock_analysis(TICKER) | 是 (行 135) | 否 | 否 |

**每次报告生成: 同一 ticker 的 1 年日线数据被下载 2 次。**

---

## 7. P0/P1 修改引入的新约束

### 7.1 约束清单

| 阶段 | 约束 | 影响数据管线? | 详情 |
|------|------|-------------|------|
| P0-1 | HTML 转义 + 白名单 | 否 | 纯输出层, 不影响数据获取 |
| P0-2 | SSRF 防护 (文章抓取) | 否 | 仅文章正文, 不影响 yfinance/SA |
| P0-3 | 速率限制 (下载报告) | 间接 | 限制并发报告数, 不限制数据获取 |
| P1-2 | DB 责任分离 | 否 | 不影响数据层 |
| P1-5 | Flask/Streamlit 生命周期分离 | **关键** | 日报**不依赖** Flask 运行 |
| P1-6 | POST 迁移 | 否 | 日报不调用 /api/stock_data |
| P1-7 | 登录防暴力 | 否 | 不影响数据层 |
| P1-3 | 邮件幂等 | 否 | 不影响数据层 |
| P1-8 | 队列容量/过期 | 间接 | 限制并发报告数 (REPORT_MAX_GLOBAL_RUNNING=1) |

### 7.2 对 P1-1B 的关键约束

1. **日报不能依赖 Flask**: 共享层必须能被 CLI、worker 和 Flask 独立调用
2. **单 worker 设计**: `REPORT_MAX_GLOBAL_RUNNING=1`, 报告串行生成
3. **auto_adjust 不一致**: `yf.download` 用 False, `yf.Ticker().history()` 用默认(True) — 合并时必须保持各自语义
4. **per-user 缓存隔离**: 后端缓存是 per-user 的, 日报无用户概念 — 共享缓存需要全局 DB
5. **不得改变金融计算**: 指标公式、评分公式、技术分析规则不能变
6. **指数/ETF 保留 Volume**: 不得因指数不可直接交易而删除 Volume 相关指标

---

## 8. 最小风险的共享接口设计

### 8.1 设计原则

1. **不改变现有入口** — fetch_and_calc.py、gen_chart.py、后端 API 保持原有接口
2. **共享层可选** — 现有代码可以选择不使用共享层
3. **渐进式迁移** — 先共享最明显的重复 (yf.download), 再逐步扩展
4. **不引入新依赖** — 仅使用 yfinance、pandas、sqlite3 等已有依赖

### 8.2 共享市场数据服务 (MarketDataService)

```
daily_report/market_data_service.py  (新文件)

class MarketDataService:
    """共享市场数据服务 — 可被 CLI、worker、Flask 独立调用。"""

    def __init__(self, cache_db_path: str | None = None):
        """
        Args:
            cache_db_path: SQLite 缓存路径。None = 内存模式 (不缓存)。
                          后端可传入 per-user DB 路径。
                          日报可传入全局 DB 路径或 None。
        """

    def fetch_ohlcv(
        self,
        ticker: str,
        period: str = "1y",
        interval: str = "1d",
        auto_adjust: bool = False,
        use_cache: bool = True,
    ) -> pd.DataFrame:
        """
        获取 OHLCV 数据。

        - ticker: 已标准化的 yfinance ticker (调用方负责 normalize)
        - period: "1y", "2y", "6mo", etc.
        - interval: "1d", "1h", "4h", etc.
        - auto_adjust: False = 保留 Adj Close 列; True = Close 即调整后
        - use_cache: True = 检查/写入 SQLite 缓存 (仅 interval=1d + auto_adjust=False)

        Returns:
            DataFrame with columns: Open, High, Low, Close, Adj Close (if auto_adjust=False), Volume

        缓存策略:
        - 仅缓存 interval="1d" + auto_adjust=False 的 Adj Close + Volume
        - 其他参数组合不缓存 (保持当前行为)
        - 增量更新: 复用后端 get_prices_with_cache 的 3-way delta 逻辑
        """

    def fetch_ticker_info(self, ticker: str) -> dict:
        """获取 yf.Ticker().info, 带可选缓存。"""

    def fetch_stock_analysis(
        self,
        ticker: str,
        use_cache: bool = True,
    ) -> dict:
        """
        获取 StockAnalysis.com 数据。
        使用 daily_report 版 scraper (20 字段超集)。
        缓存: 当日缓存 (可选)。
        """

    def fetch_beta(
        self,
        ticker: str,
        benchmark: str = "^GSPC",
        window: int = 252,
    ) -> float | None:
        """
        计算 Beta。
        复用后端 np.cov 算法。
        """

    @staticmethod
    def normalize_ticker(ticker: str) -> str:
        """委托给 ticker_mapping.normalize_yfinance_ticker()。"""
```

### 8.3 数据快照机制

```
class ReportDataSnapshot:
    """一次报告生成的数据快照 — 确保主数据和图表使用同一数据。"""

    def __init__(self, service: MarketDataService, ticker: str, months: int):
        self._service = service
        self._ticker = ticker
        self._months = months
        self._ohlcv: pd.DataFrame | None = None
        self._info: dict | None = None
        self._sa_data: dict | None = None

    @property
    def ohlcv(self) -> pd.DataFrame:
        """延迟加载, 首次调用后缓存。"""
        if self._ohlcv is None:
            self._ohlcv = self._service.fetch_ohlcv(
                self._ticker, period="1y", auto_adjust=False
            )
        return self._ohlcv

    @property
    def info(self) -> dict:
        if self._info is None:
            self._info = self._service.fetch_ticker_info(self._ticker)
        return self._info

    @property
    def stock_analysis(self) -> dict:
        if self._sa_data is None:
            self._sa_data = self._service.fetch_stock_analysis(self._ticker)
        return self._sa_data

    def ohlcv_for_chart(self, months: int) -> pd.DataFrame:
        """截取近 N 个月用于图表。"""
        return self.ohlcv.tail(months * 21)  # ~21 交易日/月
```

### 8.4 统一 StockAnalysis scraper

**决策**: 合并为 daily_report 版 (20 字段超集), 因为:
- 函数签名与根目录版完全兼容
- 20 字段是 9 字段的超集
- 代码更 DRY (FIELD_ALIASES)
- 解析更健壮 (双重正则 + 别名 + 更宽松 N/A 检测)

**合并方式**:
- 将 daily_report 版复制到根目录, 替换根目录版
- 删除 daily_report/scripts/ 版本 (fetch_and_calc.py 改为导入根目录版)
- 保留 `qwen_forward_pe.py` 不变 (不删除, 但标注 deprecated)

### 8.5 数据状态区分

```python
from enum import Enum

class DataStatus(Enum):
    ACTUAL = "actual"          # 实际值 (包括 0)
    MISSING = "missing"        # 数据缺失 (provider 返回空)
    NOT_APPLICABLE = "n/a"     # 不适用 (如 ETF 没有 P/E)
    PROVIDER_ERROR = "error"   # provider 失败 (网络错误等)

# scraper 返回值中, 每个字段改为:
# {"value": float | None, "status": DataStatus, "raw": str}
```

**注意**: 这改变了 scraper 返回格式, 需要在 characterization tests 中先锁定当前行为, 再逐步迁移。

### 8.6 共享层调用关系

```
改造后:

后端 stock_watch_list_back_end.py
  └── MarketDataService(cache_db_path=per_user_db)
        ├── fetch_ohlcv() → 替代 get_prices_with_cache()
        ├── fetch_stock_analysis() → 替代 get_cached_stock_analysis()
        └── fetch_beta() → 替代内嵌 Beta 计算

日报 fetch_and_calc.py
  └── MarketDataService(cache_db_path=None or global_db)
        ├── fetch_ohlcv() → 替代 yf.download()
        ├── fetch_ticker_info() → 替代 yf.Ticker().info
        └── fetch_stock_analysis() → 替代 scrape_stock_analysis()

日报 gen_chart.py
  └── ReportDataSnapshot.ohlcv_for_chart() → 复用 fetch_and_calc 的快照
      (或独立调用 MarketDataService.fetch_ohlcv() 带缓存)
```

---

## 9. 迁移步骤和回滚边界

### 9.1 迁移步骤 (P1-1B)

| 步骤 | 内容 | 风险 | 回滚方式 |
|------|------|------|---------|
| 0 | 添加 characterization tests | 无 | 删除测试文件 |
| 1 | 创建 `market_data_service.py` (新文件) | 无 (新增不修改) | 删除文件 |
| 2 | 合并 scraper: daily_report 版 → 根目录版 | 中 (解析逻辑变化) | 恢复根目录版 |
| 3 | fetch_and_calc.py 改用 MarketDataService | 中 | 恢复 yf.download 调用 |
| 4 | gen_chart.py 改用 MarketDataService (或快照) | 中 | 恢复 yf.download 调用 |
| 5 | 后端 get_prices_with_cache 改用 MarketDataService | 高 (影响所有用户) | 恢复原函数 |
| 6 | 后端 get_cached_stock_analysis 改用 MarketDataService | 中 | 恢复原函数 |
| 7 | 添加 DataStatus 枚举到 scraper 返回值 | 中 (格式变化) | 恢复原格式 |
| 8 | 删除 qwen_forward_pe.py (可选, 标注 deprecated) | 低 | git checkout |

### 9.2 回滚边界

- **每一步都是独立可回滚的**
- **步骤 1-2 可以独立回滚** (不影响现有代码)
- **步骤 3-4 可以独立回滚** (仅影响日报)
- **步骤 5-6 可以独立回滚** (仅影响后端)
- **步骤 7 必须与步骤 2-6 一起回滚** (格式变化影响所有调用方)
- **最安全策略**: 先做 0-4 (日报侧), 验证无回归后再做 5-7 (后端侧)

### 9.3 不做的事

- **不改变 auto_adjust 语义**: `yf.download` 路径保持 `auto_adjust=False`; `yf.Ticker().history()` 路径保持默认
- **不改变 Beta 算法**: 后端 np.cov(252天) 不变; 日报 yf.Ticker().info['beta'] 不变
- **不改变 K线图的 yf.Ticker().history() 调用**: K线需要 OHLC, 缓存只有 Adj Close+Volume
- **不改变指标公式**: MA/RSI/MACD/布林带/Volume Ratio/Volume Profile 计算不变
- **不改变指数/ETF 的 Volume 指标**: 继续保留 Volume、Volume Ratio、Volume Profile
- **不强制多 worker**: 保持 REPORT_MAX_GLOBAL_RUNNING=1

---

## 10. Characterization Tests 计划

### 10.1 目的

在修改前用测试锁定当前行为, 确保重构不改变语义。

### 10.2 测试文件

`tests/test_market_data_characterization.py`

### 10.3 测试用例

#### A. 价格数据快照一致性

| 测试 | 验证内容 |
|------|---------|
| `test_fetch_and_calc_uses_auto_adjust_false` | fetch_and_calc.py 的 yf.download 调用使用 auto_adjust=False |
| `test_gen_chart_uses_auto_adjust_false` | gen_chart.py 的 yf.download 调用使用 auto_adjust=False |
| `test_same_ticker_same_period` | 两个脚本对同一 ticker 使用相同 period='1y' |
| `test_report_uses_single_data_snapshot` | 改造后: gen_chart 复用 fetch_and_calc 的数据, yf.download 只调用 1 次 |
| `test_chart_ohlcv_matches_calc_ohlcv` | 改造后: 图表数据与技术指标数据来自同一 DataFrame |

#### B. Adj Close 和收益率计算

| 测试 | 验证内容 |
|------|---------|
| `test_adj_close_preserved` | auto_adjust=False 时 DataFrame 包含 Adj Close 列 |
| `test_daily_returns_unchanged` | 日收益率计算基于 Adj Close, 改造前后结果一致 |
| `test_ma200_unchanged` | MA200 计算基于 Adj Close, 改造前后结果一致 |
| `test_rsi_unchanged` | RSI 计算基于 Adj Close, 改造前后结果一致 |
| `test_macd_unchanged` | MACD 计算基于 Adj Close, 改造前后结果一致 |

#### C. Beta 算法

| 测试 | 验证内容 |
|------|---------|
| `test_beta_uses_252_day_window` | 后端 Beta 使用 252 个交易日 |
| `test_beta_uses_gspc_benchmark` | 后端 Beta 基准为 ^GSPC |
| `test_beta_calculation_method` | Beta = cov[0,1] / var_sp500 |
| `test_beta_report_uses_yfinance_info` | 日报 Beta 来自 yf.Ticker().info['beta'] (不合并) |

#### D. Volume 指标 (指数/ETF)

| 测试 | 验证内容 |
|------|---------|
| `test_index_retains_volume` | 指数 (^GSPC, ^IXIC) 的数据包含 Volume |
| `test_etf_retains_volume` | ETF (SPY, QQQ) 的数据包含 Volume |
| `test_volume_ratio_unchanged` | Volume Ratio 计算不变 |
| `test_volume_profile_input_unchanged` | Volume Profile 输入数据不变 |
| `test_chip_distribution_uses_4h` | 筹码分布使用 4h interval (不合并到日线缓存) |

#### E. StockAnalysis scraper 兼容性

| 测试 | 验证内容 |
|------|---------|
| `test_merged_scraper_returns_20_fields` | 合并后的 scraper 返回 20 个字段 |
| `test_merged_scraper_backward_compatible` | 原 9 个字段的 key 和值类型不变 |
| `test_merged_scraper_parses_js_value` | JS 嵌入数据解析正确 |
| `test_merged_scraper_parses_table_value` | HTML 表格解析正确 |
| `test_merged_scraper_handles_n_a` | N/A 检测覆盖 {"n/a", "na", "-", "—"} |
| `test_merged_scraper_field_aliases` | FIELD_ALIASES 别名匹配正确 |
| `test_merged_scraper_fcf_yield_derivation` | FCF Yield 从 P/FCF 推导正确 |

#### F. Ticker 映射

| 测试 | 验证内容 |
|------|---------|
| `test_normalize_us_stock` | AAPL → AAPL |
| `test_normalize_hk_stock` | 0700.HK → 0700.HK, hkg:0700 → 0700.HK |
| `test_normalize_a_share` | 510300.SS → 510300.SS |
| `test_normalize_european` | SAP.DE → SAP.DE |
| `test_etf_not_normalized_away` | SPY, QQQ 保持不变 |
| `test_index_not_normalized_away` | ^GSPC, ^IXIC 保持不变 |
| `test_crypto_not_normalized_away` | BTC-USD 保持不变 |

#### G. 货币和时区

| 测试 | 验证内容 |
|------|---------|
| `test_usd_ticker_currency` | AAPL 的 currency = USD |
| `test_hkd_ticker_currency` | 0700.HK 的 currency = HKD |
| `test_eur_ticker_currency` | SAP.DE 的 currency = EUR |
| `test_market_date_uses_et` | get_market_date() 返回美东日期 |
| `test_beta_tz_localize_none` | Beta 计算使用 tz_localize(None) |

#### H. 多市场覆盖

| 测试 | 验证内容 |
|------|---------|
| `test_us_stock_full_pipeline` | 美股 (AAPL) 完整管线 |
| `test_etf_full_pipeline` | ETF (SPY) 完整管线 |
| `test_index_full_pipeline` | 指数 (^GSPC) 完整管线 |
| `test_hk_stock_full_pipeline` | 港股 (0700.HK) 完整管线 |
| `test_european_stock_full_pipeline` | 欧股 (SAP.DE) 完整管线 |
| `test_crypto_full_pipeline` | 加密货币 (BTC-USD) 完整管线 |

#### I. 数据状态区分

| 测试 | 验证内容 |
|------|---------|
| `test_actual_zero_vs_missing` | 实际值为 0 与数据缺失 (None) 区分 |
| `test_missing_vs_not_applicable` | 数据缺失与 N/A 区分 |
| `test_not_applicable_vs_provider_error` | N/A 与 provider 失败区分 |
| `test_provider_error_has_raw` | provider 失败时 raw_answer 包含错误信息 |

#### J. 独立调用

| 测试 | 验证内容 |
|------|---------|
| `test_service_callable_without_flask` | MarketDataService 不导入 Flask |
| `test_service_callable_from_cli` | CLI 入口可调用 MarketDataService |
| `test_service_callable_from_worker` | Worker 入口可调用 MarketDataService |
| `test_service_callable_from_flask` | Flask 入口可调用 MarketDataService |

#### K. 网络调用次数

| 测试 | 验证内容 |
|------|---------|
| `test_single_report_single_yf_download` | 改造后: 一次报告生成只调用 yf.download 1 次 (而非 2 次) |
| `test_cached_ohlcv_no_download` | 缓存命中时不调用 yf.download |
| `test_sa_scrape_called_once_per_report` | StockAnalysis 每报告只爬取 1 次 |
| `test_ticker_info_called_once_per_report` | yf.Ticker().info 每报告只调用 1 次 |

#### L. P0/P1 回归

| 测试 | 验证内容 |
|------|---------|
| `test_p0_html_escape_unchanged` | P0-1 HTML 转义不变 |
| `test_p0_ssrf_unchanged` | P0-2 SSRF 防护不变 |
| `test_p0_rate_limit_unchanged` | P0-3 速率限制不变 |
| `test_p1_email_dedup_unchanged` | P1-3 邮件幂等不变 |
| `test_p1_queue_capacity_unchanged` | P1-8 队列限制不变 |
| `test_p1_login_security_unchanged` | P1-7 登录安全不变 |

---

## 附录: 文件影响范围

### P1-1B 将修改的文件

| 文件 | 修改类型 |
|------|---------|
| `daily_report/market_data_service.py` | **新建** — 共享市场数据服务 |
| `stockanalysis_scraper.py` (根目录) | **替换** — 用 daily_report 版 (20 字段超集) |
| `daily_report/scripts/stockanalysis_scraper.py` | **删除** — 合并到根目录版 |
| `daily_report/scripts/fetch_and_calc.py` | **修改** — 改用 MarketDataService |
| `daily_report/scripts/gen_chart.py` | **修改** — 改用 MarketDataService 或快照 |
| `qwen_forward_pe.py` | **标注 deprecated** (不删除) |
| `tests/test_market_data_characterization.py` | **新建** — characterization tests |

### 不修改的文件

| 文件 | 原因 |
|------|------|
| `stock_watch_list_back_end.py` | 后端改造可在后续阶段独立进行 (降低风险) |
| `ticker_mapping.py` | 已满足需求, 无需修改 |
| `daily_report/service.py` | 编排层不变 |
| `daily_report/worker.py` | 不涉及数据层 |
| `daily_report/src/stock_daily_agent/tools.py` | 工具层不变 (仍调用 fetch_and_calc.py / gen_chart.py) |
| `daily_report/scripts/build_report.py` | 输出层不变 |

---

## 结论

### 可以安全合并的

1. **fetch_and_calc.py 和 gen_chart.py 的 yf.download 调用** — 完全相同的参数, 可通过共享快照消除重复
2. **两个 stockanalysis_scraper.py** — daily_report 版是超集, 可合并
3. **日报缺失的缓存层** — 可通过 MarketDataService 引入

### 不能合并的

1. **后端 `yf.download(auto_adjust=False)` vs K线 `yf.Ticker().history()`** — 语义不同 (缓存 vs 实时 OHLC)
2. **后端自算 Beta vs 日报 yf.info['beta']** — 算法不同
3. **后端 per-user 缓存 vs 日报全局** — 隔离粒度不同
4. **K线图的筹码分布 4h 数据** — 不应合并到日线缓存

### 最大风险

- **scraper 合并**: 根目录版替换为 daily_report 版可能改变解析行为 (虽然字段是超集)
- **fetch_and_calc.py 重构**: 脚本式代码改为函数式可能引入微妙 bug
- **gen_chart.py 数据来源改变**: 从独立下载改为复用快照, 图表可能略有不同 (如果数据时刻不同)

### 缓解措施

- **先加 characterization tests**, 锁定当前行为
- **分步迁移**, 每步独立可回滚
- **先改日报侧** (低风险), 后改后端侧 (高风险)
- **不改变 auto_adjust / Beta / Volume 指标语义**

# Stock Watch List

本项目是一个本地优先的股票观察列表和 AI 日报应用。它提供桌面端、单用户网页端、多用户网页端、独立 Flask 数据 API，以及用于邮件日报的后台 worker。

项目用于研究和数据观察，不构成投资建议。Yahoo Finance、StockAnalysis、搜索服务和模型服务都可能限流、延迟、改变字段或返回空数据。

## 功能概览

- 股票与跨市场观察列表：价格、收益率、相对 `^GSPC` 的短中期超额收益、RSI、均线偏离、布林带、成交量、估值、分析师目标价、加权相对动量和 Beta。
- 顶部情绪仪表：CNN Fear & Greed、`^VIX` 波动率压力 gauge 和 Crypto Fear & Greed。
- 市场宽度：S&P 500 与 Nasdaq 100 成分股位于 20/50/200 日均线上方的比例。
- K 线与技术图表：K 线、均线、MACD、RSI、KDJ、布林带、成交量和 Fibonacci 工具。
- 单用户与多用户 watchlist：多用户版本支持账户、独立配置、独立缓存和只读游客视图。
- AI 日报：搜索、文章正文提取、证据整理、技术指标、评分、图表和 HTML 报告。
- 日报投递：一次性邮件、按周计划、后台 worker、失败重试、队列容量与过期控制。

## 当前架构

```text
Tkinter                 单用户 Streamlit             多用户 Streamlit
app_tkinter.py          app_streamlit.py             app_streamlit_multiuser.py
       \                     |                                  |
        \--------------------+-------------- HTTP ----------------+
                                      |
                                      v
                     Flask API: stock_watch_list_back_end.py
                                      |
                         SQLite 市场数据与用户缓存
                                      |
                    yfinance / StockAnalysis / 外部指数数据

多用户 Streamlit（已登录）
        |
        +-- 同步下载日报：daily_report.service.generate_report()
        |
        +-- 邮件/计划任务：daily_report.jobs -> daily_report.worker
                                             |
                                             v
                         AI Agent / 搜索 / SSRF 防护正文抓取 / SMTP
```

### 前端与后端关系

- `app_tkinter.py` 是桌面端。它通过本地 Flask API 获取市场数据。
- `app_streamlit.py` 是单用户网页端；没有账户和邮件计划管理。
- `app_streamlit_multiuser.py` 是多用户网页端；包含登录、每用户 watchlist、AI 报告下载、邮件任务和周计划。
- `stock_watch_list_back_end.py` 是市场数据 API。开发模式下 Streamlit 可以启动本地后台线程；生产环境应把 Flask 作为独立服务运行。
- `daily_report.worker` 是独立进程，只处理持久化的邮件报告任务和周计划物化，不依赖浏览器会话。

## Watchlist 指标

观察列表中的相对动量列统一以 `^GSPC` 为基准：

- `20D Rel%`、`60D Rel%`、`120D Rel%`：标的过去 20、60、120 个交易日收益率减去 `^GSPC` 同窗口收益率，单位为百分点。
- `3/6/12M Rel%`：原相对动量列，计算为 3、6、12 个月相对 `^GSPC` 超额收益的加权值，权重为 `0.2 / 0.3 / 0.5`，单位为百分点。
- `RSI`：14 日 RSI。表格颜色以 50 为中性白色，高于 50 越多越红，低于 50 越多越绿。

多用户 Streamlit 的表格提供列组开关：

- `Show relative momentum columns`：统一显示或折叠 `20D Rel%`、`60D Rel%`、`120D Rel%` 和 `3/6/12M Rel%`，默认折叠。
- `Show financial columns`：统一显示或折叠 `Next Earnings`、`Trailing PE`、`Forward PE`、`PEG Ratio`、`Analysts`、`Price Target` 和 `Market Cap`，默认显示。
- `Show EMA deviation columns`：单独控制 EMA 偏离列，默认折叠。

表格使用固定列宽和横向滚动来压缩窄数值列；`Name` 列会以省略号显示过长名称，鼠标悬停可查看完整名称。

## 目录结构

```text
.
├── app_tkinter.py                    # Tkinter 桌面前端
├── app_streamlit.py                  # 单用户 Streamlit 前端
├── app_streamlit_multiuser.py        # 多用户 Streamlit 前端与日报 UI
├── stock_watch_list_back_end.py      # Flask 市场数据 API
├── market_data_service.py             # 共享市场数据访问层
├── multiuser_store.py                 # 用户、密码哈希与 watchlist 配置
├── ticker_mapping.py                  # ticker 格式与映射
├── config_loader.py                   # 统一 .env 加载器
├── daily_report/
│   ├── service.py                     # 同步报告子进程封装
│   ├── jobs.py                        # 邮件队列、计划和限额
│   ├── worker.py                      # 后台邮件 worker
│   ├── mailer.py                      # SMTP 投递
│   ├── run_report.py                  # 日报 CLI 入口
│   ├── scripts/                       # 行情、图表和 HTML 报告脚本
│   └── src/stock_daily_agent/
│       ├── cli.py                     # Agent CLI
│       ├── tools.py                   # Agent 工具、证据和评分流程
│       └── article_fetcher.py         # SSRF 防护的正文抓取模块
├── deploy/                            # worker 用户与 systemd 模板
├── tests/                             # pytest 测试
├── requirements.in                    # 顶层运行时依赖
├── requirements.txt                   # 锁定后的运行时依赖
├── requirements-dev.in / .txt         # 开发与测试依赖
└── .env.example                       # 配置模板；复制为本地 .env
```

## 安装依赖

要求 Python 3.10+。在项目根目录创建虚拟环境：

```bash
python -m venv .venv
```

Windows：

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -r requirements-dev.txt
```

macOS / Linux：

```bash
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -r requirements-dev.txt
```

`requirements.txt` 是运行时锁定文件；只运行应用时可不安装开发依赖。依赖升级应先修改 `.in` 文件，再在受控环境中重新生成锁定文件并运行测试。

## 配置与 `.env`

先复制模板，然后只在本机或部署环境填写真实值：

```bash
copy .env.example .env        # Windows 命令提示符
# 或：cp .env.example .env    # macOS / Linux
```

不要把 `.env` 提交到 Git。

统一加载规则由 `config_loader.load_project_env()` 定义：

1. 已存在的进程环境变量优先。
2. 调用方显式传入的 env 文件其次。
3. 项目根目录的 `.env` 再次。
4. 最后使用代码中的默认值。

除非调用方明确传入 `override=True`，`.env` 不会覆盖现有进程环境变量；项目根目录由代码位置确定，不依赖当前工作目录。Flask 后端、日报 worker 和日报 CLI 都遵循这一规则。

常用配置包括：

- `STOCK_API_BASE_URL`：前端访问 Flask API 的地址，默认本机 `http://127.0.0.1:5000`。
- `STOCK_DEV_MODE`：`1` 允许 Streamlit 在开发时尝试启动本地 Flask；`0` 表示只连接独立后端。
- `STOCK_CACHE_DB_PATH`：市场缓存 SQLite 覆盖路径。
- `REPORT_JOB_DB`：日报队列 SQLite 覆盖路径。
- `REPORT_*`：日报队列、下载、计划、邮件和 worker 限额。
- `DASHSCOPE_API_KEY`、`DEEPSEEK_API_KEY`、`OPENAI_API_KEY`、`SERPER_API_KEY`：按所选提供商配置。模板中的 `your_...` 仅为占位符。

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

   成功时返回服务标识、状态和版本。前端会通过此接口确认目标端口确实是本项目后端。

4. 另开终端启动所需前端之一：

   ```bash
   streamlit run app_streamlit.py
   streamlit run app_streamlit_multiuser.py
   python app_tkinter.py
   ```

5. 若要处理邮件任务，再开一个终端启动 worker：

   ```bash
   python -m daily_report.worker
   ```

开发模式下，两个 Streamlit 前端可以尝试启动本地 Flask；生产环境应设置 `STOCK_DEV_MODE=0` 并由独立进程管理 API。

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

`GET /api/stock_data` 仅为兼容旧客户端保留，并在响应中标记为弃用。市场宽度接口使用 `POST /api/breadth_data`；K 线接口为 `GET /api/kline_data`。

响应数据会包含 watchlist 表格所需的价格、收益率、RSI、相对动量、估值、财报日期、分析师评级和市值字段。相对动量相关字段为 `20D Rel%`、`60D Rel%`、`120D Rel%` 和 `3/6/12M Rel%`；后端会在价格请求中隐式加入 `^GSPC` 用于基准计算，但不会把它额外显示为用户 watchlist 行，除非用户自己配置了 `^GSPC`。

## SQLite 数据位置

- 市场缓存默认位于项目根目录的 `stock_cache.db`；使用 `STOCK_CACHE_DB_PATH` 可覆盖。
- 多用户数据默认位于项目根目录的 `watchlist_users.db`。
- 报告队列默认位于项目根目录的 `daily_report_jobs.db`；使用 `REPORT_JOB_DB` 可覆盖。
- 多用户市场缓存位于 `user_data/`，由受限的 cache key 派生。
- 每次报告生成会使用 `daily_report/runs/` 下的临时目录；服务完成后会清理运行产物。

数据库、缓存和用户数据均属于运行时数据，不应提交。

## AI 日报

### CLI

日报 CLI 从项目根目录运行。示例：

```bash
python daily_report/run_report.py AAPL --months 3 --search-provider auto
```

常用选项包括 `--provider`、`--model`、`--search-provider`、`--no-article-fetch`、`--run-dir` 与 `--output`。CLI 会创建独立 run directory，调用 Agent 依次获取行情、检索证据、可选正文、技术说明、图表和最终 HTML。

### 邮件、worker 与周计划

多用户 Streamlit 的 **Generate & Email** 提供：

- 一次性邮件任务；
- 一个 ticker 对应一个周计划，可勾选周一至周日任意组合；
- 一个计划使用一个统一的 Europe/Berlin 发送时间；
- 每个账户最多 7 个 ticker 计划；
- 暂停、恢复、删除计划；
- worker 将到期计划物化为持久化邮件任务，再生成报告并通过 SMTP 发送。

worker 必须独立启动。关闭浏览器不会取消已入队的邮件任务。队列会执行每账户、全局 pending/running、重试次数和过期时间限制；具体值由 `REPORT_*` 配置控制。

### systemd 专用用户部署

生产环境不要以 root 身份运行 worker。仓库提供：

- `deploy/setup-worker-user.sh`：创建专用系统用户、数据目录权限和队列数据库迁移的辅助脚本；
- `deploy/stock-watchlist-report-worker.service`：worker 的 systemd 模板。

部署前应根据实际安装位置和 Python 虚拟环境，审查并替换 unit 文件中的 `WorkingDirectory`、`ExecStart`、`EnvironmentFile` 与 `ReadWritePaths`。然后由管理员安装 unit、执行 daemon reload、启用服务，并通过 `systemd-analyze security` 检查隔离设置。

该 unit 的目标是：专用非 root 用户、最小文件写入范围、无新权限、受限 capabilities、私有临时目录、资源上限和受限地址族。

## 安全边界

### HTML 报告

yfinance 元数据、搜索摘要、文章文本和 LLM notes 都是不可信输入。报告生成器会转义文本，并对白名单 CSS class、颜色和标的类型做约束。仅本地生成的图表片段被当作受信 HTML。

### 文章抓取与 SSRF

文章抓取只允许 HTTP/HTTPS，拒绝 URL 凭据、非允许端口、loopback、私网、link-local、multicast、reserved、unspecified 和云 metadata 地址。初始 URL 与每个重定向目标都会重新验证；请求固定连接到已验证 IP，并限制重定向数和响应大小。

这不替代网络层出站控制。面向公网部署时，仍应使用防火墙、隔离的 outbound proxy 或等价网络策略。

### 报告权限与成本控制

AI 报告下载和邮件任务要求登录。系统对账户的每日任务、活动生成和全局运行数应用数据库限制；公网部署还应在反向代理层配置 IP 限流、认证与审计。

### 登录保护

密码采用 PBKDF2-SHA256、随机 salt 和常量时间比较。登录失败按用户名计数并临时锁定；管理员创建用户时应使用交互式密码提示，不要将密码放进 shell history 或命令行参数。

### worker 与 SMTP

worker 应以专用低权限用户运行。SMTP 的成功确认与本地状态写入不是同一原子事务，因此无法严格保证 exactly-once 投递；固定 Message-ID 和状态记录只能降低重复邮件概率。用户界面应将“可能已发送”的状态视为需要确认的投递状态。

## 数据隐私与 Git 规则

以下内容不得提交：

- `.env`、API key、SMTP 凭据、密码和真实收件人邮箱；
- SQLite 数据库、WAL/SHM 文件、缓存、运行日志和日报输出；
- `user_data/`、`daily_report/runs/` 及任何用户生成内容。

只提交 `.env.example` 中的占位符。报告、搜索证据和邮件任务可能含有用户 ticker、收件人或研究内容，应按照所在环境的数据保留策略处理。

## 测试

运行完整测试：

```bash
python -m pytest -q
```

运行特定安全或日报测试：

```bash
python -m pytest tests/test_report_html_escape.py tests/test_article_url_security.py -q
python -m pytest tests/test_weekly_schedule_multiday.py tests/test_queue_capacity.py -q
```

修改 Python 文件后，至少运行语法检查和相关测试：

```bash
python -m py_compile path/to/modified_file.py
python -m pytest path/to/relevant_test.py -q
```

## 当前已知限制

- 外部数据源、模型和搜索 API 受网络、费用、配额和供应商行为影响。
- 首次加载或大范围市场宽度计算可能较慢。
- AI 报告仍会执行同步下载生成；虽然要求登录并有限额，公网部署必须补充反向代理限流和容量监控。
- SMTP 无法提供严格 exactly-once 语义，极端崩溃窗口下仍可能重复投递。
- 日报 HTML 可在离线环境查看，但文件体积可能较大，因为图表脚本会嵌入报告。
- 本项目不提供账户自助注册、支付、投资建议、交易执行或数据源 SLA。

## 许可证

如无另行说明，请将本项目视为个人研究工具；在分发或部署前请自行确认所使用数据源、模型和邮件服务的许可与条款。

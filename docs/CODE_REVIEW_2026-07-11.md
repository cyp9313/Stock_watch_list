# Stock Watch List 完整仓库 Review

审查基于当前 `main` 分支最新提交 `7ebb70a62ef1b614c95164d36b35d9a29fbac256`，覆盖：

* Tkinter 前端
* 单用户 Streamlit
* 多用户 Streamlit
* Flask 后端
* SQLite 行情、用户与任务数据库
* StockAnalysis 抓取
* `daily_report/` Agent、工具、Skill、评分和 HTML 报告
* 邮件队列、周报调度与 worker
* systemd、环境变量、依赖和 README

这是一次**仓库级静态审查**。我没有实际调用付费模型、SMTP 或外部搜索服务执行端到端报告，因此运行时性能、模型输出质量和真实限流情况尚未实测。

## 总体评价

| 维度     |     评分 | 评价                                  |
| ------ | -----: | ----------------------------------- |
| 功能完整度  | 8.5/10 | 看板、技术分析、多用户、AI 日报、邮件和定时任务都已具备       |
| 金融逻辑设计 | 7.5/10 | 标的类型区分、缺失评分重分配、证据审计思路较好             |
| 稳定性    | 5.5/10 | 网络请求、SQLite、同步长任务和多套数据链路带来风险        |
| 安全性    |   4/10 | 报告 HTML 注入、SSRF、游客高成本生成是关键问题        |
| 可维护性   | 4.5/10 | 多个超大文件和重复实现已开始产生明显技术债               |
| 测试能力   |   2/10 | 没有看到成体系测试，而且 `.gitignore` 会忽略常见测试文件 |
| 部署成熟度  |   5/10 | 已有 worker/systemd，但权限隔离和进程架构还不够成熟   |

**目前不建议直接将多用户版本无保护地暴露到公网。**

---

# 一、仓库的真实架构

项目现在已经不只是 README 开头所说的“两种前端 + 一个 Flask 后端”，实际包含：

```text
Tkinter
Single-user Streamlit
Multi-user Streamlit
        │
        ├── Embedded Flask backend
        │     ├── yfinance
        │     ├── StockAnalysis
        │     ├── stock_cache.db
        │     └── per-user stock cache DB
        │
        ├── watchlist_users.db
        │
        └── Daily Report
              ├── synchronous subprocess generation
              ├── Qwen-Agent
              ├── Serper / SearXNG / DashScope search
              ├── article body fetching
              ├── technical and fundamental scripts
              ├── HTML report renderer
              ├── daily_report_jobs.db
              ├── background worker
              ├── SMTP delivery
              └── weekly scheduler
```

多用户 Streamlit 直接导入 Flask 后端、日报 service、邮件任务和用户存储，因此这些模块都属于同一个应用运行链路。 README 后半部分已经补充日报说明，但前面的“项目结构”和“技术架构”仍然停留在旧版本，没有把多用户前端、任务队列和 worker 纳入主架构。

---

# 二、P0：必须优先修复

## P0-1：生成的 HTML 报告存在注入/XSS 风险

这是当前最明确的安全问题。

日报中的新闻内容来自搜索结果、文章正文和 LLM，但 `build_news_html()` 直接把 `text` 拼接到 HTML：

```python
<div class="news-summary">{text}</div>
```

没有进行 HTML 转义。

公司名称、交易所、ticker、描述等 yfinance 数据也直接通过字符串相加写入 HTML：

```python
html += '<h1>' + LONG_NAME + '</h1>'
html += '<div class="logo">' + TICKER + '</div>'
```

公司简介 `desc_short` 和所有消息面内容同样未经转义。

攻击场景并不要求用户手动输入恶意 HTML。只要以下任一来源包含标签即可：

* 被污染的搜索摘要
* 恶意新闻网页正文
* LLM 输出
* 异常的公司名称或描述
* 搜索提示注入产生的 HTML

生成的报告被下载或作为附件打开后，可能执行脚本、加载远程资源、伪造页面或外传数据。

### 修复建议

不要继续手工拼接 HTML，改成 Jinja2 并保持 autoescape：

```python
from jinja2 import Environment, FileSystemLoader, select_autoescape

env = Environment(
    loader=FileSystemLoader(template_dir),
    autoescape=select_autoescape(["html", "xml"]),
)
```

紧急修复至少要对所有文本执行：

```python
from html import escape

safe_text = escape(str(text), quote=True)
```

需要转义：

* ticker、公司名、描述、行业、交易所
* 新闻标题、fact、logic、investment meaning
* rating text、method、status
* 所有来源名称和 URL 显示文本

URL 还必须单独验证 `https/http` scheme，不能只做 `html.escape()`。

---

## P0-2：文章正文抓取存在 SSRF 风险

`_fetch_article_text()` 会对搜索结果给出的任意 URL 执行：

```python
requests.get(
    url,
    allow_redirects=True,
)
```

但没有检查：

* URL scheme
* localhost
* 私网地址
* link-local 地址
* 云服务器 metadata 地址
* DNS 解析结果
* 重定向后的最终地址

因为 URL 来自 Serper、SearXNG 或其他搜索来源，攻击者可以通过搜索结果污染诱导服务器访问：

```text
http://127.0.0.1:5000/...
http://localhost:8501/...
http://169.254.169.254/...
http://10.x.x.x/...
http://192.168.x.x/...
```

如果部署在云服务器上，这可能访问内部服务或云 metadata。

### 修复建议

抓取前和每一次 redirect 后都必须：

1. 只允许 `http` 和 `https`
2. DNS 解析域名
3. 拒绝 loopback、private、link-local、multicast、reserved 地址
4. 禁止带用户名密码的 URL
5. 限制响应大小
6. 限制 redirect 次数
7. 最好通过隔离的 outbound proxy 抓取

不能只检查字符串中的 `localhost`，必须检查实际解析出的 IP。

---

## P0-3：游客可以无限同步触发高成本 Agent 报告

多用户页面允许未登录游客进入 `Generate & Download`，直接执行：

```python
generate_report(...)
```

只有邮件任务要求登录。

每一次生成都会启动独立 Python subprocess，默认最多运行 **1800 秒**，并进行：

* yfinance 请求
* StockAnalysis 请求
* 搜索 API 请求
* 文章正文抓取
* 多次 LLM 调用
* 图表和 HTML 生成

当前同步下载流程没有：

* 游客限流
* 用户每日额度
* 全局并发上限
* CAPTCHA
* API 成本配额
* 单 IP 限制
* 同一 session 的 active-job 限制

如果部署到公网，少量并发请求就可能耗尽：

* CPU 和内存
* Qwen/DeepSeek token 额度
* Serper 配额
* Yahoo/StockAnalysis 请求额度
* Streamlit worker 线程

### 修复建议

所有报告，包括下载报告，都通过任务队列生成：

```text
Streamlit → enqueue report job → worker → result storage → download
```

同时增加：

* 必须登录才能生成 Agent 报告
* 每用户每日生成额度
* 全局 semaphore，例如同时最多 1–2 个
* 每用户只能有一个 active job
* IP 级限流
* 报告结果短期缓存
* 相同 ticker、参数和市场日期复用结果

---

# 三、P1：高优先级问题

## P1-1：Watchlist 和 Daily Report 有两套独立数据链路

watchlist 后端已经有：

* SQLite 增量价格缓存
* StockAnalysis 缓存
* Beta 缓存
* ticker 名称缓存

但日报又单独执行：

```python
yf.download(TICKER, period="1y")
yf.Ticker(TICKER).info
```

随后 `gen_chart.py` 又第二次下载同一个 ticker 的一年数据。

并且仓库内还有两份不同版本的 StockAnalysis scraper：

```text
/stockanalysis_scraper.py
/daily_report/scripts/stockanalysis_scraper.py
```

后者包含更多估值字段。

后果：

* 一份报告至少重复下载行情两次
* watchlist 与日报可能使用不同时间点的数据
* 指标计算口径可能逐渐分叉
* StockAnalysis 页面解析需要维护两遍
* 更容易触发 Yahoo 和 StockAnalysis 限流
* 修复一个 scraper 时可能忘记另一个

### 建议架构

提取共享模块：

```text
core/
├── market_data_service.py
├── technical_indicators.py
├── fundamentals_service.py
├── stockanalysis_provider.py
├── ticker_service.py
└── models.py
```

报告生成流程应该是：

```text
一次数据抓取
    ↓
统一 Snapshot 对象
    ├── watchlist
    ├── chart
    ├── report
    └── score
```

不要让 `fetch_and_calc.py` 和 `gen_chart.py` 分别重新下载。

---

## P1-2：`init_db()` 每次连接都执行迁移和清理

主后端的 `init_db()` 不只是“打开数据库”，还会：

* 设置 WAL
* 建表
* 建索引
* 检查并执行字段迁移
* 删除 750 天前价格
* 删除 90 天前基本面/Beta
* commit

而多种读取函数都会调用它。结果是普通读取请求也可能触发写事务，增加：

* SQLite 锁冲突
* `database is locked`
* 磁盘 I/O
* 多线程请求不稳定
* 请求延迟

而且默认数据库路径仍是：

```python
DB_PATH = "stock_cache.db"
```

位置取决于当前工作目录。

### 修复建议

拆成：

```python
initialize_schema_once()
open_connection()
run_daily_cleanup_once()
```

并使用绝对路径：

```python
DATA_DIR = Path(
    os.getenv("STOCK_WATCHLIST_DATA_DIR", BASE_DIR / "data")
).resolve()
```

---

## P1-3：邮件投递是 at-least-once，可能重复发送

worker 的处理顺序是：

```python
send_report_email(...)
mark_job_sent(job_id)
```

如果邮件已经发送成功，但 worker 在 `mark_job_sent()` 前崩溃，重启后 `recover_interrupted_jobs()` 会把 `sending` 任务重新放回队列。

结果是同一报告可能发送两次。

### 修复建议

可以采用：

* 固定使用 job ID 生成 Message-ID
* 增加 `send_started_at`、`smtp_message_id`、`delivery_state`
* 使用可查询投递状态的邮件服务
* 对同一 job 的 SMTP 投递进行幂等处理
* UI 明确显示“可能已发送，状态待确认”

传统 SMTP 很难实现严格 exactly-once，但至少要避免无条件重发。

---

## P1-4：systemd worker 以 root 身份运行

当前 service：

```ini
User=root
```

而 worker 会：

* 访问互联网
* 执行 LLM 工具
* 抓取任意文章
* 启动 Python subprocess
* 解析不可信网页
* 写 SQLite 和临时 HTML

这类进程不应拥有 root 权限，尤其结合当前 SSRF 和报告注入问题。

### 修复建议

创建专用用户：

```ini
User=stockwatch
Group=stockwatch
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/Stock_watch_list/data
RestrictSUIDSGID=true
```

同时通过：

```ini
EnvironmentFile=/opt/Stock_watch_list/.env
```

显式加载环境变量。

---

## P1-5：Streamlit 内嵌 Flask 的启动判断不可靠

三个前端都采用相似方式：

* 测试 5000 端口是否开放
* 开放就认为正确后端存在
* 启动线程后固定等待两秒

问题：

* 5000 端口可能被其他程序占用
* 两秒不代表数据库和路由已经可用
* Streamlit rerun、多 session 和多进程可能竞争
* Flask 失败后 `_flask_started` 仍被设为 True
* 生产环境无法独立扩容和监控后端

### 修复建议

增加：

```text
GET /api/health
```

验证 service name、version 和数据库状态。生产环境应把 Flask 作为独立服务运行，而不是 daemon thread。

---

## P1-6：`stock_data` 仍通过 GET 传完整 JSON

多用户前端把整个 watchlist 配置序列化进 query string：

```python
requests.get(
    "/api/stock_data",
    params={
        "groups": json.dumps(...),
    },
)
```

用户增加页面和 ticker 后，很容易再次触发：

* HTTP 400
* 414 URI Too Long
* 代理截断
* 浏览器/服务器日志暴露完整配置

应改为 POST JSON。

---

## P1-7：登录没有防暴力破解措施

身份验证本身使用 PBKDF2-SHA256、随机盐和常量时间比较，这部分是好的。

但 UI 登录失败后可以立即无限重试，没有：

* 失败次数记录
* IP 限流
* 用户锁定
* 延迟
* 审计日志
* CAPTCHA

另外管理员创建用户时，密码作为命令行参数：

```bash
python multiuser_store.py create-user alice password
```

密码可能出现在：

* shell history
* 进程列表
* 运维日志

应改为 `getpass.getpass()` 或从 stdin 读取。

---

## P1-8：周报数量与单 worker 吞吐量不匹配

每个账户默认可创建 10 个周报计划。到期计划会全部物化成任务，而 worker 一次只串行处理一个任务。

单份报告最长可运行 30 分钟。多个用户在同一时间设置周报时，任务可能排队数小时。

而定时任务不经过手动任务的“一个 active job”和每日三次限制。

建议增加：

* 全局队列长度上限
* 每用户 pending schedule job 上限
* 计划时间错峰
* 多 worker，但必须先移除 process-global Agent context
* 任务最长排队时间
* 过期任务跳过策略

---

# 四、P2：中等优先级问题

## 1. 跨市场报告的货币符号错误

`fetch_and_calc.py` 已读取真实 `CURRENCY`，但报告模板几乎所有价格都硬编码为 `$`：

```python
'$' + last_str
'$' + fw52lo_str
'$' + target_price
```

因此：

* 港股会显示美元
* A 股会显示美元
* 德股会显示美元
* 英国 pence 也会显示美元

这会造成实质性的金融信息误导。

应生成统一的：

```python
format_price(value, currency, ticker)
```

并正确支持 USD、HKD、CNY、EUR、GBX、JPY 等。

---

## 2. 图表标题硬编码“近3个月”

即使用户选择 1–24 个月，最终报告仍显示：

```text
近3个月K线图
```

应把 `months` 写入 data JSON 或 report builder 参数。

---

## 3. 报告日期使用服务器本地日期

`generate_report()` 使用：

```python
datetime.date.today()
```

这不一定等于标的市场日期。欧洲凌晨、美东盘后和亚洲市场会出现日期口径不一致。

应明确：

* 行情数据日期
* 报告生成 UTC 时间
* 标的市场日期
* 用户本地显示时间

---

## 4. HTML 报告依赖外部 Plotly CDN

图表使用：

```python
include_plotlyjs="cdn"
```

因此邮件附件并非真正离线报告：

* 无网络时图表不显示
* CDN 被拦截时失效
* 打开附件会向第三方发请求
* 增加隐私和供应链风险

邮件版本建议内嵌 Plotly JS，下载版本可以提供“轻量 CDN”和“完全离线”两种模式。

---

## 5. 主 watchlist 加载失败会让日报也无法使用

多用户页面在创建 tabs 之前先加载股票数据；失败后直接：

```python
st.stop()
```

即使日报 worker、模型和搜索都正常，只要 watchlist 后端失败，AI Reports 标签也不可用。

应让各功能页独立失败，不要让一个数据源阻断整个应用。

---

## 6. README 结构说明已经过时

README 后面补充了日报和多用户说明，但主项目结构仍没有列出：

* `app_streamlit_multiuser.py`
* `multiuser_store.py`
* `daily_report/`
* `daily_report_jobs.db`
* `daily_report.worker`
* `deploy/`

这也是上一轮 review 漏掉日报的直接原因之一。

建议以当前真实架构重新写项目结构，而不是只在文档末尾追加新章节。

---

## 7. 环境变量加载存在口径漂移

README 说日报使用根目录 `.env`。

但 Agent CLI 的自定义 loader 会读取传入 project root 下的 `.env`，而 CLI 的 project root 实际是 `daily_report/`。

worker 会显式读取仓库根目录 `.env`，所以：

* worker 模式通常正常
* Streamlit 模式可能依赖后端 import 时碰巧加载根 `.env`
* 直接运行 `daily_report/run_report.py` 时可能找不到根 `.env`

此外 `.env.example` 没有列出 README 中提到的 `DEEPSEEK_API_KEY`、model server 等配置。

建议全仓只使用一个配置模块和一个根 `.env`。

---

## 8. 缺少测试体系，而且 Git 会忽略测试文件

`.gitignore` 包含：

```text
test_*.py
_test_*.py
```

这会把绝大多数 pytest 文件直接忽略。

至少需要测试：

```text
tests/
├── test_ticker_mapping.py
├── test_market_breadth.py
├── test_beta.py
├── test_stockanalysis_parser.py
├── test_report_html_escape.py
├── test_article_url_security.py
├── test_report_queue.py
├── test_weekly_dst.py
├── test_user_isolation.py
└── test_api_routes.py
```

尤其必须加入：

* HTML 注入测试
* SSRF 私网地址测试
* SMTP 重试幂等测试
* worker 崩溃恢复测试
* 多用户缓存隔离测试

---

## 9. 依赖没有锁定

基础依赖几乎都没有版本范围，只有 Agent 相关包设置最低版本。

yfinance、pandas、Streamlit、Plotly 和 Qwen-Agent 都属于行为可能随版本变化的依赖。

建议：

```text
pyproject.toml
uv.lock
```

或：

```text
requirements.in
requirements.lock
```

并把依赖拆成：

```text
core
desktop
web
daily-report
dev/test
```

---

## 10. 超大文件已经形成维护瓶颈

当前主要大文件包括：

* `stock_watch_list_back_end.py`
* `app_streamlit_multiuser.py`：约 2375 行
* `daily_report/src/.../tools.py`：约 2960 行
* `fetch_and_calc.py`：约 660 行
* `build_report.py`：约 550 行
* `jobs.py`：约 700 行

`tools.py` 同时包含：

* 搜索
* 文章抓取
* HTML 提取
* 证据评级
* notes 验证
* 技术面摘要
* 估值评分
* 风险评分
* 最终评级
* 文件写入

建议按 provider、evidence、scoring、tools、rendering 拆分。

---

## 11. 市场宽度缺失值被画成 0

breadth payload 把 NaN 转为 `0`：

```python
... if not np.isnan(x) else 0
```

缺失值不代表 0% 成分股位于均线上方。这样会产生假的极端超卖点。

应返回 `None`。

---

## 12. 全局忽略 warnings 和大量静默异常

主后端和日报数据脚本都有：

```python
warnings.filterwarnings("ignore")
```

同时不少请求失败直接：

```python
except Exception:
    pass
```

例如 `Ticker.info` 失败后所有基本面会变成 0 或空值。

金融项目里这很危险，因为“缺失”和“真实为 0”可能被混淆。

---

# 五、做得好的地方

## 1. 日报运行目录隔离和清理做得较好

每次生成使用 UUID 独立目录，成功、失败和超时都会在 finally 中清理。

## 2. subprocess 没有使用 `shell=True`

命令通过 argv list 执行，显著降低了 shell command injection 风险。

## 3. 用户密码存储设计合理

使用：

* 16 字节随机 salt
* PBKDF2-SHA256
* 200,000 iterations
* `hmac.compare_digest`

## 4. 邮件任务数据库处理比主缓存数据库成熟

任务数据库具备：

* 绝对路径

* chmod 600

* busy timeout

* WAL

* `BEGIN IMMEDIATE`

* 唯一的 schedule occurrence 索引

## 5. 隐私清理意识较好

邮件成功或最终失败后，会清除完整 recipient email 和 HTML BLOB，最终状态只保留一段时间。

## 6. Agent 证据链设计方向正确

最终 notes 要绑定本地 evidence ID，系统还会记录：

* evidence grade
* evidence origin
* allowed uses
* support excerpt
* source domain

相比单纯让 LLM 自由生成消息面，这种设计可靠得多。

## 7. v5.8 的标的自适应评分思路合理

EQUITY、ETF、INDEX、CRYPTO 使用不同名义权重，缺失或不适用的评分项会被排除后重新归一化，不会简单填充 50 分。指数和 ETF 仍保留 OHLCV、成交量和筹码峰技术分析。

这部分符合你之前提出的“指数和 ETF 技术面统一处理，差异只放在估值、分析师和最终评分适用性”的要求。

---

# 六、推荐修复顺序

## 第一批：安全热修复

1. 对报告模板所有外部文本做 HTML 转义
2. 给正文抓取增加 SSRF 防护
3. 禁止游客无限生成 Agent 报告
4. 增加用户和全局生成限流
5. worker 改用非 root 用户
6. 不再向普通用户展示完整 stdout/stderr

## 第二批：稳定性

1. 所有报告统一走任务队列
2. 解决 SMTP 重复发送问题
3. `init_db()` 拆分初始化、连接和清理
4. SQLite 路径全部绝对化
5. Flask 改为独立服务，并增加 health endpoint
6. `/api/stock_data` 改为 POST JSON
7. breadth NaN 改为 `None`

## 第三批：数据架构

1. 合并两套 StockAnalysis scraper
2. 报告复用 watchlist 的行情和基本面 service
3. 一次行情快照同时供指标、图表和报告使用
4. 统一 ticker、currency、market date 模型
5. 区分 `missing`、`not applicable` 和真实数值 0

## 第四批：工程质量

1. 拆分三个超大模块
2. 建立 pytest 测试
3. 移除 `.gitignore` 中的测试规则
4. 锁定依赖
5. 重写 README 架构图和目录树
6. 建立 CI：lint、type check、unit test、security test

---

# 最终结论

这个项目的核心功能和金融分析思路已经相当完整，尤其日报系统不只是“调用一次 LLM”，而是已经具备搜索、正文、证据绑定、评分审计、临时目录、邮件队列和定时任务，这一点做得比普通个人项目成熟。

但新增日报以后，项目的安全边界发生了本质变化：

> 它现在是一个会访问任意网页、调用付费 API、执行子进程、生成可执行 HTML、存储用户邮箱并自动发邮件的多用户 Web 应用。

因此，当前最重要的已经不是继续增加指标，而是先修复：

1. **报告 HTML 注入**
2. **正文抓取 SSRF**
3. **游客无限同步生成**
4. **root worker**
5. **重复数据链路和同步长任务**

解决这些问题后，这个仓库才适合从“个人工具”升级为相对安全、可长期运行的公网多用户服务。

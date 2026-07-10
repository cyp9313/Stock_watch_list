---
name: stock-daily-report
description: "生成股票/加密货币/ETF每日投资日报（暗色主题交互式HTML）。支持美股、港股、加密货币（BTC-USD）、ETF（QQQ）等所有yfinance标的。报告含K线图（绿涨红跌）、均线MA5–MA200、布林带、MACD、RSI、KDJ、基本面概要与技术评级。当用户说「生成XXX日报」「分析XXX」「做XXX报告」时触发。"
agent_created: true
---

# stock-daily-report — 通用金融日报生成 Skill

## 目的

生成任意股票、加密货币或 ETF 的暗色主题交互式 HTML 日报，内容包含完整技术分析、
基本面概要（个股/ETF 优先使用 StockAnalysis，行情和技术数据使用 yfinance）以及结构化证据驱动的消息面资讯。

## 使用场景

- 用户请求生成单个或多个标的的日报（"帮我做 BTC-USD 日报"）
- 用户提供 ticker 代码让你分析行情
- 定期自动化日报任务（配合 automation）

## 前置依赖

```bash
pip install yfinance pandas numpy plotly
```

## 三脚本工作流

本 skill 包含三个核心脚本，必须按顺序执行：

### 脚本1：数据获取与技术指标计算
**文件**: `scripts/fetch_and_calc.py`

```bash
# 用法
python scripts/fetch_and_calc.py <TICKER> [OUTPUT_JSON]

# 示例
python scripts/fetch_and_calc.py ORCL orcl_data.json
python scripts/fetch_and_calc.py BTC-USD btc_data.json
python scripts/fetch_and_calc.py QQQ qqq_data.json
```

输出一个 JSON 文件，包含：
- 价格数据（最新收盘、涨跌、今日高低开、成交量）
- 52周高低及当前分位
- 标的类型与评分模型（EQUITY / ETF / INDEX / CRYPTO）
- 个股/ETF 的 StockAnalysis 估值字段（适用时），缺失字段明确标记为 N/A
- 指数和加密货币不适用的估值/分析师字段不会写成数值 0
- 均线（MA5/10/20/50/120/200）及相对位置信号
- 布林带（BB_UP/BB_MID/BB_DN）
- MACD（MACD线/信号线/柱状图）
- RSI(14)
- KDJ(9,3,3)
- ATR(14)、成交量均线、20日实现波动率、63日最大回撤
- 63/126/252 日筹码峰 / Volume Profile；ETF 与指数使用相同的 OHLCV 技术分析逻辑

### 脚本2：K线图生成
**文件**: `scripts/gen_chart.py`

```bash
# 用法
python scripts/gen_chart.py <TICKER> [OUTPUT_HTML] [--months N]

# 示例（默认近3个月）
python scripts/gen_chart.py ORCL orcl_chart.html
python scripts/gen_chart.py BTC-USD btc_chart.html --months 6
```

输出可嵌入 HTML 的图表片段（div + Plotly script，无 html/head/body 标签）。
图表使用红涨绿跌（中国惯例）K线配色。

### 脚本3：HTML报告拼装
**文件**: `scripts/build_report.py`

```bash
# 用法
python scripts/build_report.py <DATA_JSON> <CHART_HTML> <OUTPUT_HTML> [--date YYYY-MM-DD] [--notes NOTES_FILE]

# 示例（不含消息面）
python scripts/build_report.py orcl_data.json orcl_chart.html orcl-report-2026-05-21.html

# 示例（含消息面）
python scripts/build_report.py btc_data.json btc_chart.html btc-report.html --notes btc_notes.txt
```

## 完整一键流程（推荐模板）

```bash
# 设定变量
TICKER=ORCL
DATE=$(date +%Y-%m-%d)
WD=/path/to/workspace  # 替换为实际工作目录

cd $WD

# 步骤1：获取数据
python ~/.workbuddy/skills/stock-daily-report/scripts/fetch_and_calc.py $TICKER ${TICKER}_data.json

# 步骤2：生成K线图
python ~/.workbuddy/skills/stock-daily-report/scripts/gen_chart.py $TICKER ${TICKER}_chart.html

# 步骤3：生成报告
python ~/.workbuddy/skills/stock-daily-report/scripts/build_report.py \
  ${TICKER}_data.json ${TICKER}_chart.html \
  ${TICKER}-report-${DATE}.html \
  --date $DATE
```

Windows 下将 `~/.workbuddy` 替换为 `C:/Users/<用户名>/.workbuddy`。

## 消息面注释文件格式

使用 `--notes notes.txt` 传入消息面资讯，文件格式为纯文本，每行一条：

```
[BULL] 积极利好资讯内容
[BEAR] 负面利空资讯内容
[MIX] 中性或混合资讯内容
```

参见 `assets/notes_example.txt` 查看示例。

### ⚠️ 消息面撰写质量要求（重要）

**每条必须写为完整分析段落，而非一行摘要。** 消息面是日报中读者最关注的板块之一，
简略潦草的内容会严重拉低报告质量。每条资讯应包含：

1. **具体数据**：营收数字、增长率、目标价、交易量等硬数据
2. **逻辑推演**：该事件为何利好/利空，影响链条是什么
3. **投资含义**：对短期/中期走势的实际影响是什么

**错误示范**（过于简略）：
```
[BULL] 财报超预期
[BEAR] 估值偏高
```

**正确示范**（完整分析段落）：
```
[BULL] 【财报大超预期】Q1 FY2026营收$1438亿（同比+16%），EPS $2.84（+19%），双双超越华尔街预期。iPhone营收$852.7亿同比+23%创历史纪录，所有地理区域均创新高。强劲的基本面为当前股价提供坚实支撑，分析师集中上调目标价。
```

**覆盖维度要求**：消息面至少覆盖以下 4–6 个维度，总条数不少于 10 条：
- 最新财报/业绩数据（如有）
- 分析师评级与目标价变动
- 行业/板块动态与催化剂
- 宏观经济与政策环境
- 重大事件（产品发布、并购、监管等）
- 多空分歧与风险因素
- 技术面与基本面交叉验证
- 资金流向与市场情绪

BULL/BEAR/MIX 比例建议约为 5:4:2，确保多空观点均衡呈现。

## Ticker 格式

参见 `references/ticker_formats.md` 查看完整的 ticker 格式规则，包含：
- 美股/港股/A股/加密货币/ETF/指数的代码格式
- 各标的类型的数据可用性说明
- 常用标的速查表

## 报告结构

生成的 HTML 报告包含以下区块：
1. **头部**：标的名称、当前价格、涨跌幅
2. **KPI 概览**：按标的类型展示；个股显示市值/目标价，指数和加密货币显示波动率、回撤、ATR与成交活跃度
3. **技术面分析**：
   - 近3个月交互式K线图（可缩放/悬停）
   - 均线系统精确值（MA5–MA200）
   - 技术指标（RSI、MACD、KDJ、ATR）
   - 布林带当前值
   - 52周价位标尺
4. **基本面概要**：StockAnalysis 优先的估值数据（适用时）与评分适用性
5. **消息面资讯**：手动录入（可选）
6. **综合研判**：按标的类型使用技术、消息、估值、分析师和风险中的适用评分项

## AI 补充的最佳实践

脚本只能自动生成技术面和基本面概要部分。以下内容需要 AI 在报告中手动补充或丰富：

1. **深度基本面**：财报数据（营收/净利/EPS 逐季趋势）、业务拆分、估值对比
2. **消息面**：最新新闻、机构评级变动、催化事件（**必须写为完整分析段落，见上方质量要求**）
3. **多空研判**：Bull Case / Bear Case 论据整理
4. **操作建议**：具体买入/止损/止盈价位

补充方式：
- 运行脚本生成基础报告后，通过 WebSearch/WebFetch 搜集最新资讯
- 编制高质量 `--notes` 文件（每条含具体数据+逻辑推演+投资含义，总条数≥10）
- 调用 build_report.py 时通过 `--notes` 参数注入
- 如有特别重要的深度分析需要补充，可直接编辑生成的 HTML 文件中对应区块

**信息搜集清单**（每次生成日报前至少完成以下搜索）：
- 最新财报/业绩数据及机构解读
- 最近30天分析师评级变动（升级/降级/目标价调整）
- 行业板块动态与竞争对手动态
- 宏观经济数据（利率、通胀、就业等）及政策动向
- 标的相关的重大事件（产品发布、并购、监管、诉讼等）

## 常见问题

**Q：指数或加密货币为什么没有 PE、目标价评分？**  
A：这些字段对该类标的不适用，v5.8 会显示 N/A 并从最终权重中排除，不再显示为数值 0。Volume、Volume Ratio 和筹码峰技术分析仍正常保留。

**Q：A股数据获取失败或数据不完整**  
A：yfinance 对 A股覆盖有限。建议改用 NeoData 金融搜索工具获取 A股数据，再手动整理到报告中。

**Q：生成的报告没有消息面内容**  
A：消息面内容需要手动通过 `--notes` 参数注入，或由 AI 在报告生成后手动补充。

**Q：图表在浏览器中无法加载**  
A：图表使用 Plotly CDN，需要网络连接。若离线使用，在 `gen_chart.py` 中将 `include_plotlyjs='cdn'` 改为 `include_plotlyjs=True`（体积会增大约 3MB）。


## v5.8 评分与技术面原则

- 个股：技术 + 消息 + 估值 + 分析师 + 风险。
- ETF：技术 + 消息 + 估值 + 风险，分析师评分 N/A。
- 指数/加密货币：技术 + 消息 + 风险，估值与分析师评分 N/A。
- ETF 与指数在技术面上统一使用 yfinance OHLCV、Volume Ratio 和 63/126/252 日筹码峰，不做额外降权或代理替换。
- 风险 notes 只对 BEAR 完整扣分、对含风险因素的 MIX 半额扣分；BULL 不会因为出现“资本开支、债务、利率”等词而被自动扣分。
- 指数检索覆盖宏观、市场宽度、成分股盈利、资金流和下行风险，并使用标的类型对应的 evidence focus 做充分性检查。

# Stock Watch List

一个功能强大的美股观察列表应用，提供实时股票数据展示、技术分析图表和市场广度指标。支持 Tkinter 桌面版和 Streamlit 网页版两种界面。

## 功能特性

### 📊 核心功能

- **实时股票数据展示**
  - 实时价格、涨跌幅（1日、5日、1月、YTD）
  - 相对动量指标
  - 与各均线的偏离度（EMA5/10/20/50/100/200）
  - 布林带位置（上轨/下轨偏离度）
  - 成交量比率
  - 下一次财报日期
  - 估值指标：市盈率（Trailing/Forward）、PEG、市销率、市净率
  - 分析师评级与目标价
  - 市值信息

- **交互式 K 线图分析**
  - 基于 Plotly 的交互式 K 线图，支持缩放、平移
  - 显示均线（MA20, MA50, MA200）
  - Volume 成交量柱状图
  - RSI 相对强弱指标
  - MACD 指数平滑异同移动平均线
  - Stochastics 随机指标
  - TD Sequential 神奇九转指标（主图 + 子图）
  - 自定义斐波那契回调线绘制
  - StockAnalysis 链接快速跳转

- **市场广度指标**
  - McClellan Oscillator 麦克莱伦振荡器
  - McClellan Summation Index 麦克莱伦求和指数
  - Advance-Decline Line (AD Line) 涨跌线
  - 涨跌数量柱状图
  - 52周新高/新低统计
  - 标普500、纳斯达克、道琼斯指数表现
  - 20日/50日/200日均线以上股票占比

- **分组管理**
  - 按行业/主题分组展示股票列表（Mag7、芯片/AI、金融/加密、医疗等）
  - 大盘指标分组（市场方向、广度、利率/外汇、波动率等）
  - 滚动时表头固定，便于查看
  - 颜色高亮显示涨跌状态

### 🔧 技术特性

- **双界面支持**：Tkinter 桌面版 + Streamlit 网页版，功能完全一致
- **本地缓存**：使用 SQLite 数据库缓存股票数据，减少网络请求
- **自动更新**：定期刷新股票数据
- **统一数据来源**：通过 Flask 后端统一提供数据，双前端共用
- **智能缓存**：requests_cache 缓存 HTTP 请求

## 项目结构详解

```
Stock_watch_list/
├── app_tkinter.py              # Tkinter 桌面版前端
│   ├── 使用 tksheet 实现高性能表格
│   ├── Matplotlib 绘制技术分析图表
│   ├── 支持鼠标交互和缩放
│   └── 右键菜单功能
│
├── app_streamlit.py            # Streamlit 网页版前端
│   ├── 使用 Plotly 绘制交互式图表
│   ├── 响应式布局，支持宽屏显示
│   ├── 侧边栏导航
│   └── 固定表头的可滚动表格
│
├── stock_watch_list_back_end.py # 后端 API 服务
│   ├── Flask Web 服务器
│   ├── SQLite 数据缓存层
│   ├── yfinance 数据获取
│   ├── StockAnalysis 数据爬取
│   └── 技术指标计算
│
├── stockanalysis_scraper.py    # StockAnalysis 网站数据爬取
│   ├── 批量获取 Forward PE、PEG 等数据
│   ├── 分析师评级与目标价
│   └── 财报日期获取
│
├── qwen_forward_pe.py          # 远期市盈率数据处理（备用）
│
├── launch_tkinter.bat          # Windows 一键启动 Tkinter 版本
├── launch_streamlit.bat        # Windows 一键启动 Streamlit 版本
│
├── requirements.txt            # Python 依赖包
├── .gitignore                  # Git 忽略文件配置
└── README.md                   # 项目说明文档
```

## 技术架构

```
┌─────────────────┐         ┌─────────────────┐
│  app_tkinter.py │         │ app_streamlit.py│
│   (Tkinter UI)  │         │  (Streamlit UI) │
└────────┬────────┘         └────────┬────────┘
         │                           │
         └────────────┬──────────────┘
                      │ HTTP API
                      ▼
         ┌──────────────────────────┐
         │stock_watch_list_back_end.py│
         │    (Flask Backend)        │
         └────────────┬─────────────┘
                      │
        ┌─────────────┼─────────────┐
        ▼             ▼             ▼
   ┌──────────┐  ┌─────────┐  ┌───────────┐
   │  SQLite  │  │yfinance │  │StockAnalysis│
   │  Cache   │  │  (API)  │  │ (Scraper)  │
   └──────────┘  └─────────┘  └───────────┘
```

## 安装方法

### 1. 克隆项目

```bash
git clone https://github.com/cyp9313/Stock_watch_list.git
cd Stock_watch_list
```

### 2. 创建虚拟环境（推荐）

Windows:
```bash
python -m venv .venv
.venv\Scripts\activate
```

Linux/Mac:
```bash
python -m venv .venv
source .venv/bin/activate
```

### 3. 安装依赖

```bash
pip install -r requirements.txt
```

## 使用方法

### Windows 用户（推荐）

#### 启动 Tkinter 桌面版
双击 `launch_tkinter.bat` 文件即可自动启动 Tkinter 版本。

#### 启动 Streamlit 网页版
双击 `launch_streamlit.bat` 文件即可自动启动 Streamlit 版本。浏览器会自动打开 http://localhost:8501

### 手动启动

#### Tkinter 版本
```bash
python app_tkinter.py
```

#### Streamlit 版本
```bash
python -m streamlit run app_streamlit.py --server.port 8501 --server.address localhost
```

#### 后端服务（可选）
如果需要使用 API 服务，可以单独启动后端：
```bash
python stock_watch_list_back_end.py
```

## 如何定制 Watch List

本项目支持完全自定义你的股票观察列表。只需要修改相应文件中的分组配置即可。

### 修改 Tkinter 版本的股票列表

编辑 [`app_tkinter.py`](file:///c:/Users/Administrator/Desktop/development/Stock_watch_list/app_tkinter.py) 文件中的 `stock_groups` 字典：

```python
stock_groups = {
    "Mag7": ["AAPL", "MSFT", "GOOG", "AMZN", "META", "TSLA", "NVDA", "SPCX"],
    "Chips/AI": ["MU", "ORCL", "AMD", "INTC", "AVGO", "SMCI", "PLTR", "RGTI", "DXYZ", "SNPS", "APP"],
    "Fin/Crypto": ["V", "JPM", "BRK-B", "COIN", "HOOD", "MSTR", "CRCL", "SOFI", "OSCR"],
    "Health": ["LLY", "NVO", "ABBV", "UNH"],
    "Energy": ["SMR", "VST", "OKLO", "NEE", "ENPH", "GE", "GEV"],
    "Defense": ["LMT", "BA", "ACHR", "AXON"],
    "Consumer": ["LULU", "NKE", "CMG", "COST"],
    "China": ["BYDDY", "XIACY", "PDD", "BABA", "TCEHY", "BIDU"],
    "Themes": ["ASTS", "CRWV", "NBIS", "MP", "RKLB"],
}
```

#### 如何修改：

1. **添加新分组**：在字典中添加新的键值对，键是分组名称，值是股票代码列表
   ```python
   "My Favorites": ["AAPL", "MSFT", "GOOGL"],
   ```

2. **修改现有分组**：直接编辑分组中的股票代码列表
   ```python
   "Mag7": ["AAPL", "MSFT", "TSLA"],  # 只保留这三只
   ```

3. **删除分组**：删除对应的键值对

4. **添加单只股票**：在对应分组的列表中添加股票代码
   ```python
   "Chips/AI": ["MU", "ORCL", "AMD", "你的股票代码"],
   ```

### 修改 Streamlit 版本的股票列表

编辑 [`app_streamlit.py`](file:///c:/Users/Administrator/Desktop/development/Stock_watch_list/app_streamlit.py) 文件中的 `STOCK_GROUPS` 字典：

```python
STOCK_GROUPS = {
    "Mag7": ["AAPL", "MSFT", "GOOG", "AMZN", "META", "TSLA", "NVDA", "SPCX"],
    "Chips/AI": ["MU","ORCL", "AMD", "INTC", "AVGO", "SMCI", "PLTR", "RGTI", "DXYZ", "SNPS", "APP"],
    "Fin/Crypto": ["V", "JPM", "BRK-B", "COIN", "HOOD", "MSTR", "CRCL", "SOFI", "OSCR"],
    "Health": ["LLY", "NVO", "ABBV", "UNH"],
    "Energy": ["SMR", "VST", "OKLO", "NEE", "ENPH", "GE", "GEV"],
    "Defense": ["LMT", "BA", "ACHR", "AXON"],
    "Consumer": ["LULU", "NKE", "CMG", "COST"],
    "China": ["BYDDY", "XIACY", "PDD", "BABA", "TCEHY", "BIDU"],
    "Themes": ["ASTS", "CRWV", "NBIS", "MP", "RKLB"],
}
```

修改方法与 Tkinter 版本完全相同。**建议两个文件保持一致，方便切换使用。**

### 修改大盘指标列表

同样可以定制大盘指标（指数、外汇、商品、加密货币等）：

#### Tkinter 版本
编辑 [`app_tkinter.py`](file:///c:/Users/Administrator/Desktop/development/Stock_watch_list/app_tkinter.py) 中的 `broad_market_groups` 字典。

#### Streamlit 版本
编辑 [`app_streamlit.py`](file:///c:/Users/Administrator/Desktop/development/Stock_watch_list/app_streamlit.py) 中的 `BROAD_MARKET_GROUPS` 字典。

```python
BROAD_MARKET_GROUPS = {
    "Dashboard": [
        "^GSPC", "^NDX", "RSP", "QQQE", "^TNX",
        "EURUSD=X", "^VIX", "GC=F", "BZ=F", "BTC-USD", "510300.SS"
    ],
    "US Mkt Dir": ["^GSPC", "^NDX", "^DJI", "^RUT"],
    "Breadth": ["RSP", "QQQE"],
    "AI/Tech Risk": ["TQQQ", "^SOX"],
    "China Beta": ["510300.SS", "510050.SS", "159915.SZ", "588000.SS", "3033.HK"],
    "Rates/FX": ["^TNX", "EURUSD=X", "EURCNY=X"],
    "Fear/Vol": ["^VIX", "^VXN"],
    "Safe Haven": ["GC=F", "SI=F"],
    "Oil/Geopol": ["BZ=F"],
    "Crypto": ["BTC-USD", "ETH-USD"],
    "Strat Resources": ["WNUC.DE", "REMX"],
}
```

### 股票代码格式说明

- **美股**：直接使用代码，如 `AAPL`、`MSFT`
- **指数**：使用 Yahoo Finance 格式，如 `^GSPC`（标普500）、`^NDX`（纳斯达克100）
- **外汇**：格式为 `XXXYYY=X`，如 `EURUSD=X`（欧元兑美元）
- **商品期货**：格式为 `XXX=F`，如 `GC=F`（黄金）、`BZ=F`（布伦特原油）
- **加密货币**：格式为 `XXX-USD`，如 `BTC-USD`（比特币）
- **A股/港股**：使用相应后缀，如 `510300.SS`（沪深300）、`3033.HK`（港股）

### 配置示例

假设你想添加一个"新能源"分组，包含特斯拉、蔚来、理想、小鹏：

```python
"New Energy": ["TSLA", "NIO", "LI", "XPEV"],
```

将这一行添加到 `stock_groups` 或 `STOCK_GROUPS` 字典中即可。

## 主要功能说明

### 1. 股票观察列表
- 显示实时价格、涨跌幅、成交量等信息
- 按行业分组显示，方便管理
- 支持颜色高亮显示涨跌状态（绿色涨、红色跌）
- 滚动时表头固定，便于查看
- 20+ 个数据列，涵盖技术面和基本面

### 2. K线图分析
- 交互式 Plotly / Matplotlib K 线图
- 显示均线（MA20, MA50, MA200）
- TD Sequential 神奇九转指标（数字标注，第9个加粗显示）
- Volume 成交量柱状图
- RSI、MACD、Stochastics 等技术指标
- TD Seq 专门子图，柱状图 + 数字标注
- 自定义斐波那契回调线，支持输入点位
- StockAnalysis 链接快速跳转

### 3. 市场广度指标
- McClellan Oscillator：衡量市场内部动量
- McClellan Summation Index：长期市场广度指标
- Advance-Decline Line (AD Line)：涨跌趋势线
- 涨跌数量柱状图：直观展示市场涨跌比
- 52周新高/新低统计：识别极端情绪
- 标普500、纳斯达克、道琼斯指数表现：大盘风向标
- 均线占比指标：20日/50日/200日均线以上股票比例

## 技术栈

- **前端界面**：Tkinter + tksheet / Streamlit + Plotly
- **数据获取**：yfinance, requests, requests_cache
- **数据处理**：pandas, numpy
- **可视化**：matplotlib, mplfinance, plotly
- **后端**：Flask
- **数据库**：SQLite

## 依赖项

主要依赖包：
- streamlit - 网页应用框架
- plotly - 交互式图表
- pandas - 数据处理
- numpy - 数值计算
- matplotlib - 数据可视化
- mplfinance - 金融图表库
- yfinance - Yahoo Finance 数据接口
- Flask - 后端 Web 框架
- tksheet - Tkinter 表格组件
- requests_cache - HTTP 请求缓存
- python-dotenv - 环境变量管理
- fear_and_greed - 恐惧与贪婪指数

完整列表请见 [requirements.txt](requirements.txt)

## 配置说明

如需配置环境变量，可以创建 `.env` 文件：

```env
# 可选配置
FLASK_ENV=development
FLASK_PORT=5000
```

## 注意事项

- 首次运行会下载股票数据，可能需要几分钟
- 数据缓存在 `stock_cache.db` 中，下次启动会更快
- 建议保持稳定的网络连接以获取实时数据
- 修改股票分组后需要重启应用才能生效
- 两个前端文件的分组配置建议保持一致

## 常见问题

**Q: 如何同时使用两个版本？**
A: 两个版本共用后端，启动一个即可，也可以同时运行两个前端。

**Q: 数据更新频率是多少？**
A: 应用启动时会刷新数据，后续会定期更新。

**Q: 可以添加美股以外的股票吗？**
A: 可以，只要 Yahoo Finance 支持该股票代码即可。

**Q: 如何清空缓存重新获取数据？**
A: 删除 `stock_cache.db` 文件，重启应用即可。

## 贡献

欢迎提交 Issue 和 Pull Request！

## 许可证

MIT License

## 作者

cyp9313

---

# English Version

# Stock Watch List

A powerful US stock watchlist application providing real-time stock data display, technical analysis charts, and market breadth indicators. Supports both Tkinter desktop version and Streamlit web version.

## Features

### 📊 Core Features

- **Real-time Stock Data Display**
  - Real-time prices, price changes (1D, 5D, 1M, YTD)
  - Relative momentum indicator
  - Deviation from various EMAs (EMA5/10/20/50/100/200)
  - Bollinger Bands position (upper/lower band deviation)
  - Volume ratio
  - Next earnings date
  - Valuation metrics: PE (Trailing/Forward), PEG, P/S, P/B
  - Analyst ratings and target prices
  - Market cap information

- **Interactive Candlestick Chart Analysis**
  - Plotly-based interactive candlestick charts with zoom and pan support
  - Moving averages display (MA20, MA50, MA200)
  - Volume bar chart
  - RSI indicator
  - MACD indicator
  - Stochastics indicator
  - TD Sequential indicator (main chart + subplot)
  - Custom Fibonacci retracement drawing
  - Quick StockAnalysis link

- **Market Breadth Indicators**
  - McClellan Oscillator
  - McClellan Summation Index
  - Advance-Decline Line (AD Line)
  - Advance/Decline bar chart
  - 52-week new highs/lows statistics
  - S&P 500, Nasdaq, Dow Jones performance
  - Percentage of stocks above 20/50/200-day MAs

- **Group Management**
  - Stock list grouped by sector/theme (Mag7, Chips/AI, Fin/Crypto, Health, etc.)
  - Broad market indicator groups (market direction, breadth, rates/FX, volatility, etc.)
  - Fixed header when scrolling for easy viewing
  - Color-coded price change highlighting

### 🔧 Technical Features

- **Dual Interface Support**: Tkinter desktop + Streamlit web versions with identical functionality
- **Local Caching**: SQLite database for stock data caching to reduce network requests
- **Automatic Updates**: Periodic data refresh
- **Unified Data Source**: Flask backend provides data for both frontends
- **Smart Caching**: requests_cache for HTTP request caching

## Project Structure Details

```
Stock_watch_list/
├── app_tkinter.py              # Tkinter desktop frontend
│   ├── High-performance table using tksheet
│   ├── Matplotlib for technical analysis charts
│   ├── Mouse interaction and zoom support
│   └── Right-click menu functionality
│
├── app_streamlit.py            # Streamlit web frontend
│   ├── Plotly for interactive charts
│   ├── Responsive layout with wide-screen support
│   ├── Sidebar navigation
│   └── Scrollable table with fixed header
│
├── stock_watch_list_back_end.py # Backend API service
│   ├── Flask web server
│   ├── SQLite data cache layer
│   ├── yfinance data fetching
│   ├── StockAnalysis data scraping
│   └── Technical indicator calculation
│
├── stockanalysis_scraper.py    # StockAnalysis website scraper
│   ├── Batch fetch Forward PE, PEG and more
│   ├── Analyst ratings and target prices
│   └── Earnings date fetching
│
├── qwen_forward_pe.py          # Forward PE data processing (backup)
│
├── launch_tkinter.bat          # Windows one-click Tkinter launcher
├── launch_streamlit.bat        # Windows one-click Streamlit launcher
│
├── requirements.txt            # Python dependencies
├── .gitignore                  # Git ignore file configuration
└── README.md                   # Project documentation
```

## Technical Architecture

```
┌─────────────────┐         ┌─────────────────┐
│  app_tkinter.py │         │ app_streamlit.py│
│   (Tkinter UI)  │         │  (Streamlit UI) │
└────────┬────────┘         └────────┬────────┘
         │                           │
         └────────────┬──────────────┘
                      │ HTTP API
                      ▼
         ┌──────────────────────────┐
         │stock_watch_list_back_end.py│
         │    (Flask Backend)        │
         └────────────┬─────────────┘
                      │
        ┌─────────────┼─────────────┐
        ▼             ▼             ▼
   ┌──────────┐  ┌─────────┐  ┌───────────┐
   │  SQLite  │  │yfinance │  │StockAnalysis│
   │  Cache   │  │  (API)  │  │ (Scraper)  │
   └──────────┘  └─────────┘  └───────────┘
```

## Installation

### 1. Clone the Project

```bash
git clone https://github.com/cyp9313/Stock_watch_list.git
cd Stock_watch_list
```

### 2. Create Virtual Environment (Recommended)

Windows:
```bash
python -m venv .venv
.venv\Scripts\activate
```

Linux/Mac:
```bash
python -m venv .venv
source .venv/bin/activate
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

## Usage

### Windows Users (Recommended)

#### Launch Tkinter Desktop Version
Double-click `launch_tkinter.bat` to automatically launch the Tkinter version.

#### Launch Streamlit Web Version
Double-click `launch_streamlit.bat` to automatically launch the Streamlit version. Browser will open to http://localhost:8501 automatically.

### Manual Launch

#### Tkinter Version
```bash
python app_tkinter.py
```

#### Streamlit Version
```bash
python -m streamlit run app_streamlit.py --server.port 8501 --server.address localhost
```

#### Backend Service (Optional)
If you need to use the API service separately:
```bash
python stock_watch_list_back_end.py
```

## How to Customize the Watch List

This project supports full customization of your stock watchlist. Simply modify the group configuration in the corresponding file.

### Modify Tkinter Version Stock List

Edit the `stock_groups` dictionary in [`app_tkinter.py`](file:///c:/Users/Administrator/Desktop/development/Stock_watch_list/app_tkinter.py):

```python
stock_groups = {
    "Mag7": ["AAPL", "MSFT", "GOOG", "AMZN", "META", "TSLA", "NVDA", "SPCX"],
    "Chips/AI": ["MU", "ORCL", "AMD", "INTC", "AVGO", "SMCI", "PLTR", "RGTI", "DXYZ", "SNPS", "APP"],
    "Fin/Crypto": ["V", "JPM", "BRK-B", "COIN", "HOOD", "MSTR", "CRCL", "SOFI", "OSCR"],
    "Health": ["LLY", "NVO", "ABBV", "UNH"],
    "Energy": ["SMR", "VST", "OKLO", "NEE", "ENPH", "GE", "GEV"],
    "Defense": ["LMT", "BA", "ACHR", "AXON"],
    "Consumer": ["LULU", "NKE", "CMG", "COST"],
    "China": ["BYDDY", "XIACY", "PDD", "BABA", "TCEHY", "BIDU"],
    "Themes": ["ASTS", "CRWV", "NBIS", "MP", "RKLB"],
}
```

#### How to Modify:

1. **Add new group**: Add a new key-value pair to the dictionary, where the key is the group name and the value is a list of stock tickers
   ```python
   "My Favorites": ["AAPL", "MSFT", "GOOGL"],
   ```

2. **Modify existing group**: Directly edit the stock ticker list within a group
   ```python
   "Mag7": ["AAPL", "MSFT", "TSLA"],  # Keep only these three
   ```

3. **Delete group**: Remove the corresponding key-value pair

4. **Add single stock**: Add the stock ticker to the corresponding group list
   ```python
   "Chips/AI": ["MU", "ORCL", "AMD", "YOUR_TICKER"],
   ```

### Modify Streamlit Version Stock List

Edit the `STOCK_GROUPS` dictionary in [`app_streamlit.py`](file:///c:/Users/Administrator/Desktop/development/Stock_watch_list/app_streamlit.py):

```python
STOCK_GROUPS = {
    "Mag7": ["AAPL", "MSFT", "GOOG", "AMZN", "META", "TSLA", "NVDA", "SPCX"],
    "Chips/AI": ["MU","ORCL", "AMD", "INTC", "AVGO", "SMCI", "PLTR", "RGTI", "DXYZ", "SNPS", "APP"],
    "Fin/Crypto": ["V", "JPM", "BRK-B", "COIN", "HOOD", "MSTR", "CRCL", "SOFI", "OSCR"],
    "Health": ["LLY", "NVO", "ABBV", "UNH"],
    "Energy": ["SMR", "VST", "OKLO", "NEE", "ENPH", "GE", "GEV"],
    "Defense": ["LMT", "BA", "ACHR", "AXON"],
    "Consumer": ["LULU", "NKE", "CMG", "COST"],
    "China": ["BYDDY", "XIACY", "PDD", "BABA", "TCEHY", "BIDU"],
    "Themes": ["ASTS", "CRWV", "NBIS", "MP", "RKLB"],
}
```

The modification method is identical to the Tkinter version. **It's recommended to keep both files consistent for easy switching.**

### Modify Broad Market Indicator List

You can also customize broad market indicators (indices, forex, commodities, cryptocurrencies, etc.):

#### Tkinter Version
Edit the `broad_market_groups` dictionary in [`app_tkinter.py`](file:///c:/Users/Administrator/Desktop/development/Stock_watch_list/app_tkinter.py).

#### Streamlit Version
Edit the `BROAD_MARKET_GROUPS` dictionary in [`app_streamlit.py`](file:///c:/Users/Administrator/Desktop/development/Stock_watch_list/app_streamlit.py).

```python
BROAD_MARKET_GROUPS = {
    "Dashboard": [
        "^GSPC", "^NDX", "RSP", "QQQE", "^TNX",
        "EURUSD=X", "^VIX", "GC=F", "BZ=F", "BTC-USD", "510300.SS"
    ],
    "US Mkt Dir": ["^GSPC", "^NDX", "^DJI", "^RUT"],
    "Breadth": ["RSP", "QQQE"],
    "AI/Tech Risk": ["TQQQ", "^SOX"],
    "China Beta": ["510300.SS", "510050.SS", "159915.SZ", "588000.SS", "3033.HK"],
    "Rates/FX": ["^TNX", "EURUSD=X", "EURCNY=X"],
    "Fear/Vol": ["^VIX", "^VXN"],
    "Safe Haven": ["GC=F", "SI=F"],
    "Oil/Geopol": ["BZ=F"],
    "Crypto": ["BTC-USD", "ETH-USD"],
    "Strat Resources": ["WNUC.DE", "REMX"],
}
```

### Stock Ticker Format Guide

- **US Stocks**: Use the ticker directly, e.g., `AAPL`, `MSFT`
- **Indices**: Use Yahoo Finance format, e.g., `^GSPC` (S&P 500), `^NDX` (Nasdaq 100)
- **Forex**: Format as `XXXYYY=X`, e.g., `EURUSD=X` (EUR/USD)
- **Commodity Futures**: Format as `XXX=F`, e.g., `GC=F` (Gold), `BZ=F` (Brent Crude)
- **Cryptocurrencies**: Format as `XXX-USD`, e.g., `BTC-USD` (Bitcoin)
- **A-Share/Hong Kong Stocks**: Use appropriate suffixes, e.g., `510300.SS` (CSI 300), `3033.HK` (HK)

### Configuration Example

Suppose you want to add a "New Energy" group containing Tesla, NIO, Li Auto, XPeng:

```python
"New Energy": ["TSLA", "NIO", "LI", "XPEV"],
```

Add this line to the `stock_groups` or `STOCK_GROUPS` dictionary.

## Main Features Explained

### 1. Stock Watchlist
- Display real-time prices, price changes, volume, and more
- Grouped by sector for easy management
- Color-coded price change highlighting (green for up, red for down)
- Fixed header when scrolling for easy viewing
- 20+ data columns covering technical and fundamental metrics

### 2. Candlestick Chart Analysis
- Interactive Plotly/Matplotlib candlestick charts
- Display moving averages (MA20, MA50, MA200)
- TD Sequential indicator (numbered annotations, 9th bolded)
- Volume bar chart
- RSI, MACD, Stochastics and other technical indicators
- Dedicated TD Seq subplot with bars + number labels
- Custom Fibonacci retracement with support for price point input
- Quick StockAnalysis link

### 3. Market Breadth Indicators
- McClellan Oscillator: Measures internal market momentum
- McClellan Summation Index: Long-term market breadth indicator
- Advance-Decline Line (AD Line): Trend line of advances vs declines
- Advance/Decline bar chart: Visual display of market breadth
- 52-week new highs/lows statistics: Identify extreme sentiment
- S&P 500, Nasdaq, Dow Jones performance: Market bellwethers
- MA ratio indicators: Percentage of stocks above 20/50/200-day MAs

## Technology Stack

- **Frontend Interface**: Tkinter + tksheet / Streamlit + Plotly
- **Data Fetching**: yfinance, requests, requests_cache
- **Data Processing**: pandas, numpy
- **Visualization**: matplotlib, mplfinance, plotly
- **Backend**: Flask
- **Database**: SQLite

## Dependencies

Main packages:
- streamlit - Web application framework
- plotly - Interactive charts
- pandas - Data processing
- numpy - Numerical computing
- matplotlib - Data visualization
- mplfinance - Financial charting library
- yfinance - Yahoo Finance data interface
- Flask - Backend web framework
- tksheet - Tkinter table component
- requests_cache - HTTP request caching
- python-dotenv - Environment variable management
- fear_and_greed - Fear & Greed Index

See [requirements.txt](requirements.txt) for complete list.

## Configuration

To configure environment variables, create a `.env` file:

```env
# Optional configuration
FLASK_ENV=development
FLASK_PORT=5000
```

## Notes

- First run will download stock data and may take several minutes
- Data is cached in `stock_cache.db`, subsequent launches will be faster
- Stable internet connection recommended for real-time data
- Restart application after modifying stock groups for changes to take effect
- Recommended to keep both frontend files in sync

## FAQ

**Q: How to use both versions simultaneously?**
A: Both versions share the backend, you can launch one or both frontends simultaneously.

**Q: How often is data updated?**
A: Data is refreshed on application launch and updated periodically thereafter.

**Q: Can I add stocks outside the US market?**
A: Yes, as long as Yahoo Finance supports the ticker symbol.

**Q: How to clear the cache and refetch data?**
A: Delete the `stock_cache.db` file and restart the application.

## Contributing

Issues and Pull Requests welcome!

## License

MIT License

## Author

cyp9313

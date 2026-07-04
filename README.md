# Stock Watch List

一个功能强大的美股观察列表应用，提供实时股票数据展示、技术分析图表和市场广度指标。支持 Tkinter 桌面版和 Streamlit 网页版两种界面。

## 功能特性

- ✅ **实时股票数据**：自动获取美股实时价格和技术指标
- 📊 **K线图分析**：基于 Plotly 的交互式 K 线图，支持缩放、平移
- 🔮 **神奇九转指标**：TD Sequential 指标显示买卖信号
- 📈 **斐波那契回调**：支持自定义斐波那契点位计算
- 📉 **市场广度指标**：展示 McClellan Oscillator、Summation Index、Advance-Decline Line 等
- 🎨 **分组显示**：按行业分组展示股票列表
- 🔍 **StockAnalysis 链接**：快速跳转至股票详情页面
- 💾 **本地缓存**：使用 SQLite 数据库缓存股票数据，减少网络请求

## 项目结构

```
Stock_watch_list/
├── app_tkinter.py              # Tkinter 桌面版前端
├── app_streamlit.py            # Streamlit 网页版前端
├── stock_watch_list_back_end.py # 后端 API 服务
├── stockanalysis_scraper.py    # StockAnalysis 网站数据爬取
├── qwen_forward_pe.py          # 远期市盈率数据处理
├── launch_tkinter.bat          # Windows 启动 Tkinter 版本
├── launch_streamlit.bat        # Windows 启动 Streamlit 版本
├── requirements.txt            # Python 依赖包
├── .gitignore                  # Git 忽略文件配置
└── README.md                   # 项目说明文档
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

## 主要功能说明

### 1. 股票观察列表
- 显示实时价格、涨跌幅、成交量等信息
- 按行业分组显示，方便管理
- 支持颜色高亮显示涨跌状态
- 滚动时表头固定，便于查看

### 2. K线图分析
- 交互式 Plotly K 线图
- 显示均线（MA20, MA50, MA200）
- TD Sequential 神奇九转指标
- Volume 成交量柱状图
- RSI、MACD、Stochastics 等技术指标
- TD Seq 子图显示
- 斐波那契回调线绘制
- StockAnalysis 链接快速跳转

### 3. 市场广度指标
- McClellan Oscillator
- McClellan Summation Index
- Advance-Decline Line (AD Line)
- 涨跌数量柱状图
- 52周新高/新低统计
- 标普500、纳斯达克、道琼斯指数表现

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
- yfinance - Yahoo Finance 数据接口
- Flask - 后端 Web 框架
- tksheet - Tkinter 表格组件
- requests_cache - HTTP 请求缓存

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

## 贡献

欢迎提交 Issue 和 Pull Request！

## 许可证

MIT License

## 作者

cyp9313

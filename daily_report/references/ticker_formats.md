# Ticker 格式参考手册

## yfinance Ticker 格式规则

| 市场         | 示例                         | 说明                        |
|--------------|------------------------------|-----------------------------|
| 美股         | ORCL, AAPL, MSFT, NVDA       | 直接使用股票代码             |
| 加密货币     | BTC-USD, ETH-USD, SOL-USD    | `代码-USD` 格式              |
| ETF (美)     | QQQ, SPY, IWM, ARKK          | 直接使用 ETF 代码            |
| 港股         | 0700.HK (腾讯), 9988.HK (阿里)| `代码.HK` 格式（4-5位数字）  |
| A股 (上交所) | 600519.SS (茅台), 601318.SS  | `代码.SS` 格式               |
| A股 (深交所) | 000858.SZ (五粮液)           | `代码.SZ` 格式               |
| 纳斯达克指数 | ^NDX, ^COMP                  | 指数前加 `^`                 |
| 标普500指数  | ^GSPC                        | 指数前加 `^`                 |
| 道琼斯指数   | ^DJI                         | 指数前加 `^`                 |

## 常用 Ticker 速查

### 大型科技股（美股）
- AAPL — Apple
- MSFT — Microsoft  
- NVDA — NVIDIA
- GOOGL — Alphabet (Google Class A)
- AMZN — Amazon
- META — Meta Platforms
- TSLA — Tesla
- ORCL — Oracle

### 半导体
- AMD — Advanced Micro Devices
- INTC — Intel
- QCOM — Qualcomm
- AVGO — Broadcom
- TSM — TSMC (ADR)

### 金融
- JPM — JPMorgan Chase
- BAC — Bank of America
- GS — Goldman Sachs

### 中概股 (ADR/港股)
- BABA — 阿里巴巴 (ADR)
- 9988.HK — 阿里巴巴 (港股)
- 0700.HK — 腾讯
- JD — 京东 (ADR)
- PDD — 拼多多 (ADR)
- BIDU — 百度 (ADR)

### 主要加密货币
- BTC-USD — 比特币
- ETH-USD — 以太坊
- SOL-USD — Solana
- BNB-USD — Binance Coin
- XRP-USD — Ripple
- DOGE-USD — Dogecoin

### 常用 ETF
- QQQ — 纳斯达克100 ETF (Invesco)
- SPY — 标普500 ETF (SPDR)
- IWM — 罗素2000 ETF
- GLD — 黄金 ETF
- TLT — 20年+美国国债 ETF
- ARKK — ARK Innovation ETF
- SOXS/SOXX — 半导体 ETF

### 主要指数
- ^NDX — 纳斯达克100
- ^GSPC — 标普500
- ^DJI — 道琼斯
- ^VIX — 恐慌指数（VIX）
- ^TNX — 10年期美债收益率

## 注意事项

1. **加密货币**：`info` 字段 `forwardPE`、`dividendYield`、`targetMeanPrice` 等基本面数据通常为 0 或 None，属正常现象，不影响技术分析部分。
2. **指数（^开头）**：没有 `Volume` 数据，成交量相关图表会为空，属正常现象。
3. **A股代码**：yfinance 对 A股数据覆盖有限，建议配合 NeoData 金融搜索工具补充数据。
4. **港股**：数据一般完整，但分析师评级字段可能为空。
5. **ETF**：`sector`、`industry`、`employees` 等字段通常为空，基本面区块会显示 `—`。

## 数据字段可用性一览

| 字段           | 美股 | 港股 | 加密 | ETF | A股 |
|----------------|------|------|------|-----|-----|
| 价格/成交量    | ✅   | ✅   | ✅   | ✅  | ⚠️  |
| 52周高低       | ✅   | ✅   | ✅   | ✅  | ⚠️  |
| 技术指标       | ✅   | ✅   | ✅   | ✅  | ⚠️  |
| 市值/PE        | ✅   | ✅   | ❌   | ⚠️  | ⚠️  |
| 分析师目标价   | ✅   | ⚠️   | ❌   | ❌  | ❌  |
| 股息           | ✅   | ✅   | ❌   | ⚠️  | ⚠️  |
| 公司描述       | ✅   | ✅   | ⚠️   | ✅  | ⚠️  |

✅ 完整 ⚠️ 部分/不稳定 ❌ 不适用

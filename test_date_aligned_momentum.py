#!/usr/bin/env python3
"""
测试日期对齐的相对动量分数计算
"""
import yfinance as yf
import pandas as pd
import numpy as np
import sys
import os

# 添加后端代码路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def get_price_on_date(prices, target_date, max_days=5):
    """
    获取指定日期或最近交易日的价格
    prices: pd.Series with DatetimeIndex
    target_date: 目标日期
    max_days: 最多向前/后搜索多少天
    返回: (价格, 实际使用的日期) 或 (np.nan, None)
    """
    if prices is None or len(prices) == 0:
        return np.nan, None
    
    # 确保 target_date 是 pd.Timestamp
    if not isinstance(target_date, pd.Timestamp):
        target_date = pd.Timestamp(target_date)
    
    # 尝试精确匹配
    if target_date in prices.index:
        return prices[target_date], target_date
    
    # 向前搜索（找之前的交易日）
    for i in range(1, max_days + 1):
        search_date = target_date - pd.Timedelta(days=i)
        # 跳过周末
        if search_date.dayofweek >= 5:  # 5=Saturday, 6=Sunday
            continue
        if search_date in prices.index:
            print(f"    日期 {target_date.strftime('%Y-%m-%d')} 不是交易日，使用前一个交易日 {search_date.strftime('%Y-%m-%d')}")
            return prices[search_date], search_date
    
    # 向后搜索（找之后的交易日）
    for i in range(1, max_days + 1):
        search_date = target_date + pd.Timedelta(days=i)
        if search_date.dayofweek >= 5:
            continue
        if search_date in prices.index:
            print(f"    日期 {target_date.strftime('%Y-%m-%d')} 不是交易日，使用后一个交易日 {search_date.strftime('%Y-%m-%d')}")
            return prices[search_date], search_date
    
    return np.nan, None

def test_date_aligned_momentum():
    """测试日期对齐的相对动量分数计算"""
    print("=" * 60)
    print("测试日期对齐的相对动量分数计算")
    print("=" * 60)
    
    # 1. 下载标普500数据
    print("\n1. 正在下载标普500 (^GSPC) 数据...")
    try:
        sp500_data = yf.download("^GSPC", period="1y", interval="1d", auto_adjust=False, progress=False)
        if sp500_data.empty:
            print("  ❌ 标普500数据为空！")
            return False
        
        if isinstance(sp500_data.columns, pd.MultiIndex):
            sp500_prices = sp500_data['Adj Close']['^GSPC'].dropna()
        else:
            sp500_prices = sp500_data['Adj Close'].dropna()
        
        print(f"  ✅ 标普500数据下载成功，共 {len(sp500_prices)} 个数据点")
        print(f"     数据范围: {sp500_prices.index[0].strftime('%Y-%m-%d')} 到 {sp500_prices.index[-1].strftime('%Y-%m-%d')}")
        
    except Exception as e:
        print(f"  ❌ 下载标普500数据失败: {e}")
        return False
    
    # 2. 计算标普500的参考日期
    print("\n2. 计算标普500的参考日期...")
    sp500_reference_dates = {}
    if len(sp500_prices) >= 63:
        sp500_reference_dates[63] = sp500_prices.index[-63]
        print(f"  3M参考日期: {sp500_reference_dates[63].strftime('%Y-%m-%d')}")
    if len(sp500_prices) >= 126:
        sp500_reference_dates[126] = sp500_prices.index[-126]
        print(f"  6M参考日期: {sp500_reference_dates[126].strftime('%Y-%m-%d')}")
    if len(sp500_prices) >= 252:
        sp500_reference_dates[252] = sp500_prices.index[-252]
        print(f"  12M参考日期: {sp500_reference_dates[252].strftime('%Y-%m-%d')}")
    
    # 3. 测试几个股票的相对动量分数（包括A股）
    test_tickers = ["AAPL", "510300.SS", "^GSPC"]  # 510300.SS是沪深300ETF
    
    print("\n3. 测试股票的相对动量分数计算（日期对齐）...")
    for ticker in test_tickers:
        print(f"\n  正在处理 {ticker}...")
        try:
            # 下载股票数据
            stock_data = yf.download(ticker, period="1y", interval="1d", auto_adjust=False, progress=False)
            if stock_data.empty:
                print(f"    ❌ {ticker} 数据为空！")
                continue
            
            if isinstance(stock_data.columns, pd.MultiIndex):
                price_series = stock_data['Adj Close'][ticker].dropna()
            else:
                price_series = stock_data['Adj Close'].dropna()
            
            print(f"    ✅ {ticker} 数据下载成功，共 {len(price_series)} 个数据点")
            print(f"       数据范围: {price_series.index[0].strftime('%Y-%m-%d')} 到 {price_series.index[-1].strftime('%Y-%m-%d')}")
            
            if len(price_series) < 22:
                print(f"    ⚠️  {ticker} 数据不足22天，跳过计算")
                continue
            
            # 计算相对动量分数（日期对齐）
            m3m = np.nan
            m6m = np.nan
            m12m = np.nan
            
            # 计算3个月（63个交易日）的收益率
            if 63 in sp500_reference_dates:
                ref_date_3m = sp500_reference_dates[63]
                print(f"    参考日期 3M: {ref_date_3m.strftime('%Y-%m-%d')}")
                
                # 获取标普500在参考日期的价格
                sp500_price_3m, actual_date_3m = get_price_on_date(sp500_prices, ref_date_3m)
                
                # 获取股票在相同时日期（或最近交易日）的价格
                if actual_date_3m is not None:
                    stock_price_3m, _ = get_price_on_date(price_series, actual_date_3m)
                else:
                    stock_price_3m, _ = get_price_on_date(price_series, ref_date_3m)
                
                if not pd.isna(sp500_price_3m) and not pd.isna(stock_price_3m):
                    sp500_return_3m = (sp500_prices.iloc[-1] / sp500_price_3m - 1) * 100
                    stock_return_3m = (price_series.iloc[-1] / stock_price_3m - 1) * 100
                    m3m = stock_return_3m - sp500_return_3m
                    print(f"    M3M: 股票收益率={stock_return_3m:.2f}%, 标普500收益率={sp500_return_3m:.2f}%, 相对差={m3m:.2f}%")
                else:
                    print(f"    ⚠️  M3M: 数据不足（sp500_price={sp500_price_3m}, stock_price={stock_price_3m})")
            
            # 计算6个月（126个交易日）的收益率
            if 126 in sp500_reference_dates:
                ref_date_6m = sp500_reference_dates[126]
                print(f"    参考日期 6M: {ref_date_6m.strftime('%Y-%m-%d')}")
                
                sp500_price_6m, actual_date_6m = get_price_on_date(sp500_prices, ref_date_6m)
                
                if actual_date_6m is not None:
                    stock_price_6m, _ = get_price_on_date(price_series, actual_date_6m)
                else:
                    stock_price_6m, _ = get_price_on_date(price_series, ref_date_6m)
                
                if not pd.isna(sp500_price_6m) and not pd.isna(stock_price_6m):
                    sp500_return_6m = (sp500_prices.iloc[-1] / sp500_price_6m - 1) * 100
                    stock_return_6m = (price_series.iloc[-1] / stock_price_6m - 1) * 100
                    m6m = stock_return_6m - sp500_return_6m
                    print(f"    M6M: 股票收益率={stock_return_6m:.2f}%, 标普500收益率={sp500_return_6m:.2f}%, 相对差={m6m:.2f}%")
            
            # 计算12个月（252个交易日）的收益率
            if 252 in sp500_reference_dates:
                ref_date_12m = sp500_reference_dates[252]
                print(f"    参考日期 12M: {ref_date_12m.strftime('%Y-%m-%d')}")
                
                sp500_price_12m, actual_date_12m = get_price_on_date(sp500_prices, ref_date_12m)
                
                if actual_date_12m is not None:
                    stock_price_12m, _ = get_price_on_date(price_series, actual_date_12m)
                else:
                    stock_price_12m, _ = get_price_on_date(price_series, ref_date_12m)
                
                if not pd.isna(sp500_price_12m) and not pd.isna(stock_price_12m):
                    sp500_return_12m = (sp500_prices.iloc[-1] / sp500_price_12m - 1) * 100
                    stock_return_12m = (price_series.iloc[-1] / stock_price_12m - 1) * 100
                    m12m = stock_return_12m - sp500_return_12m
                    print(f"    M12M: 股票收益率={stock_return_12m:.2f}%, 标普500收益率={sp500_return_12m:.2f}%, 相对差={m12m:.2f}%")
            
            # 计算相对动量分数
            if not (pd.isna(m3m) or pd.isna(m6m) or pd.isna(m12m)):
                relative_momentum = 0.2 * m3m + 0.3 * m6m + 0.5 * m12m
                print(f"    ✅ 相对动量分数: {relative_momentum:.2f}")
            elif ticker == "^GSPC":
                relative_momentum = 0.0
                print(f"    ✅ {ticker} 是标普500，相对动量分数设为 0.0")
            else:
                print(f"    ❌ 无法计算 {ticker} 的相对动量分数（数据不足）")
            
        except Exception as e:
            print(f"    ❌ 处理 {ticker} 时出错: {e}")
            import traceback
            traceback.print_exc()
    
    print("\n" + "=" * 60)
    print("测试完成！")
    print("=" * 60)
    return True

if __name__ == "__main__":
    test_date_aligned_momentum()

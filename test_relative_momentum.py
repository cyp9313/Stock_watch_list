#!/usr/bin/env python3
"""
测试相对动量分数计算
"""
import yfinance as yf
import pandas as pd
import numpy as np
import sys
import os

# 添加后端代码路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def calculate_return(prices, days):
    """计算收益率"""
    if len(prices) < days:
        return np.nan
    return (prices.iloc[-1] / prices.iloc[-days] - 1) * 100 if pd.notna(prices.iloc[-days]) and prices.iloc[-days] != 0 else np.nan

def test_relative_momentum():
    """测试相对动量分数计算"""
    print("=" * 60)
    print("测试相对动量分数计算")
    print("=" * 60)
    
    # 1. 下载标普500数据
    print("\n1. 正在下载标普500 (^GSPC) 数据...")
    try:
        sp500_data = yf.download("^GSPC", period="1y", interval="1d", auto_adjust=False, progress=False)
        if sp500_data.empty:
            print("  ❌ 标普500数据为空！")
            return False
        
        if isinstance(sp500_data.columns, pd.MultiIndex):
            if 'Adj Close' in sp500_data.columns:
                sp500_prices = sp500_data['Adj Close']['^GSPC'].dropna()
            else:
                sp500_prices = sp500_data['Close']['^GSPC'].dropna()
        else:
            if 'Adj Close' in sp500_data.columns:
                sp500_prices = sp500_data['Adj Close'].dropna()
            else:
                sp500_prices = sp500_data['Close'].dropna()
        
        print(f"  ✅ 标普500数据下载成功，共 {len(sp500_prices)} 个数据点")
        print(f"     数据范围: {sp500_prices.index[0].strftime('%Y-%m-%d')} 到 {sp500_prices.index[-1].strftime('%Y-%m-%d')}")
        
    except Exception as e:
        print(f"  ❌ 下载标普500数据失败: {e}")
        return False
    
    # 2. 测试几个股票的相对动量分数
    test_tickers = ["AAPL", "MSFT", "GOOGL", "^GSPC"]
    
    print("\n2. 测试股票的相对动量分数计算...")
    for ticker in test_tickers:
        print(f"\n  正在处理 {ticker}...")
        try:
            # 下载股票数据
            stock_data = yf.download(ticker, period="1y", interval="1d", auto_adjust=False, progress=False)
            if stock_data.empty:
                print(f"    ❌ {ticker} 数据为空！")
                continue
            
            if isinstance(stock_data.columns, pd.MultiIndex):
                if 'Adj Close' in stock_data.columns:
                    price_series = stock_data['Adj Close'][ticker].dropna()
                else:
                    price_series = stock_data['Close'][ticker].dropna()
            else:
                if 'Adj Close' in stock_data.columns:
                    price_series = stock_data['Adj Close'].dropna()
                else:
                    price_series = stock_data['Close'].dropna()
            
            print(f"    ✅ {ticker} 数据下载成功，共 {len(price_series)} 个数据点")
            
            if len(price_series) < 22:
                print(f"    ⚠️  {ticker} 数据不足22天，跳过计算")
                continue
            
            # 计算收益率
            sp500_return_3m = calculate_return(sp500_prices, 63) if len(sp500_prices) >= 63 else np.nan
            sp500_return_6m = calculate_return(sp500_prices, 126) if len(sp500_prices) >= 126 else np.nan
            sp500_return_12m = calculate_return(sp500_prices, 252) if len(sp500_prices) >= 252 else np.nan
            
            stock_return_3m = calculate_return(price_series, 63) if len(price_series) >= 63 else np.nan
            stock_return_6m = calculate_return(price_series, 126) if len(price_series) >= 126 else np.nan
            stock_return_12m = calculate_return(price_series, 252) if len(price_series) >= 252 else np.nan
            
            print(f"    标普500收益率: 3M={sp500_return_3m:.2f}%, 6M={sp500_return_6m:.2f}%, 12M={sp500_return_12m:.2f}%" if not pd.isna(sp500_return_3m) else "    标普500收益率: 数据不足")
            print(f"    {ticker} 收益率: 3M={stock_return_3m:.2f}%, 6M={stock_return_6m:.2f}%, 12M={stock_return_12m:.2f}%" if not pd.isna(stock_return_3m) else f"    {ticker} 收益率: 数据不足")
            
            # 计算相对收益率差
            m3m = stock_return_3m - sp500_return_3m if not (pd.isna(stock_return_3m) or pd.isna(sp500_return_3m)) else np.nan
            m6m = stock_return_6m - sp500_return_6m if not (pd.isna(stock_return_6m) or pd.isna(sp500_return_6m)) else np.nan
            m12m = stock_return_12m - sp500_return_12m if not (pd.isna(stock_return_12m) or pd.isna(sp500_return_12m)) else np.nan
            
            print(f"    相对收益率差: M3M={m3m:.2f}%, M6M={m6m:.2f}%, M12M={m12m:.2f}%" if not pd.isna(m3m) else "    相对收益率差: 计算失败")
            
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
    test_relative_momentum()

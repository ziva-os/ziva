#!/usr/bin/env python3
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta

# 获取TSLA数据
ticker = yf.Ticker('TSLA')

# 获取近一个月数据（约30天）
end_date = datetime.now()
start_date = end_date - timedelta(days=35)

hist = ticker.history(start=start_date, end=end_date)
print('=== TSLA 近一个月股价数据 ===')
print(f'数据范围: {hist.index[0].strftime("%Y-%m-%d")} 至 {hist.index[-1].strftime("%Y-%m-%d")}')
print()
print(hist.to_string())
print()
print('=== 关键统计数据 ===')
print(f'最新收盘价: ${hist["Close"].iloc[-1]:.2f}')
print(f'一个月前收盘价: ${hist["Close"].iloc[0]:.2f}')
print(f'月内最高价: ${hist["High"].max():.2f}')
print(f'月内最低价: ${hist["Low"].min():.2f}')
print(f'月涨跌幅: {((hist["Close"].iloc[-1] / hist["Close"].iloc[0]) - 1) * 100:.2f}%')
print(f'平均成交量: {hist["Volume"].mean():,.0f}')

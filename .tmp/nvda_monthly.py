import yfinance as yf
import json
from datetime import datetime, timedelta

ticker = yf.Ticker("NVDA")
end_date = datetime(2026, 6, 6)
start_date = end_date - timedelta(days=31)

# Get daily data for the past month
hist = ticker.history(start=start_date, end=end_date, interval="1d")
print("=== NVDA 近一个月日线数据 ===")
print(f"时间范围: {hist.index[0].strftime('%Y-%m-%d')} 至 {hist.index[-1].strftime('%Y-%m-%d')}")
print(f"交易日数: {len(hist)}\n")

# Show key daily data
print(f"{'日期':<12}{'开盘':>10}{'最高':>10}{'最低':>10}{'收盘':>10}{'成交量':>16}{'涨跌幅':>10}")
print("-" * 80)
prev_close = None
for date, row in hist.iterrows():
    close = float(row['Close'])
    if prev_close is not None:
        chg = (close - prev_close) / prev_close * 100
        chg_str = f"{chg:+.2f}%"
    else:
        chg_str = "—"
    print(f"{date.strftime('%Y-%m-%d'):<12}{float(row['Open']):>10.2f}{float(row['High']):>10.2f}{float(row['Low']):>10.2f}{close:>10.2f}{int(row['Volume']):>16,}{chg_str:>10}")
    prev_close = close

print("\n=== 关键统计 ===")
start_price = float(hist['Close'].iloc[0])
end_price = float(hist['Close'].iloc[-1])
month_high = float(hist['High'].max())
month_low = float(hist['Low'].min())
month_change = (end_price - start_price) / start_price * 100
avg_volume = int(hist['Volume'].mean())

print(f"月初收盘价: ${start_price:.2f}")
print(f"月末收盘价: ${end_price:.2f}")
print(f"月内最高价: ${month_high:.2f}")
print(f"月内最低价: ${month_low:.2f}")
print(f"月内涨跌幅: {month_change:+.2f}%")
print(f"平均日成交量: {avg_volume:,}")
print(f"价格区间幅度: {((month_high - month_low) / month_low * 100):.2f}%")

# Volatility (daily returns std)
import statistics
returns = hist['Close'].pct_change().dropna().tolist()
vol_daily = statistics.stdev(returns) * 100
vol_annual = vol_daily * (252 ** 0.5)
print(f"日波动率: {vol_daily:.2f}%")
print(f"年化波动率: {vol_annual:.2f}%")

# 52-week info
info = ticker.info
print(f"\n=== 基本面快照 ===")
print(f"52周最高: ${info.get('fiftyTwoWeekHigh', 'N/A')}")
print(f"52周最低: ${info.get('fiftyTwoWeekLow', 'N/A')}")
print(f"市值: ${info.get('marketCap', 0)/1e12:.2f}T")
print(f"市盈率(P/E): {info.get('trailingPE', 'N/A')}")
print(f"前收盘价: ${info.get('previousClose', 'N/A')}")
print(f"当前价 vs 52周高点: {(end_price/info.get('fiftyTwoWeekHigh', 1)-1)*100:+.2f}%")

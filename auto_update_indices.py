import json
import re
import requests
import numpy as np
import pandas as pd
from scipy import stats
from datetime import datetime, timedelta
import os

# 配置指数代码映射
INDEX_CONFIG = [
    {"code": "H00922", "name": "中证红利全收益", "source": "CSI"},
    {"code": "932305CNY010", "name": "智选高股息全收益", "source": "CSI"},
    {"code": "H20269", "name": "红利低波全收益", "source": "CSI"},
    {"code": "H20955", "name": "红利低波100全收益", "source": "CSI"},
    {"code": "932422CNY010", "name": "A500红利低波全收益", "source": "CSI"},
    {"code": "480081", "name": "价值100全收益", "source": "CNI"}
]

def fetch_csi_history(code):
    url = f"https://www.csindex.com.cn/csindex-home/perf/index-perf?indexCode={code}"
    try:
        resp = requests.get(url, timeout=20).json()
        if resp.get("code") == "200" and resp.get("data"):
            return {item["tradeDate"][:10]: float(item["close"]) for item in resp["data"]}
    except Exception as e:
        print(f"抓取中证指数 {code} 失败: {e}")
    return {}

def fetch_cni_history(code):
    url = "http://hq.cnindex.com.cn/market/market/getIndexDailyDataWithDataFormat"
    end_str = datetime.now().strftime("%Y-%m-%d")
    start_str = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
    params = {"indexCode": code, "startDate": start_str, "endDate": end_str, "frequency": "day"}
    try:
        r = requests.get(url, params=params, timeout=15)
        data = r.json()
        if data.get("data") and data["data"].get("data"):
            return {row[0]: float(row[5]) for row in data["data"]["data"]}
    except Exception as e:
        print(f"获取国证指数 {code} 历史行情失败: {e}")
    return {}

def calculate_rolling_quantiles(drawdowns, window_years, quantile_levels):
    window_size = int(252 * window_years)
    s = pd.Series(drawdowns)
    results = {}
    for q in quantile_levels:
        rolling_q = s.rolling(window=window_size, min_periods=1).quantile(q/100.0)
        results[q] = [round(float(v), 2) for v in rolling_q]
    return results

def find_points(prices, is_high=True, window=120):
    points = []
    n = len(prices)
    for i in range(0, n, window):
        segment = prices[i:i+window]
        if not segment: continue
        val = max(segment) if is_high else min(segment)
        idx = i + segment.index(val)
        points.append((idx, np.log(val)))
    return points

def update_index_calculations(data_obj):
    values = data_obj["values"]
    prices = np.array(values)
    
    # 1. 计算回撤
    max_prices = np.maximum.accumulate(prices)
    drawdowns = (prices / max_prices - 1) * 100
    data_obj["drawdowns"] = [round(float(d), 2) for d in drawdowns]
    data_obj["max_drawdown"] = round(float(np.min(drawdowns)), 2)
    
    # 2. 计算滚动分位线
    q5 = calculate_rolling_quantiles(drawdowns, 5, [50, 70, 90])
    q10 = calculate_rolling_quantiles(drawdowns, 10, [50, 70])
    
    data_obj["quantile_lines"] = {
        "滚动五年50%分位": q5[50],
        "滚动五年70%分位": q5[70],
        "滚动五年90%分位": q5[90],
        "滚动十年50%分位": q10[50],
        "滚动十年70%分位": q10[70]
    }
    
    # 3. 对数回归通道 (近10年)
    n_total = len(prices)
    n_10y = min(n_total, 252 * 10)
    start_idx = n_total - n_10y
    
    prices_10y = values[start_idx:]
    high_pts = find_points(prices_10y, is_high=True)
    low_pts = find_points(prices_10y, is_high=False)
    high_pts = [(p[0] + start_idx, p[1]) for p in high_pts]
    low_pts = [(p[0] + start_idx, p[1]) for p in low_pts]
    
    def get_line(pts, length):
        x = np.array([p[0] for p in pts])
        y = np.array([p[1] for p in pts])
        slope, intercept, _, _, _ = stats.linregress(x, y)
        line = slope * np.arange(length) + intercept
        annual_ret = (np.exp(slope * 252) - 1) * 100
        return line.tolist(), round(float(annual_ret), 2)

    top_line, high_ret = get_line(high_pts, n_total)
    bottom_line, low_ret = get_line(low_pts, n_total)
    
    x_base = np.arange(n_10y)
    y_base = np.log(prices_10y)
    slope_b, intercept_b, _, _, _ = stats.linregress(x_base, y_base)
    intercept_global = intercept_b - slope_b * start_idx
    regression_line = slope_b * np.arange(n_total) + intercept_global
    
    data_obj["regression"] = [round(float(v), 4) for v in regression_line]
    data_obj["top_line"] = [round(float(v), 4) for v in top_line]
    data_obj["bottom_line"] = [round(float(v), 4) for v in bottom_line]
    data_obj["high_points"] = [[int(p[0]), round(float(p[1]), 4)] for p in high_pts]
    data_obj["low_points"] = [[int(p[0]), round(float(p[1]), 4)] for p in low_pts]
    data_obj["annual_return_high"] = high_ret
    data_obj["annual_return_low"] = low_ret

def update_html(file_path):
    print(f"正在处理文件: {file_path}")
    if not os.path.exists(file_path): return

    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    match = re.search(r'let allData = (\{.*?\});', content, re.DOTALL)
    if not match: return
    
    all_data = json.loads(match.group(1))
    updated_any = False

    for item in INDEX_CONFIG:
        code, name, source = item["code"], item["name"], item["source"]
        if name not in all_data: continue
            
        recent_data = fetch_csi_history(code) if source == "CSI" else fetch_cni_history(code)
        
        dates, values = all_data[name]["dates"], all_data[name]["values"]
        index_updated = False
        
        if recent_data:
            for d_str, price in sorted(recent_data.items(), key=lambda x: x[0]):
                if d_str in dates:
                    idx = dates.index(d_str)
                    if abs(values[idx] - price) > 0.001:
                        values[idx] = price
                        index_updated = True
                else:
                    dates.append(d_str)
                    values.append(price)
                    index_updated = True
        
        # 强制检查：如果派生数据长度与日期长度不符，也触发重新计算
        q_lines = all_data[name].get("quantile_lines", {})
        q_len = len(next(iter(q_lines.values()))) if q_lines else 0
        if index_updated or q_len != len(dates) or len(all_data[name].get("top_line", [])) != len(dates):
            combined = sorted(zip(dates, values), key=lambda x: x[0])
            all_data[name]["dates"] = [x[0] for x in combined]
            all_data[name]["values"] = [x[1] for x in combined]
            
            print(f"正在为 {name} 重新计算分位线和回归通道...")
            update_index_calculations(all_data[name])
            updated_any = True

    if updated_any:
        new_json = json.dumps(all_data, ensure_ascii=False)
        new_content = content.replace(match.group(1), new_json)
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(new_content)
        print("🎉 所有指数及图表指标已成功同步更新。")
    else:
        print("ℹ️ 所有数据及指标已是最新。")

if __name__ == "__main__":
    update_html("指数全收益定投策略.html")

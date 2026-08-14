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

def fetch_csi_history(code, last_saved_date):
    """从中证官网新版历史接口获取最近交易日点位。

    该接口必须带 startDate 与 endDate；缺少这两个参数时会返回
    ``Parameter Errors`` 或空数据。仅拉取已保存日期前 14 个自然日至今，
    既能补齐节假日、延迟披露和近期修订，又不会用远端数据覆盖网页历史序列。
    """
    try:
        last_dt = datetime.strptime(last_saved_date, "%Y-%m-%d")
    except (TypeError, ValueError):
        last_dt = datetime.now() - timedelta(days=30)

    start_date = (last_dt - timedelta(days=14)).strftime("%Y%m%d")
    end_date = datetime.now().strftime("%Y%m%d")
    url = "https://www.csindex.com.cn/csindex-home/perf/index-perf"
    params = {"indexCode": code, "startDate": start_date, "endDate": end_date}
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.csindex.com.cn/"
    }

    try:
        response = requests.get(url, params=params, headers=headers, timeout=30)
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("data") or []
        if payload.get("success") is True and rows:
            result = {}
            for item in rows:
                trade_date = str(item.get("tradeDate", ""))
                close = item.get("close")
                if len(trade_date) == 8 and close is not None:
                    normalized_date = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}"
                    result[normalized_date] = float(close)
            if result:
                latest_date = max(result)
                print(f"中证 {code}：获取 {len(result)} 条数据，最新 {latest_date} / {result[latest_date]:.2f}")
                return result
        print(f"中证 {code}：接口未返回有效数据（code={payload.get('code')}，msg={payload.get('msg')}）。")
    except (requests.RequestException, ValueError, TypeError) as e:
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
    """
    对齐 Excel 的滚动分位线计算:
    1. 窗口大小: 5年=1218天, 10年=2431天 (参考 Excel 选区)
    2. 分位逻辑: Excel 的 70%分位线使用 PERCENTILE.INC(..., 0.3), 即 (100-q)/100
    3. 显示逻辑: 前面不足窗口期的数据设为 null (不显示)
    """
    window_size = 1218 if window_years == 5 else 2431
    s = pd.Series(drawdowns)
    results = {}
    for q in quantile_levels:
        p = (100 - q) / 100.0
        # 使用 pandas 的 rolling().quantile，默认插值方式为 linear，等同于 Excel 的 PERCENTILE.INC
        # min_periods=window_size 确保前期数据不足时不显示
        rolling_q = s.rolling(window=window_size, min_periods=window_size).quantile(p)
        # 将 NaN 转换为 None，以便 JSON 序列化为 null
        results[q] = [round(float(v), 2) if not np.isnan(v) else None for v in rolling_q]
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
    n_total = len(prices)
    
    # 1. 计算回撤
    max_prices = np.maximum.accumulate(prices)
    drawdowns = (prices / max_prices - 1) * 100
    data_obj["drawdowns"] = [round(float(d), 2) for d in drawdowns]
    data_obj["max_drawdown"] = round(float(np.min(drawdowns)), 2)
    
    # 2. 计算滚动分位线 (使用对齐 Excel 的新逻辑)
    q5 = calculate_rolling_quantiles(drawdowns, 5, [50, 70, 90])
    q10 = calculate_rolling_quantiles(drawdowns, 10, [50, 70])
    
    # 修正 HTML 字段名映射: 网页 JS 查找的是 'percentiles'
    data_obj["percentiles"] = {
        "滚动五年50%分位": q5[50],
        "滚动五年70%分位": q5[70],
        "滚动五年90%分位": q5[90],
        "滚动十年50%分位": q10[50],
        "滚动十年70%分位": q10[70]
    }
    
    # 3. 对数回归通道 (近10年)
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

    reg_high, high_ret = get_line(high_pts, n_total)
    reg_low, low_ret = get_line(low_pts, n_total)
    
    x_base = np.arange(n_10y)
    y_base = np.log(prices_10y)
    slope_b, intercept_b, _, _, _ = stats.linregress(x_base, y_base)
    intercept_global = intercept_b - slope_b * start_idx
    regression_line = slope_b * np.arange(n_total) + intercept_global
    
    data_obj["regression"] = [round(float(v), 4) for v in regression_line]
    data_obj["reg_high"] = [round(float(v), 4) for v in reg_high]
    data_obj["reg_low"] = [round(float(v), 4) for v in reg_low]
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
            
        dates, values = all_data[name]["dates"], all_data[name]["values"]
        last_saved_date = max(dates) if dates else None
        recent_data = (
            fetch_csi_history(code, last_saved_date)
            if source == "CSI"
            else fetch_cni_history(code)
        )
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
        
        # 强制检查：如果派生数据长度与日期长度不符，或者字段缺失，触发重新计算
        p_lines = all_data[name].get("percentiles", {})
        p_len = len(next(iter(p_lines.values()))) if p_lines else 0
        
        if index_updated or p_len != len(dates) or len(all_data[name].get("reg_high", [])) != len(dates):
            combined = sorted(zip(dates, values), key=lambda x: x[0])
            all_data[name]["dates"] = [x[0] for x in combined]
            all_data[name]["values"] = [x[1] for x in combined]
            
            print(f"正在为 {name} 重新计算所有图表指标...")
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

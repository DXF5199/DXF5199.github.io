import json
import re
import requests
import akshare as ak
import pandas as pd
from datetime import datetime, timedelta

# 配置指数代码映射 (对标 Wind 终端全收益点位)
INDEX_CONFIG = [
    {"code": "H00922", "name": "中证红利全收益", "source": "CSI"},
    {"code": "932305CNY010", "name": "智选高股息全收益", "source": "CSI"},
    {"code": "H20269", "name": "红利低波全收益", "source": "CSI"},
    {"code": "H20955", "name": "红利低波100全收益", "source": "CSI"},
    {"code": "932422CNY010", "name": "A500红利低波全收益", "source": "CSI"},
    {"code": "480081", "name": "价值100全收益", "source": "AK"}
]

def fetch_csi_data(code, date_str):
    """从自中证指数官网获取数据"""
    url = f"https://www.csindex.com.cn/csindex-home/perf/index-perf?indexCode={code}&startDate={date_str}&endDate={date_str}"
    try:
        resp = requests.get(url, timeout=20).json()
        if resp.get("code") == "200" and resp.get("data"):
            return float(resp["data"][0]["close"])
    except Exception as e:
        print(f"抓取中证指数 {code} 失败: {e}")
    return None

def fetch_ak_data(code, date_str):
    """使用 akshare 获取数据 (针对 480081)"""
    try:
        # akshare 的 index_zh_a_hist 接口通常比较稳定
        df = ak.index_zh_a_hist(symbol=code, period="daily", start_date=date_str, end_date=date_str)
        if not df.empty:
            return float(df.iloc[-1]["收盘"])
    except Exception as e:
        print(f"抓取 AK 指数 {code} 失败: {e}")
    return None

def update_html(file_path):
    # 获取最近 3 天的日期，确保能抓取到最新交易日
    today = datetime.now()
    dates_to_check = [(today - timedelta(days=i)).strftime("%Y%m%d") for i in range(3)]
    
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    # 兼容多种引号和空格
    match = re.search(r'let allData = (\{.*?\});', content, re.DOTALL)
    if not match:
        print("未找到 allData 数据标识")
        return
    
    all_data = json.loads(match.group(1))
    updated_any = False

    for item in INDEX_CONFIG:
        code = item["code"]
        name = item["name"]
        source = item["source"]
        
        if name not in all_data:
            print(f"警告：HTML 数据中未找到指数名称【{name}】")
            continue
            
        latest_val = None
        target_date = ""
        
        # 依次检查最近日期
        for d_str in dates_to_check:
            if source == "CSI":
                val = fetch_csi_data(code, d_str)
            else:
                val = fetch_ak_data(code, d_str)
            
            if val:
                latest_val = val
                target_date = f"{d_str[:4]}-{d_str[4:6]}-{d_str[6:]}"
                break
        
        if latest_val:
            dates = all_data[name]["dates"]
            values = all_data[name]["values"]
            
            if target_date in dates:
                idx = dates.index(target_date)
                values[idx] = latest_val
            else:
                dates.append(target_date)
                values.append(latest_val)
                # 排序
                combined = sorted(zip(dates, values), key=lambda x: x[0])
                all_data[name]["dates"] = [x[0] for x in combined]
                all_data[name]["values"] = [x[1] for x in combined]
            
            # 重新计算回撤
            max_val = 0
            dds = []
            min_dd = 0
            for v in all_data[name]["values"]:
                if v > max_val: max_val = v
                dd = (v / max_val - 1) * 100
                dds.append(round(dd, 2))
                if dd < min_dd: min_dd = dd
            all_data[name]["drawdowns"] = dds
            all_data[name]["max_drawdown"] = round(min_dd, 2)
            
            updated_any = True
            print(f"成功更新 {name}: {target_date} -> {latest_val}")

    if updated_any:
        new_json = json.dumps(all_data, ensure_ascii=False)
        # 精准替换
        new_content = content.replace(match.group(1), new_json)
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(new_content)
        print("HTML 文件已成功更新并保存。")
    else:
        print("未检测到新数据，文件未修改。")

if __name__ == "__main__":
    # 自动识别文件名（支持用户上传的文件名）
    import os
    target_file = "指数全收益定投策略.html"
    if os.path.exists(target_file):
        update_html(target_file)
    else:
        print(f"错误：未找到目标文件 {target_file}")

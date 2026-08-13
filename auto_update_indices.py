import json
import re
import requests
from datetime import datetime, timedelta
import os

# 配置指数代码映射 (对标 Wind 终端全收益点位)
INDEX_CONFIG = [
    {"code": "H00922", "name": "中证红利全收益", "source": "CSI"},
    {"code": "932305CNY010", "name": "智选高股息全收益", "source": "CSI"},
    {"code": "H20269", "name": "红利低波全收益", "source": "CSI"},
    {"code": "H20955", "name": "红利低波100全收益", "source": "CSI"},
    {"code": "932422CNY010", "name": "A500红利低波全收益", "source": "CSI"},
    {"code": "480081", "name": "价值100全收益", "source": "CNI"}  # 切换到国证核心接口
]

def fetch_csi_history(code):
    """从中证指数官网获取最近 30 天的历史行情数据"""
    url = f"https://www.csindex.com.cn/csindex-home/perf/index-perf?indexCode={code}"
    try:
        resp = requests.get(url, timeout=20).json()
        if resp.get("code") == "200" and resp.get("data"):
            result = {}
            for item in resp["data"]:
                date_str = item["tradeDate"][:10] # yyyy-MM-dd
                result[date_str] = float(item["close"])
            return result
    except Exception as e:
        print(f"抓取中证指数 {code} 失败: {e}")
    return {}

def fetch_cni_history(code):
    """从国证指数官方内部 API 获取最近的历史行情数据"""
    url = "http://hq.cnindex.com.cn/market/market/getIndexDailyDataWithDataFormat"
    end_str = datetime.now().strftime("%Y-%m-%d")
    start_str = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    
    params = {
        "indexCode": code,
        "startDate": start_str,
        "endDate": end_str,
        "frequency": "day",
    }
    
    try:
        r = requests.get(url, params=params, timeout=15)
        data = r.json()
        if data.get("data") and data["data"].get("data"):
            rows = data["data"]["data"]
            result = {}
            for row in rows:
                date_str = row[0]
                close_price = float(row[5])
                result[date_str] = close_price
            return result
    except Exception as e:
        print(f"获取国证指数 {code} 历史行情失败: {e}")
    return {}

def update_html(file_path):
    print(f"正在处理文件: {file_path}")
    if not os.path.exists(file_path):
        print(f"错误：找不到文件 {file_path}")
        return

    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    match = re.search(r'let allData = (\{.*?\});', content, re.DOTALL)
    if not match:
        print("错误：在 HTML 中未找到 allData 数据标识")
        return
    
    try:
        all_data = json.loads(match.group(1))
    except Exception as e:
        print(f"解析 allData JSON 失败: {e}")
        return

    updated_any = False

    for item in INDEX_CONFIG:
        code = item["code"]
        name = item["name"]
        source = item["source"]
        
        if name not in all_data:
            print(f"警告：HTML 数据中未找到指数名称【{name}】")
            continue
            
        if source == "CSI":
            recent_data = fetch_csi_history(code)
        else:
            recent_data = fetch_cni_history(code)
            
        if not recent_data:
            print(f"未获取到 {name} ({code}) 的最新数据")
            continue
            
        dates = all_data[name]["dates"]
        values = all_data[name]["values"]
        
        index_updated = False
        for date_str, price in sorted(recent_data.items(), key=lambda x: x[0]):
            if date_str in dates:
                idx = dates.index(date_str)
                if abs(values[idx] - price) > 0.001:
                    values[idx] = price
                    index_updated = True
                    print(f"更新已有数据 -> {name} [{date_str}]: {price}")
            else:
                dates.append(date_str)
                values.append(price)
                index_updated = True
                print(f"新增最新数据 -> {name} [{date_str}]: {price}")
        
        if index_updated:
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
            print(f"✅ {name} 数据处理完成。")

    if updated_any:
        new_json = json.dumps(all_data, ensure_ascii=False)
        new_content = content.replace(match.group(1), new_json)
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(new_content)
        print("🎉 HTML 文件已成功更新并保存。")
    else:
        print("ℹ️ 所有指数数据已是最新，无需更新。")

if __name__ == "__main__":
    target_file = "指数全收益定投策略.html"
    update_html(target_file)

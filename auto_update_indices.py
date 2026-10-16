import json
import re
import requests
import numpy as np
import pandas as pd
from scipy import stats
from bisect import bisect_left
from datetime import datetime, timedelta
import time
import os
import tempfile

# 配置指数代码映射
INDEX_CONFIG = [
    {"code": "H00922", "name": "中证红利全收益", "source": "CSI"},
    {"code": "932305CNY010", "name": "智选高股息全收益", "source": "CSI"},
    {"code": "H20269", "name": "红利低波全收益", "source": "CSI"},
    {"code": "H20955", "name": "红利低波100全收益", "source": "CSI"},
    {"code": "932422CNY010", "name": "A500红利低波全收益", "source": "CSI"},
    {"code": "480081", "name": "价值100全收益", "source": "CNI"},
    {"code": "480080", "name": "成长100全收益", "source": "CNI"}
]

# 指数 -> 场内 ETF 映射（用于把回撤分位线换算成可交易的价格）
# 口径说明：指数为全收益口径，ETF 为价格口径且会现金分红除息，二者长期存在漂移。
# 这里统一以“最新一期”的比价为基准做等比换算，用于给出当下可参考的价格位置。
ETF_CONFIG = {
    "中证红利全收益":     {"code": "515080", "market": "sh", "name": "中证红利ETF招商"},
    "智选高股息全收益":   {"code": "159207", "market": "sz", "name": "高股息ETF广发"},
    "红利低波全收益":     {"code": "563020", "market": "sh", "name": "红利低波ETF易方达"},
    "红利低波100全收益":  {"code": "159307", "market": "sz", "name": "红利低波100ETF博时"},
    "A500红利低波全收益": {"code": "563510", "market": "sh", "name": "A500红利低波ETF易方达"},
    "价值100全收益":      {"code": "159263", "market": "sz", "name": "价值ETF易方达"},
}

# 指数估值（成分股权重加权）
# 值为 (价格指数代码, 数据来源)：CSI=中证指数官网、CNI=国证指数官网。
# 权重来源都是指数公司公开文件（中证为月度、国证为每日），个股行情每日实时，
# 因此估值数据每个交易日都会刷新。
VALUATION_CONFIG = {
    "中证红利全收益": ("000922", "CSI"),
    "智选高股息全收益": ("932305", "CSI"),
    "红利低波全收益": ("H30269", "CSI"),
    "红利低波100全收益": ("930955", "CSI"),
    "A500红利低波全收益": ("932422", "CSI"),
    "价值100全收益": ("480081", "CNI"),
    "成长100全收益": ("480080", "CNI"),
}

CSI_WEIGHT_URL = (
    "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/"
    "file/autofile/closeweight/{code}closeweight.xls"
)
TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q={symbols}"
TENCENT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://gu.qq.com/",
}
# 腾讯行情字段下标
TENCENT_FIELD_PE = 39    # 市盈率(TTM)
TENCENT_FIELD_PB = 46    # 市净率
TENCENT_FIELD_DY = 64    # 股息率(%)
TENCENT_FIELD_TIME = 30  # 行情时间戳 YYYYMMDDHHMMSS
TENCENT_FIELD_MARKET_CAP = 45  # 总市值(亿元)

# 国证指数官网：样本权重下载接口（完整成分股权重，每日更新）
CNI_WEIGHT_URL = (
    "https://www.cnindex.com.cn/sample-detail/download-history?indexcode={code}"
)
CNI_HEADERS = {
    "User-Agent": TENCENT_HEADERS["User-Agent"],
    "Referer": "https://www.cnindex.com.cn/module/index-detail.html",
}

# 中证指数官网：行业权重分布接口（cicsType = 1/2/3/4 对应一/二/三/四级行业）
CSI_INDUSTRY_URL = (
    "https://www.csindex.com.cn/csindex-home/index/weight/"
    "industry-weight-two-new/{code}?cicsType={level}"
)
# 中证一级行业权重（部分新指数只发布到一级）
CSI_INDUSTRY_L1_URL = (
    "https://www.csindex.com.cn/csindex-home/index/weight/industry-weight/{code}"
)
CSI_HEADERS = {
    "User-Agent": TENCENT_HEADERS["User-Agent"],
    "Referer": "https://www.csindex.com.cn/",
}
# 行业分类层级：2 = 中证二级行业
INDUSTRY_LEVEL = 2
INDUSTRY_LEVEL_NAME = {1: "一", 2: "二", 3: "三", 4: "四"}

# 申万行业分布（红色火箭）：成分股接口一次返回全部成分股的
# 权重 + 申万一/二/三/四级行业名，按权重聚合成行业分布。
# 注意国证指数用 98 前缀（价值100 = 980081、成长100 = 980080），
# 与国证官网的 48 前缀不同。
REDROCKET_COMPONENT_URL = (
    "https://www.hongsehuojian.com/fundex-quote/security/component/"
    "componentStock?securityCode={code}&isAll=1"
)
REDROCKET_CODES = {
    "中证红利全收益": "000922.CSI",
    "智选高股息全收益": "932305.CSI",
    "红利低波全收益": "h30269.CSI",
    "红利低波100全收益": "930955.CSI",
    "A500红利低波全收益": "932422.CSI",
    "价值100全收益": "980081.CNI",
    "成长100全收益": "980080.CNI",
}
REDROCKET_HEADERS = {
    "User-Agent": TENCENT_HEADERS["User-Agent"],
    "Referer": "https://www.hongsehuojian.com/red-rocket/indexDetail",
}
# 申万行业层级：(内部键, 展示名, 接口字段名)
SW_INDUSTRY_LEVELS = [
    ("L1", "申万一级", "industriesNameOfLevelOne"),
    ("L2", "申万二级", "industriesNameOfLevelTwo"),
]

# 成分股总市值分档（单位：亿元）——(名称, 下限, 上限)，上限为 None 表示不封顶
MARKET_CAP_BUCKETS = [
    ("大盘股", 500, None),
    ("中盘股", 100, 500),
    ("小盘股", None, 100),
]

# 东方财富 secid 市场前缀：1=沪市，0=深市
SECID_PREFIX = {"sh": "1", "sz": "0"}


def _request_with_retry(url, tries=3, sleep_seconds=1.5, **kwargs):
    """带重试的 GET：行情接口常被限流短暂返回 403 / Forbidden。"""
    last_error = None
    for attempt in range(tries):
        try:
            response = requests.get(url, **kwargs)
            if response.status_code == 200:
                return response
            last_error = f"HTTP {response.status_code}"
        except requests.RequestException as e:
            last_error = str(e)
        if attempt < tries - 1:
            time.sleep(sleep_seconds)
    raise requests.RequestException(last_error or "请求失败")


def fetch_etf_from_eastmoney(code, market):
    """东方财富行情接口：f43 为最新价，需按 f59 的小数位还原。"""
    secid = f"{SECID_PREFIX.get(market, '1')}.{code}"
    url = "https://push2.eastmoney.com/api/qt/stock/get"
    params = {"secid": secid, "fields": "f43,f57,f58,f59,f60,f86"}
    try:
        response = _request_with_retry(
            url, params=params, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        payload = response.json()
        data = payload.get("data") or {}
        price, scale = data.get("f43"), data.get("f59")
        if price is None or scale is None:
            return {}
        factor = 10 ** int(scale)
        quote = {
            "price": round(float(price) / factor, 4),
            "name": data.get("f58") or "",
        }
        if data.get("f60") is not None:
            quote["prev_close"] = round(float(data["f60"]) / factor, 4)
        if data.get("f86"):
            quote["quote_date"] = datetime.fromtimestamp(int(data["f86"])).strftime("%Y-%m-%d")
        return quote
    except (requests.RequestException, ValueError, TypeError, KeyError) as e:
        print(f"东方财富 ETF {code} 抓取失败: {e}")
    return {}


def fetch_etf_from_sina(code, market):
    """新浪行情接口，作为东方财富不可用时的备用源。"""
    url = "https://hq.sinajs.cn/list=" + f"{market}{code}"
    try:
        response = _request_with_retry(
            url,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"},
            timeout=20,
        )
        # 该接口返回 GBK 编码文本：名称,今开,昨收,最新价,最高,最低,...,日期,时间
        response.encoding = "gbk"
        body = response.text
        if '="' not in body:
            return {}
        fields = body.split('="')[1].strip('";\n').split(",")
        if len(fields) < 32:
            return {}
        return {
            "price": float(fields[3]),
            "prev_close": float(fields[2]),
            "name": fields[0],
            "quote_date": fields[30],
        }
    except (requests.RequestException, ValueError, IndexError) as e:
        print(f"新浪 ETF {code} 抓取失败: {e}")
    return {}


def fetch_etf_quotes_batch_sina():
    """一次性批量抓取全部 ETF 行情。

    逐只请求极易触发新浪的 Forbidden 限流，批量接口只需一次请求即可拿到全部代码。
    """
    codes = [meta["code"] for meta in ETF_CONFIG.values()]
    if not codes:
        return {}
    symbols = ",".join(f"{meta['market']}{meta['code']}" for meta in ETF_CONFIG.values())
    url = "https://hq.sinajs.cn/list=" + symbols
    quotes = {}
    try:
        response = _request_with_retry(
            url,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"},
            timeout=20,
        )
        response.encoding = "gbk"
        for line in response.text.strip().split("\n"):
            matched = re.search(r'hq_str_[a-z]{2}(\d{6})="', line)
            if not matched or '="' not in line:
                continue
            fields = line.split('="')[1].strip('";\n').split(",")
            if len(fields) < 32:
                continue
            try:
                quotes[matched.group(1)] = {
                    "price": float(fields[3]),
                    "prev_close": float(fields[2]),
                    "name": fields[0],
                    "quote_date": fields[30],
                }
            except (ValueError, IndexError):
                continue
    except (requests.RequestException, ValueError) as e:
        print(f"新浪批量行情抓取失败: {e}")
        return {}

    missing = [c for c in codes if c not in quotes]
    print(f"新浪批量行情：获取 {len(quotes)}/{len(codes)} 只 ETF" + (f"，缺失 {','.join(missing)}" if missing else ""))
    return quotes


def fetch_etf_quote(etf_meta, batch_quotes=None):
    """优先复用批量结果，再按 东方财富 -> 新浪单只 的顺序兜底。"""
    if batch_quotes and etf_meta["code"] in batch_quotes:
        return batch_quotes[etf_meta["code"]]
    for fetcher in (fetch_etf_from_eastmoney, fetch_etf_from_sina):
        quote = fetcher(etf_meta["code"], etf_meta["market"])
        if quote.get("price"):
            return quote
    return {}


def build_etf_block(data_obj, etf_meta, quote):
    """把各条回撤分位线换算成 ETF 价格。

    指数点位与 ETF 价格近似等比，故先由「现价 /(1+当前回撤)」反推出零回撤
    （即历史前高）对应的价格，再按分位线自身的回撤幅度折算：
        price(dd) = peak_price * (1 + dd / 100)
    """
    drawdowns = data_obj.get("drawdowns") or []
    if not drawdowns:
        return {}

    current_dd = float(drawdowns[-1])
    price = float(quote["price"])
    denominator = 1 + current_dd / 100.0
    if abs(denominator) < 1e-9:
        return {}
    peak_price = price / denominator  # 回撤 0% 时的价格，即前高对应价

    levels = {}
    for p_name, series in (data_obj.get("percentiles") or {}).items():
        last_q = series[-1] if series else None
        levels[p_name] = round(peak_price * (1 + float(last_q) / 100.0), 4) if last_q is not None else None

    return {
        "code": etf_meta["code"],
        "name": etf_meta.get("name") or quote.get("name") or "",
        "price": round(price, 4),
        "prev_close": quote.get("prev_close"),
        "price_date": quote.get("quote_date"),
        "index_date": data_obj["dates"][-1] if data_obj.get("dates") else None,
        "current_drawdown": round(current_dd, 2),
        "peak_price": round(peak_price, 4),
        "levels": levels,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _parse_pct(raw):
    """把 '5.96%' 这类文本转成浮点数；'---' 或空值返回 None。"""
    if raw is None:
        return None
    text = str(raw).strip().replace("%", "").replace(",", "")
    if text in ("", "---", "--", "-"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def fetch_fund_period_returns(code):
    """抓取天天基金「阶段涨幅」，返回 {阶段名: 收益率(%)}。"""
    url = "https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
    params = {"type": "jdzf", "code": code, "to": "", "per": "1", "sdate": "", "edate": ""}
    headers = dict(FUND_F10_HEADERS, Referer=f"https://fundf10.eastmoney.com/jdzf_{code}.html")
    result = {}
    try:
        response = requests.get(url, params=params, headers=headers, timeout=25)
        response.encoding = "utf-8"
        for block in re.findall(r"<ul>(.*?)</ul>", response.text, re.S):
            title = re.search(r"class='title'>([^<]*)<", block)
            values = re.findall(r"<li class='tor[^']*'>([^<]*)<", block)
            if not title or not values:
                continue
            pct = _parse_pct(values[0])  # 第一个数值列即“涨幅”
            if pct is not None:
                result[title.group(1).strip()] = pct
    except (requests.RequestException, ValueError, TypeError) as e:
        print(f"抓取基金 {code} 阶段涨幅失败: {e}")
    return result


def fetch_fund_year_returns(code):
    """抓取天天基金「年度涨幅」，返回 {年份字符串: 收益率(%)}。"""
    url = "https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
    params = {"type": "yearzf", "code": code, "to": "", "per": "1", "sdate": "", "edate": ""}
    headers = dict(FUND_F10_HEADERS, Referer=f"https://fundf10.eastmoney.com/jdzf_{code}.html")
    result = {}
    try:
        response = requests.get(url, params=params, headers=headers, timeout=25)
        response.encoding = "utf-8"
        text = response.text
        year_headers = re.findall(r"<th>(\d{4})年度</th>", text)
        values_row = []
        for tr in re.findall(r"<tr>(.*?)</tr>", text, re.S):
            cells = [re.sub(r"<[^>]+>", "", x).strip() for x in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
            if cells and cells[0] == "阶段涨幅":
                values_row = cells[1:]
                break
        for year, raw in zip(year_headers, values_row):
            pct = _parse_pct(raw)
            if pct is not None:
                result[year] = pct
    except (requests.RequestException, ValueError, TypeError) as e:
        print(f"抓取基金 {code} 年度涨幅失败: {e}")
    return result


def compute_index_returns(data_obj):
    """用页面内的全收益指数日序列，算出与天天基金同口径的收益。

    年度收益 = 该年最后一个交易日点位 / 上一年最后一个交易日点位 - 1；
    今年来   = 最新点位 / 上一年最后一个交易日点位 - 1；
    近 N 年  = 最新点位 / N 年前同日（或其之前最后一个交易日）点位 - 1。
    """
    dates = data_obj.get("dates") or []
    values = data_obj.get("values") or []
    if len(dates) < 2 or len(values) < 2:
        return {"periods": {}, "years": []}

    def value_at_or_before(target_date):
        idx = bisect_left(dates, target_date)
        if idx < len(dates) and dates[idx] == target_date:
            return values[idx]
        return values[idx - 1] if idx > 0 else None

    def pct_return(start_value):
        if not start_value:
            return None
        return round((values[-1] / start_value - 1) * 100, 2)

    last_date = datetime.strptime(dates[-1], "%Y-%m-%d")

    periods = {"今年来": pct_return(value_at_or_before(f"{last_date.year - 1}-12-31"))}
    for label, years in (("近1年", 1), ("近3年", 3), ("近5年", 5)):
        try:
            anchor = last_date.replace(year=last_date.year - years)
        except ValueError:  # 2 月 29 日
            anchor = last_date.replace(year=last_date.year - years, day=28)
        periods[label] = pct_return(value_at_or_before(anchor.strftime("%Y-%m-%d")))

    years_out = []
    for y in range(last_date.year - 1, last_date.year - 1 - TRACKING_YEARS, -1):
        start = value_at_or_before(f"{y - 1}-12-31")
        end = value_at_or_before(f"{y}-12-31")
        if start and end:
            years_out.append({"year": y, "value": round((end / start - 1) * 100, 2)})

    return {"periods": periods, "years": years_out}


def fetch_fund_inception(code):
    """从天天基金净值序列取首个净值日（近似成立日）。

    天天基金的“阶段涨幅”对存续不足的基金会给出不可靠数值（例如成立 1.5 年的基金
    也会列出近3年、近5年涨幅），因此需要用真实存续长度来过滤可对比的阶段。
    """
    url = f"https://fund.eastmoney.com/pingzhongdata/{code}.js"
    try:
        response = requests.get(url, headers=FUND_F10_HEADERS, timeout=25)
        response.encoding = "utf-8"
        match = re.search(r"Data_ACWorthTrend\s*=\s*(\[.*?\]);", response.text, re.S)
        if not match:
            return None
        series = json.loads(match.group(1))
        if not series:
            return None
        return datetime.fromtimestamp(series[0][0] / 1000).strftime("%Y-%m-%d")
    except (requests.RequestException, ValueError, TypeError) as e:
        print(f"获取基金 {code} 成立日失败: {e}")
        return None


def build_tracking_block(data_obj, etf_meta):
    """组装「ETF 收益 vs 全收益指数收益」的阶段与年度对比数据。

    差值 = 基金收益 - 指数收益，通常为小幅负值，主要来自管理费、托管费与跟踪误差。
    """
    code = etf_meta["code"]
    index_returns = compute_index_returns(data_obj)
    fund_periods = fetch_fund_period_returns(code)
    fund_years = fetch_fund_year_returns(code)
    inception = fetch_fund_inception(code)

    # 基金真实存续年数：用于剔除天天基金给出的、超出其历史的阶段涨幅
    last_date_str = data_obj.get("dates")[-1] if data_obj.get("dates") else None
    fund_age_years = None
    if inception and last_date_str:
        fund_age_years = (datetime.strptime(last_date_str, "%Y-%m-%d")
                          - datetime.strptime(inception, "%Y-%m-%d")).days / 365.25

    periods = []
    for label, need_years in TRACKING_PERIODS:
        if fund_age_years is not None and need_years and fund_age_years < need_years:
            continue  # 存续不足，该阶段不可比
        fund_value = fund_periods.get(label)
        index_value = index_returns["periods"].get(label)
        if fund_value is None or index_value is None:
            continue
        periods.append({
            "period": label,
            "fund": fund_value,
            "index": index_value,
            "diff": round(fund_value - index_value, 2),
        })

    # 年度对比只保留基金有完整披露的年份，避免出现整列无数据的空行
    years = []
    for item in index_returns["years"]:
        fund_value = fund_years.get(str(item["year"]))
        if fund_value is None:
            continue
        index_value = item["value"]
        years.append({
            "year": item["year"],
            "fund": fund_value,
            "index": index_value,
            "diff": round(fund_value - index_value, 2),
        })

    if not periods and not years:
        return {}

    skipped = [lbl for lbl, need in TRACKING_PERIODS
               if need and fund_age_years is not None and fund_age_years < need]

    return {
        "fund_code": code,
        "fund_name": etf_meta.get("name", ""),
        "inception": inception,
        "source": "天天基金",
        "source_url": f"http://fundf10.eastmoney.com/jdzf_{code}.html",
        "periods": periods,
        "years": years,
        "skipped_periods": skipped,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# 天天基金收益口径：阶段涨幅 + 年度涨幅（均为含分红再投资的复权收益，可与全收益指数直接比较）
FUND_F10_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://fundf10.eastmoney.com/",
}
# 与天天基金“阶段涨幅”对齐的对比口径；数值为该阶段所需的存续年数
TRACKING_PERIODS = [("今年来", 0), ("近1年", 1), ("近3年", 3), ("近5年", 5)]
# 年度对比：最近 5 个完整自然年
TRACKING_YEARS = 5


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

    # 1.1 回撤时长（单位：交易日）
    # 最大回撤天数：近 10 年内，从「最高点」回撤到「最低点」所经历的交易日数。
    # 取窗口内跌幅最深的那一段（即最大回撤对应的峰谷），2015 那种极端行情因超出窗口而自动排除。
    date_series = pd.to_datetime(data_obj["dates"])
    win_cutoff = date_series[-1] - pd.DateOffset(years=10)
    win_start = int(np.flatnonzero(date_series >= win_cutoff)[0]) if len(date_series) else 0
    win_prices = prices[win_start:]
    win_running_max = np.maximum.accumulate(win_prices)
    win_drawdowns = (win_prices / win_running_max - 1) * 100
    trough_idx = int(np.argmin(win_drawdowns))            # 窗口内最低点
    peak_idx = int(np.argmax(win_prices[:trough_idx + 1]))  # 该低点之前的最高点
    data_obj["max_drawdown_days"] = int(trough_idx - peak_idx)

    # 最大修复天数：近 10 年内，从「回撤最低点」涨回「前高」所经历的最长交易日数。
    # 与上一项配对看：一次完整的峰谷循环 ≈ 最大回撤天数（峰→谷）+ 修复天数（谷→突破前高）。
    repair_spans = []
    repair_peak = 0
    for i in range(1, len(win_prices)):
        if win_prices[i] >= win_prices[repair_peak]:
            if i - repair_peak > 1:            # 中间确有回撤才算一轮修复
                segment = win_prices[repair_peak + 1: i]
                trough_i = repair_peak + 1 + int(np.argmin(segment))
                repair_spans.append(i - trough_i)
            repair_peak = i
    data_obj["max_repair_days"] = int(max(repair_spans)) if repair_spans else 0

    data_obj.pop("max_recovery_days", None)   # 旧口径字段已废弃
    data_obj.pop("avg_recovery_days", None)
    # 已回撤天数：从最近一次刷新历史新高，到最新交易日
    running_max = np.maximum.accumulate(prices)
    fresh_highs = np.flatnonzero(prices >= running_max)
    last_peak_idx = int(fresh_highs[-1]) if len(fresh_highs) else 0
    data_obj["current_drawdown_days"] = int(n_total - 1 - last_peak_idx)
    
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
    
    # 3. 对数回归通道：严格使用“最新交易日向前滚动10个日历年”的样本。
    # 这样每次新增交易日后，拟合窗口会同步向前滚动，而非固定使用2520个交易日。
    date_index = pd.to_datetime(data_obj["dates"])
    cutoff_date = date_index[-1] - pd.DateOffset(years=10)
    valid_indices = np.flatnonzero(date_index >= cutoff_date)
    start_idx = int(valid_indices[0]) if len(valid_indices) else 0
    prices_10y = values[start_idx:]
    n_10y = len(prices_10y)

    high_pts_local = find_points(prices_10y, is_high=True)
    low_pts_local = find_points(prices_10y, is_high=False)

    def get_line(pts):
        x = np.array([p[0] for p in pts])
        y = np.array([p[1] for p in pts])
        slope, intercept, _, _, _ = stats.linregress(x, y)
        line = slope * np.arange(n_10y) + intercept
        annual_ret = (np.exp(slope * 252) - 1) * 100
        return line, round(float(annual_ret), 2)

    reg_high_10y, high_ret = get_line(high_pts_local)
    reg_low_10y, low_ret = get_line(low_pts_local)
    x_base = np.arange(n_10y)
    y_base = np.log(prices_10y)
    slope_b, intercept_b, _, _, _ = stats.linregress(x_base, y_base)
    regression_10y = slope_b * x_base + intercept_b

    # 在10年窗口之前填充None，防止旧历史区间出现不属于本轮拟合的通道线。
    prefix = [None] * start_idx
    data_obj["regression"] = prefix + [round(float(v), 4) for v in regression_10y]
    data_obj["reg_high"] = prefix + [round(float(v), 4) for v in reg_high_10y]
    data_obj["reg_low"] = prefix + [round(float(v), 4) for v in reg_low_10y]
    data_obj["high_points"] = [[int(p[0] + start_idx), round(float(p[1]), 4)] for p in high_pts_local]
    data_obj["low_points"] = [[int(p[0] + start_idx), round(float(p[1]), 4)] for p in low_pts_local]
    data_obj["annual_return_high"] = high_ret
    data_obj["annual_return_low"] = low_ret
    data_obj["regression_start_index"] = start_idx
    data_obj["regression_start_date"] = data_obj["dates"][start_idx]
    data_obj["regression_window_years"] = 10

def _normalize_date(raw):
    """把 20260930 / 2026-09-30 统一成 YYYY-MM-DD。"""
    text = str(raw).strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text


def _to_positive_float(raw):
    """把行情字段转成正浮点数，无效值（空、横杠、负值）返回 None。"""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def fetch_csi_weights(code):
    """下载并解析中证指数成分股权重，返回 ([(symbol, 名称, 权重%)], 权重日期)。"""
    response = _request_with_retry(
        CSI_WEIGHT_URL.format(code=code), headers=TENCENT_HEADERS, timeout=30)
    tmp_path = os.path.join(tempfile.gettempdir(), f"closeweight_{code}.xls")
    with open(tmp_path, "wb") as handle:
        handle.write(response.content)

    frame = pd.read_excel(tmp_path, header=0)
    columns = list(frame.columns)
    col_code = next(c for c in columns if "成份券代码" in c)
    col_exchange = next(c for c in columns if c.startswith("交易所"))
    col_weight = next(c for c in columns if "权重" in c)
    col_name = next(c for c in columns if "成份券名称" in c)
    col_date = next(c for c in columns if "日期" in c)

    rows = []
    for _, row in frame.iterrows():
        raw_code = str(row[col_code]).split(".")[0].zfill(6)
        if "上海" in str(row[col_exchange]):
            exchange = "sh"
        elif "深圳" in str(row[col_exchange]):
            exchange = "sz"
        else:
            exchange = "bj"
        # 第 4 位留给行业：中证指数用官网 CICS 接口取，此处不填
        rows.append((
            exchange + raw_code,
            str(row[col_name]),
            float(row[col_weight]),
            None,
        ))
    return rows, _normalize_date(str(frame.iloc[0][col_date]))


def _guess_market(code):
    """国证的样本代码不带交易所前缀，按代码段推断所属市场。"""
    if code[0] in "69":
        return "sh"
    if code[0] in "023":
        return "sz"
    if code[0] in "48":
        return "bj"
    return "sh"


def fetch_cni_weights(code):
    """下载国证指数成分股权重（样本权重 Excel），返回 ([(symbol, 名称, 权重%)], 权重日期)。"""
    response = _request_with_retry(
        CNI_WEIGHT_URL.format(code=code), headers=CNI_HEADERS, timeout=30)
    tmp_path = os.path.join(tempfile.gettempdir(), f"cni_sample_{code}.xls")
    with open(tmp_path, "wb") as handle:
        handle.write(response.content)

    frame = pd.read_excel(tmp_path, header=0)
    columns = list(frame.columns)
    col_code = next(c for c in columns if "样本代码" in c)
    col_weight = next(c for c in columns if "权重" in c)
    col_name = next(c for c in columns if "样本简称" in c)
    col_date = next(c for c in columns if "日期" in c)
    # 国证样本权重文件自带行业列（国证行业分类），作为其行业分布来源
    col_industry = next((c for c in columns if "行业" in str(c)), None)

    rows = []
    for _, row in frame.iterrows():
        raw_code = str(row[col_code]).split(".")[0].strip().zfill(6)
        weight = _to_positive_float(row[col_weight])
        if not raw_code.strip("0") or weight is None:
            continue
        industry = str(row[col_industry]).strip() if col_industry else None
        rows.append((
            _guess_market(raw_code) + raw_code,
            str(row[col_name]),
            weight,
            industry or None,
        ))
    return rows, _normalize_date(str(frame.iloc[0][col_date]))


def fetch_stock_quotes(symbols, batch_size=50):
    """批量抓取个股行情（腾讯），返回 {symbol: 字段列表}。"""
    quotes = {}
    for start in range(0, len(symbols), batch_size):
        batch = symbols[start:start + batch_size]
        response = _request_with_retry(
            TENCENT_QUOTE_URL.format(symbols=",".join(batch)),
            headers=TENCENT_HEADERS, timeout=25)
        response.encoding = "gbk"
        for line in response.text.strip().split("\n"):
            matched = re.match(r'v_([a-z]{2}\d{6})="', line)
            if not matched:
                continue
            fields = line.split('="', 1)[1].rsplit('"', 1)[0].split("~")
            if len(fields) > TENCENT_FIELD_DY:
                quotes[matched.group(1)] = fields
        time.sleep(0.3)
    return quotes


def _parse_industry_rows(data, name_key):
    """把接口返回的行业权重列表整理成 [{name, weight}]（降序）。"""
    items = []
    for row in (data or {}).get("industryWeightList") or []:
        weight = _to_positive_float(row.get("weightPct"))
        name = str(row.get(name_key, "")).strip()
        if weight is None or not name:
            continue
        items.append({"name": name, "weight": round(weight, 2)})
    items.sort(key=lambda x: -x["weight"])
    return items


def fetch_csi_industry(code, level=INDUSTRY_LEVEL):
    """抓取中证指数行业权重，返回 (items, 日期, 分类名称)。

    优先取配置的层级（默认二级）；部分较新的指数只发布一级行业
    （二级接口返回 null），此时自动回退到一级，避免该指数行业分布空白。
    """
    payload = _request_with_retry(
        CSI_INDUSTRY_URL.format(code=code, level=level),
        headers=CSI_HEADERS, timeout=25).json()
    data = (payload.get("data") or {}) if payload.get("success") else {}
    items = _parse_industry_rows(data, "csiType")
    if items:
        level_name = INDUSTRY_LEVEL_NAME.get(level, "")
        return (items, _normalize_date(data.get("updateDate") or ""),
                f"中证行业分类(CICS){level_name}级")

    # 回退：一级行业
    payload = _request_with_retry(
        CSI_INDUSTRY_L1_URL.format(code=code),
        headers=CSI_HEADERS, timeout=25).json()
    data = (payload.get("data") or {}) if payload.get("success") else {}
    items = _parse_industry_rows(data, "csiTypeL1")
    if items:
        return items, _normalize_date(data.get("updateDate") or ""), "中证行业分类(CICS)一级"
    return [], "", ""


def fetch_sw_industries(name):
    """从红色火箭抓取成分股，按权重聚合申万一级/二级行业分布。

    返回 {"source", "date", "levels": {"L1": {"label", "items"}, "L2": {...}}}；
    未配置该指数或抓取失败时返回 {}（由调用方回退到官网分类）。
    """
    code = REDROCKET_CODES.get(name)
    if not code:
        return {}
    try:
        payload = _request_with_retry(
            REDROCKET_COMPONENT_URL.format(code=code),
            headers=REDROCKET_HEADERS, timeout=30).json()
    except Exception as e:
        print(f"⚠️ {name} 申万行业抓取失败: {e}")
        return {}
    if str(payload.get("code")) != "200":
        return {}
    data = payload.get("data") or {}
    rows = data.get("componentStockListVos") or []
    if not rows:
        return {}

    levels = {}
    for key, label, field in SW_INDUSTRY_LEVELS:
        agg = {}
        for row in rows:
            industry = str(row.get(field) or "").strip()
            weight = _to_positive_float(row.get("weight"))
            if not industry or weight is None:
                continue
            agg[industry] = agg.get(industry, 0.0) + weight
        if not agg:
            continue
        items = [{"name": k, "weight": round(v, 2)} for k, v in agg.items()]
        items.sort(key=lambda x: -x["weight"])
        levels[key] = {"label": label, "items": items}
    if not levels:
        return {}

    matched = re.search(r"(\d{4}-\d{2}-\d{2})", str(data.get("showUpdateStr") or ""))
    return {
        "source": "申万宏源·红色火箭",
        "date": matched.group(1) if matched else "",
        "levels": levels,
    }


def build_industry_block(rows, code, source, weight_date, name=None):
    """行业权重分布。

    优先用红色火箭的申万一级/二级（覆盖全部 7 个指数、含各成分股行业名）；
    抓取失败时回退到指数公司官网分类（中证 CICS / 国证行业分类）。
    """
    sw = fetch_sw_industries(name) if name else {}
    if sw:
        default_level = "L2" if "L2" in sw["levels"] else next(iter(sw["levels"]))
        return {
            "items": sw["levels"][default_level]["items"],   # 兼容单层级渲染
            "levels": sw["levels"],
            "default_level": default_level,
            "standard": sw["levels"][default_level]["label"],
            "date": sw["date"] or weight_date,
            "source": sw["source"],
        }
    if source == "CSI":
        items, date, standard = fetch_csi_industry(code)
        if not items:
            return {}
        return {
            "items": items,
            "date": date or weight_date,
            "standard": standard,
            "source": "中证指数官网",
        }
    # 国证：按权重文件里的行业列聚合权重
    agg = {}
    for row in rows:
        industry = row[3] if len(row) > 3 else None
        if not industry:
            continue
        agg[industry] = agg.get(industry, 0.0) + row[2]
    if not agg:
        return {}
    items = [{"name": k, "weight": round(v, 2)} for k, v in agg.items()]
    items.sort(key=lambda x: -x["weight"])
    return {
        "items": items,
        "date": weight_date,
        "standard": "国证行业分类",
        "source": "国证指数官网",
    }


def build_market_cap_block(rows, quotes):
    """按成分股总市值分档统计（大盘/中盘/小盘），复用已抓取的行情。"""
    caps = []
    for row in rows:
        symbol, weight = row[0], row[2]
        fields = quotes.get(symbol)
        if not fields or len(fields) <= TENCENT_FIELD_MARKET_CAP:
            continue
        cap = _to_positive_float(fields[TENCENT_FIELD_MARKET_CAP])
        if cap is None:
            continue
        caps.append((cap, weight))
    if not caps:
        return {}
    total = sum(c for c, _ in caps)
    buckets = []
    for label, low, high in MARKET_CAP_BUCKETS:
        picked = [
            (c, w) for c, w in caps
            if (low is None or c >= low) and (high is None or c < high)
        ]
        buckets.append({
            "name": label,
            "count": len(picked),
            "weight": round(sum(w for _, w in picked), 2),
        })
    return {
        "total": round(total, 2),              # 成分股合计总市值（亿元）
        "average": round(total / len(caps), 2),  # 平均市值（亿元）
        "count": len(caps),
        "buckets": buckets,
    }


def build_valuation_block(code, source="CSI"):
    """按成分股权重加权计算指数的 PE-TTM / PB / 加权股息率。

    股息率是市值的线性函数，用算术加权；
    PE / PB 是「总量之比」，必须用调和加权 1/Σ(w/值)，否则会系统性高估。
    """
    rows, weight_date = (
        fetch_cni_weights(code) if source == "CNI" else fetch_csi_weights(code)
    )
    quotes = fetch_stock_quotes([row[0] for row in rows])

    total_weight = 0.0
    sum_dividend = 0.0
    inv_pe = 0.0
    inv_pb = 0.0
    valid = 0
    quote_dates = []
    for symbol, _name, weight, *_rest in rows:
        fields = quotes.get(symbol)
        if not fields:
            continue
        # 记录行情所属交易日（各成分股取最新），即估值数据的实际日期
        raw_time = fields[TENCENT_FIELD_TIME] if len(fields) > TENCENT_FIELD_TIME else ""
        if len(raw_time) >= 8 and raw_time[:8].isdigit():
            quote_dates.append(raw_time[:8])
        pe = _to_positive_float(fields[TENCENT_FIELD_PE])
        pb = _to_positive_float(fields[TENCENT_FIELD_PB])
        dividend = _to_positive_float(fields[TENCENT_FIELD_DY])
        if pe is None or pb is None or dividend is None:
            continue
        total_weight += weight
        sum_dividend += weight * dividend
        inv_pe += weight / pe
        inv_pb += weight / pb
        valid += 1

    if not total_weight:
        return {}

    return {
        "pe_ttm": round(total_weight / inv_pe, 2) if inv_pe else None,
        "pb": round(total_weight / inv_pb, 2) if inv_pb else None,
        "dividend_yield": round(sum_dividend / total_weight, 2),
        "weight_date": weight_date,
        # 个股行情所属交易日：估值数据本身的日期
        "data_date": _normalize_date(max(quote_dates)) if quote_dates else weight_date,
        "source": "中证指数官网" if source == "CSI" else "国证指数官网",
        "component_count": len(rows),
        "valid_count": valid,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def fetch_index_metrics():
    """计算全部已配置指数的估值 / 市值分布 / 行业分布，返回 {指数名: {...}}。"""
    result = {}
    for name, (code, source) in VALUATION_CONFIG.items():
        try:
            block = build_valuation_block(code, source)
        except Exception as e:  # 第三方文件结构变化不应中断整体更新
            print(f"⚠️ {name} 估值数据计算失败: {e}")
            continue
        if not block:
            continue

        # 复用同一批成分股行情算市值分布（不额外发请求）
        market_cap = {}
        industry = {}
        try:
            rows, weight_date = (
                fetch_cni_weights(code) if source == "CNI" else fetch_csi_weights(code)
            )
            quotes = fetch_stock_quotes([row[0] for row in rows])
            market_cap = build_market_cap_block(rows, quotes)
            industry = build_industry_block(rows, code, source, weight_date, name)
        except Exception as e:
            print(f"⚠️ {name} 画像数据计算失败: {e}")

        result[name] = {"valuation": block, "market_cap": market_cap, "industry": industry}
        print(
            f"{name} 估值：PE-TTM {block['pe_ttm']}｜PB {block['pb']}"
            f"｜加权股息率 {block['dividend_yield']}%"
            f"（权重日 {block['weight_date']}，"
            f"{block['valid_count']}/{block['component_count']} 只有效）"
        )
        if market_cap:
            bucket_text = " / ".join(
                f"{b['name']} {b['count']}只" for b in market_cap["buckets"]
            )
            print(f"    市值分布：合计 {market_cap['total']/10000:.2f} 万亿｜"
                  f"平均 {market_cap['average']:.2f} 亿｜{bucket_text}")
        if industry:
            level_keys = list((industry.get("levels") or {}).keys())
            extra = ""
            if level_keys:
                counts = " / ".join(
                    f"{industry['levels'][k]['label']} {len(industry['levels'][k]['items'])} 个"
                    for k in level_keys)
                extra = f"［{counts}］"
            print(f"    行业分布：{industry['standard']}{extra}，"
                  f"共 {len(industry['items'])} 个行业"
                  f"（{industry['items'][0]['name']} {industry['items'][0]['weight']}% 居首）")
    return result


def update_html(file_path):
    print(f"正在处理文件: {file_path}")
    if not os.path.exists(file_path): return

    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    match = re.search(r'let allData = (\{.*?\});', content, re.DOTALL)
    if not match: return
    
    all_data = json.loads(match.group(1))
    updated_any = False
    # 先批量取一次全部 ETF 行情，避免逐只请求被限流
    batch_quotes = fetch_etf_quotes_batch_sina()

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
        
        if (
            index_updated
            or p_len != len(dates)
            or len(all_data[name].get("reg_high", [])) != len(dates)
            or "regression_start_index" not in all_data[name]
            or all_data[name].get("regression_window_years") != 10
            or "max_drawdown_days" not in all_data[name]
            or "max_repair_days" not in all_data[name]
            or "current_drawdown_days" not in all_data[name]
        ):
            combined = sorted(zip(dates, values), key=lambda x: x[0])
            all_data[name]["dates"] = [x[0] for x in combined]
            all_data[name]["values"] = [x[1] for x in combined]
            
            print(f"正在为 {name} 重新计算所有图表指标...")
            update_index_calculations(all_data[name])
            updated_any = True

        # 同步对应 ETF 的最新收盘价，并重算「各回撤分位线 -> ETF 价格」的换算结果
        etf_meta = ETF_CONFIG.get(name)
        if etf_meta:
            quote = fetch_etf_quote(etf_meta, batch_quotes)
            if quote.get("price"):
                block = build_etf_block(all_data[name], etf_meta, quote)
                previous = all_data[name].get("etf") or {}
                # 仅在价格/日期/分位线换算结果变化时写回，避免每日空跑产生无意义的提交
                if block and (
                    previous.get("price") != block.get("price")
                    or previous.get("price_date") != block.get("price_date")
                    or previous.get("levels") != block.get("levels")
                    or previous.get("current_drawdown") != block.get("current_drawdown")
                ):
                    all_data[name]["etf"] = block
                    updated_any = True
                    print(
                        f"{name} ↔ {etf_meta['code']} 现价 {block['price']}"
                        f"（{block.get('price_date')}）：分位线对应价格已同步"
                    )
            else:
                print(f"⚠️ {name} 对应 ETF {etf_meta['code']} 行情抓取失败，保留上一次价格。")

            # 同步「ETF 收益 vs 全收益指数收益」的跟踪对比数据（来源：天天基金）
            try:
                tracking = build_tracking_block(all_data[name], etf_meta)
            except Exception as e:  # 第三方页面结构变化不应中断整体更新
                print(f"⚠️ {name} 跟踪对比数据计算失败: {e}")
                tracking = {}
            # 仅比较实质字段，忽略每次运行都会变化的 updated_at，避免每日空跑产生无意义提交
            previous_tracking = all_data[name].get("tracking") or {}
            if tracking and (
                previous_tracking.get("periods") != tracking.get("periods")
                or previous_tracking.get("years") != tracking.get("years")
                or previous_tracking.get("inception") != tracking.get("inception")
                or previous_tracking.get("skipped_periods") != tracking.get("skipped_periods")
            ):
                all_data[name]["tracking"] = tracking
                updated_any = True
                hit = [p for p in tracking["periods"] if p["diff"] is not None]
                latest_year = tracking["years"][0] if tracking["years"] else None
                extra = (
                    f"｜{latest_year['year']}年 基金 {latest_year['fund']}% vs 指数 {latest_year['index']}%"
                    if latest_year and latest_year["fund"] is not None
                    else ""
                )
                print(f"{name} 跟踪数据已同步（{len(hit)} 个阶段 / {len(tracking['years'])} 个年度）{extra}")

    # 指数估值（成分股权重加权）：PE-TTM / PB / 加权股息率，以及市值分布 / 行业分布
    for name, blocks in fetch_index_metrics().items():
        if name not in all_data:
            continue
        block = blocks["valuation"]
        previous_valuation = all_data[name].get("valuation") or {}
        # 只比较实质字段，忽略每次都会变的 updated_at，避免无意义的提交
        if (
            previous_valuation.get("pe_ttm") != block["pe_ttm"]
            or previous_valuation.get("pb") != block["pb"]
            or previous_valuation.get("dividend_yield") != block["dividend_yield"]
            or previous_valuation.get("weight_date") != block["weight_date"]
            or previous_valuation.get("source") != block["source"]
            or previous_valuation.get("data_date") != block["data_date"]
        ):
            all_data[name]["valuation"] = block
            updated_any = True

        # 成分股画像：市值分布 + 行业分布
        profile = {
            "market_cap": blocks.get("market_cap") or {},
            "industry": blocks.get("industry") or {},
        }
        if blocks.get("market_cap") or blocks.get("industry"):
            previous_profile = all_data[name].get("profile") or {}
            if (
                previous_profile.get("market_cap") != profile["market_cap"]
                or previous_profile.get("industry") != profile["industry"]
            ):
                all_data[name]["profile"] = profile
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

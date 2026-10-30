import json
import re
import requests
import numpy as np
import pandas as pd
from scipy import stats
from bisect import bisect_left
from datetime import date, datetime, timedelta, timezone
import time
import os
import tempfile

try:
    from zoneinfo import ZoneInfo
    CN_TZ = ZoneInfo("Asia/Shanghai")
except Exception:
    CN_TZ = timezone(timedelta(hours=8))


def now_cn():
    """返回北京时间（兼容无 zoneinfo 的环境）。"""
    return datetime.now(CN_TZ)


def ts_to_date_cn(ts):
    """把 Unix 时间戳转成北京时间日期字符串。"""
    return datetime.fromtimestamp(int(ts), CN_TZ).strftime("%Y-%m-%d")

# 配置指数代码映射
INDEX_CONFIG = [
    {"code": "H00922", "name": "中证红利全收益", "source": "CSI"},
    {"code": "932305CNY010", "name": "智选高股息全收益", "source": "CSI"},
    {"code": "H20269", "name": "红利低波全收益", "source": "CSI"},
    {"code": "H20955", "name": "红利低波100全收益", "source": "CSI"},
    {"code": "932422CNY010", "name": "A500红利低波全收益", "source": "CSI"},
    {"code": "480081", "name": "价值100全收益", "source": "CNI"},
    {"code": "480080", "name": "成长100全收益", "source": "CNI"},
    {"code": "H21187", "name": "中证科技100全收益", "source": "CSI"},
    {"code": "8841431.WI", "name": "万得微盘股指数", "source": "WIND"},
    # 标普中国A股大盘红利低波50指数（总收益）：官网有 Akamai 防护无法程序化抓取。
    # 平时按跟踪 ETF（515450）累计净值的日频涨跌幅推算；ypingcn.com 内嵌与官网
    # 一致的周频官方点位，每周自动校正一次（清除官方末点之后的推算日频点，
    # 只保留官方周频数据），保证推算漂移每周归零。
    {"code": "SPCNALV50TR", "name": "标普红利低波50全收益", "source": "SP"},
    # 回测工具的比较基准：仅用于回测对照，不展示在画像/估值等模块
    {"code": "H00300", "name": "沪深300全收益", "source": "CSI", "benchmark_only": True}
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
    "标普红利低波50全收益": {"code": "515450", "market": "sh", "name": "红利低波50ETF南方"},
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
    "中证科技100全收益": ("931187", "CSI"),
    # 标普指数不公开成分股权重（需 SPICE 账号），用跟踪 ETF（515450）的
    # 中报/年报全持仓近似（≈指数成分股，滞后一个报告期）；行业分布无
    # 可靠申万个股映射源，留空。
    "标普红利低波50全收益": ("515450", "SP"),
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
    """带重试的 GET：对短暂网络错误重试；对明确的客户端错误码直接失败，不重试。"""
    last_error = None
    for attempt in range(tries):
        try:
            response = requests.get(url, **kwargs)
            if response.status_code == 200:
                return response
            last_error = f"HTTP {response.status_code}"
            # 4xx 属于客户端错误，重试通常无效，直接失败
            if 400 <= response.status_code < 500:
                break
        except requests.RequestException as e:
            last_error = str(e)
        if attempt < tries - 1:
            time.sleep(sleep_seconds)
    raise requests.RequestException(last_error or "请求失败")


def fetch_risk_free_rate():
    """抓取中债国债收益率曲线最新的 10 年期到期收益率（%）。

    来源：中国债券信息网收益率曲线接口（yield.chinabond.com.cn），
    曲线每个工作日日终更新，作为回测工具「无风险利率」的默认值。
    返回 (收益率%, 曲线日期)；失败返回 (None, "")。
    """
    url = "https://yield.chinabond.com.cn/cbweb-mn/yc/inityc"
    payload = {"xyzSelect": "txy", "workTime": "", "dxbj": "0", "qxll": "0",
               "yqqxN": "N", "yqqxK": "K", "wrjxCBFlag": "0"}
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Referer": "https://yield.chinabond.com.cn/cbweb-cbrc-web",
    }
    last_error = None
    for attempt in range(3):
        try:
            response = requests.post(url, data=payload, headers=headers, timeout=20)
            if response.status_code == 200:
                data = response.json()
                block = data[1][1][0]
                worktime = str(block.get("worktime", ""))
                for x, y in block["seriesData"]:
                    if abs(float(x) - 10.0) < 1e-6:
                        return round(float(y), 2), worktime
                return None, ""
            last_error = f"HTTP {response.status_code}"
        except (requests.RequestException, ValueError, KeyError, IndexError) as e:
            last_error = str(e)
        if attempt < 2:
            time.sleep(1.5)
    print(f"中债10年期国债收益率抓取失败: {last_error}")
    return None, ""


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
            quote["quote_date"] = ts_to_date_cn(data["f86"])
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

    序列字段（drawdowns/percentiles）已不内嵌 HTML，这里由 values 现算：
    当前回撤 = 末点相对历史前高；分位线末值 = 最后一个完整窗口的分位。
    """
    values = data_obj.get("values") or []
    if not values:
        return {}

    drawdowns = _calc_drawdowns(values)
    current_dd = float(drawdowns[-1])
    price = float(quote["price"])
    denominator = 1 + current_dd / 100.0
    if abs(denominator) < 1e-9:
        return {}
    peak_price = price / denominator  # 回撤 0% 时的价格，即前高对应价

    levels = {}
    for p_name, (years, q) in PERCENTILE_SPECS.items():
        w = 1218 if years == 5 else 2431
        window = drawdowns[-w:]
        if len(window) < w:
            levels[p_name] = None
        else:
            last_q = float(np.percentile(window, (100 - q)))  # linear 插值，同 pandas rolling quantile
            levels[p_name] = round(peak_price * (1 + last_q / 100.0), 4)

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
        "updated_at": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
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
    今年来   = 最新点位 / 上一年最后一个交易日点位 - 1。
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

    # 年度收益：从指数序列起始年到上一个完整自然年，全部列出
    first_year = int(dates[0][:4])
    years_out = []
    for y in range(last_date.year - 1, first_year - 1, -1):
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
        "updated_at": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
    }


# 天天基金收益口径：阶段涨幅 + 年度涨幅（均为含分红再投资的复权收益，可与全收益指数直接比较）
FUND_F10_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://fundf10.eastmoney.com/",
}
# 与天天基金“阶段涨幅”对齐的对比口径；数值为该阶段所需的存续年数
TRACKING_PERIODS = [("今年来", 0)]


def fetch_csi_history(code, last_saved_date):
    """从中证官网新版历史接口获取最近交易日点位。

    该接口必须带 startDate 与 endDate；缺少这两个参数时会返回
    ``Parameter Errors`` 或空数据。仅拉取已保存日期前 14 个自然日至今，
    既能补齐节假日、延迟披露和近期修订，又不会用远端数据覆盖网页历史序列。
    """
    try:
        last_dt = datetime.strptime(last_saved_date, "%Y-%m-%d")
        start_date = (last_dt - timedelta(days=14)).strftime("%Y%m%d")
    except (TypeError, ValueError):
        # 本地无历史数据：全量抓取（新增指数首次嵌入时使用）
        start_date = "20150101"
    end_date = now_cn().strftime("%Y%m%d")
    url = "https://www.csindex.com.cn/csindex-home/perf/index-perf"
    params = {"indexCode": code, "startDate": start_date, "endDate": end_date}
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.csindex.com.cn/"
    }

    try:
        # 中证接口偶发连接重置/限流，统一走带重试的请求（3 次）
        response = _request_with_retry(url, params=params, headers=headers, timeout=30)
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
    url = "https://hq.cnindex.com.cn/market/market/getIndexDailyDataWithDataFormat"
    end_str = now_cn().strftime("%Y-%m-%d")
    start_str = (now_cn() - timedelta(days=60)).strftime("%Y-%m-%d")
    params = {"indexCode": code, "startDate": start_str, "endDate": end_str, "frequency": "day"}
    try:
        r = _request_with_retry(url, params=params, timeout=15)
        data = r.json()
        if data.get("data") and data["data"].get("data"):
            return {row[0]: float(row[5]) for row in data["data"]["data"]}
    except Exception as e:
        print(f"获取国证指数 {code} 历史行情失败: {e}")
    return {}

# A500 风格对比（价值&成长仪表盘「大盘风格」视图）：A500价值全收益 / A500成长全收益。
# 两者均走中证官网全收益代码（CNY010 后缀 = 全收益；CNY020 = 净收益，勿混用）：
#   932497CNY010 = 中证A500价值全收益
#   932494CNY010 = 中证A500成长全收益
# 该数据独立内嵌为 a500StyleData 块（不进入 allData，避免污染估值仪表盘/指数选择器）。
A500_VALUE_CODE = "932497CNY010"
A500_GROWTH_CODE = "932494CNY010"


def build_a500_style_js(existing_content):
    """增量更新 a500StyleData 数据块，返回替换后的 JS 字符串；无变化返回 None。

    与 allData 的增量逻辑一致：只拉取已保存日期前 14 个自然日至今，既补齐
    节假日/延迟披露，又不会用远端数据覆盖网页历史序列。两指数按日期对齐，
    仅保留两个序列都有的交易日（保证前端 log 比值可计算）。
    """
    m = re.search(r'let a500StyleData = (\{.*?\});', existing_content, re.DOTALL)
    if not m:
        print("⚠️ HTML 中未找到 a500StyleData 数据块，跳过 A500 风格数据更新。")
        return None
    try:
        data = json.loads(m.group(1))
    except ValueError as e:
        print(f"⚠️ a500StyleData 数据块解析失败，跳过更新: {e}")
        return None

    dates = data.get("dates") or []
    value = data.get("value") or []
    growth = data.get("growth") or []
    last_saved = max(dates) if dates else None

    dv = fetch_csi_history(A500_VALUE_CODE, last_saved)
    dg = fetch_csi_history(A500_GROWTH_CODE, last_saved)
    # 中证接口偶发连接重置：单指数失败重试两次，避免 Action 单次抖动导致漏更
    if not dv and last_saved:
        for _ in range(2):
            time.sleep(2)
            dv = fetch_csi_history(A500_VALUE_CODE, last_saved)
            if dv:
                break
    if not dg and last_saved:
        for _ in range(2):
            time.sleep(2)
            dg = fetch_csi_history(A500_GROWTH_CODE, last_saved)
            if dg:
                break
    if not dv and not dg:
        print("⚠️ A500 价值/成长指数抓取均失败，保留现有数据。")
        return None

    vmap = dict(zip(dates, value))
    gmap = dict(zip(dates, growth))
    for d, v in dv.items():
        vmap[d] = v
    for d, g in dg.items():
        gmap[d] = g
    # 只保留两序列共有的日期，保持与网页一致的对齐结构
    new_dates = sorted(d for d in set(vmap) | set(gmap) if d in vmap and d in gmap)
    new_value = [round(vmap[d], 2) for d in new_dates]
    new_growth = [round(gmap[d], 2) for d in new_dates]

    if new_dates == dates and new_value == value and new_growth == growth:
        print("A500 风格数据已是最新。")
        return None
    print(f"A500 风格数据：{len(dates)} → {len(new_dates)} 个交易日，最新 {new_dates[-1]}。")
    new_data = {"dates": new_dates, "value": new_value, "growth": new_growth}
    return "let a500StyleData = " + json.dumps(new_data, separators=(',', ':'), ensure_ascii=False) + ";"

# 标普指数（SP 源）：官网有 Akamai 防护无法程序化抓取，更新策略分两层：
#   1) 平时：用跟踪 ETF（红利低波50ETF南方 515450）的「累计净值」日收益推算
#      （fund.eastmoney.com 可直接抓取，无 IP 限制）。实测 2020 年以来与官方
#      TR 日收益相关系数 0.981、日均绝对误差约 0.08 个百分点（与万得微盘的
#      东财代理同量级）；误差随交易日累积（累计净值未按再投资复利计息）。
#   2) 每周：ypingcn.com 内嵌与 S&P 官网一致的周频官方点位，自动校正一次——
#      清除官方末点之后的推算日频点、只保留官方周频数据，再从最新官方点
#      重新推算至今，使推算漂移每周归零。
SP_PROXY_FUND_CODE = "515450"
# ypingcn.com：第三方指数站，点位与 S&P 官网一致（周频采样内嵌于页面）
YPINGCN_URL = "https://ypingcn.com/index-detail/SPCLLHCT/"


def _fetch_sp_proxy_nav():
    """抓取跟踪 ETF（515450）的累计净值序列，返回 {YYYY-MM-DD: 累计净值}。

    时间戳为毫秒，按东八区日界取日期（取 UTC+8，否则日界错位一天）。
    累计净值 = 单位净值 + 历史累计分红，其日变化 ≈ 全收益日收益（含分红）。
    """
    url = f"https://fund.eastmoney.com/pingzhongdata/{SP_PROXY_FUND_CODE}.js"
    r = _request_with_retry(url, timeout=20, headers={
        "User-Agent": TENCENT_HEADERS["User-Agent"],
        "Referer": f"https://fund.eastmoney.com/{SP_PROXY_FUND_CODE}.html",
    })
    match = re.search(r"Data_ACWorthTrend\s*=\s*(\[.*?\]);", r.text)
    if not match:
        raise ValueError(f"{SP_PROXY_FUND_CODE} 净值数据解析失败（Data_ACWorthTrend 缺失）")
    tz_cn = timezone(timedelta(hours=8))
    out = {}
    for t, v in json.loads(match.group(1)):
        d = datetime.fromtimestamp(t / 1000, tz_cn).strftime("%Y-%m-%d")
        out[d] = float(v)
    return out


def _extrapolate_sp_from_fund(last_saved_date, last_saved_value):
    """用 515450 累计净值倒推标普指数总收益点位（仅锚点之后的交易日）。

    换算：TR[d] = TR[锚点] × 累计净值[d] / 累计净值[锚点]，即以锚点日
    官方点位定基、用基金累计净值日收益滚动外推。
    """
    if last_saved_value is None:
        return {}
    nav = _fetch_sp_proxy_nav()
    anchor = last_saved_date
    if anchor not in nav:
        earlier = [d for d in nav if d < anchor]
        if not earlier:
            print(f"标普指数倒推：515450 净值序列早于锚点 {last_saved_date} 无数据，跳过。")
            return {}
        anchor = max(earlier)
        print(f"标普指数倒推：净值序列无 {last_saved_date}，锚点前移至 {anchor}（累计净值比例换算）。")
    base = float(last_saved_value) / nav[anchor]
    out = {d: round(base * v, 2) for d, v in nav.items() if d > anchor}
    if out:
        latest = max(out)
        print(f"标普指数倒推（{SP_PROXY_FUND_CODE} 累计净值）：{last_saved_date} → {latest} 共 {len(out)} 个交易日。")
    return out


def _fetch_sp_official_weekly():
    """抓取 ypingcn.com 指数页内嵌的官方周频全收益点位。

    该站为 Next.js SSR，点位内嵌在 RSC payload 的 initialData.chart_series
    中（约每周 1 个采样点，数据与 S&P 官网一致，起点 2016-09-30）。
    返回 {YYYY-MM-DD: 点位}；抓取/解析失败时抛异常，由调用方容错。
    """
    r = _request_with_retry(YPINGCN_URL, timeout=25, headers={
        "User-Agent": TENCENT_HEADERS["User-Agent"],
    })
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', r.text)
    if not chunks:
        raise ValueError("ypingcn 页面未找到 RSC payload（页面结构可能已变化）")
    text = "".join(c.encode().decode("unicode_escape") for c in chunks)
    # 括号配对提取 initialData 的 JSON
    i = text.find("initialData")
    start = text.find("{", i)
    depth, j, in_str, esc = 0, start, False, False
    while j < len(text):
        ch = text[j]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    break
        j += 1
    obj = json.loads(text[start:j + 1])
    series = (obj.get("all") or {}).get("chart_series") or {}
    dates, prices = series.get("dates") or [], series.get("prices") or []
    if not dates or len(dates) != len(prices):
        raise ValueError("ypingcn chart_series 解析失败（dates/prices 缺失或不等长）")
    return {d: float(v) for d, v in zip(dates, prices)}


def fetch_sp_weights():
    """从天天基金 jjcc 接口解析跟踪 ETF（515450）最新一期「全持仓」明细。

    季报只披露前十大，中报/年报为全持仓（50 只 ≈ 指数成分股），
    取股票数最多的报告期。返回 ([(symbol, 名称, 权重%, None)], 权重日期)。
    """
    url = (
        "https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
        f"?type=jjcc&code={SP_PROXY_FUND_CODE}&topline=100"
    )
    r = _request_with_retry(url, timeout=20, headers={
        "User-Agent": TENCENT_HEADERS["User-Agent"],
        "Referer": f"https://fundf10.eastmoney.com/ccmx_{SP_PROXY_FUND_CODE}.html",
    })
    r.encoding = "utf-8"
    match = re.search(r'content:"(.*?)"\s*,\s*arryear:', r.text, re.DOTALL)
    if not match:
        raise ValueError("515450 持仓接口解析失败（apidata.content 缺失）")
    content = match.group(1).replace('\\"', '"')

    best = None  # (权重日期, rows, 行数)
    for block in re.split(r"<div class='boxitem", content)[1:]:
        dm = re.search(r"截止至：<font class='px12'>(\d{4}-\d{2}-\d{2})</font>", block)
        if not dm:
            continue
        rows_b = []
        for tr in re.finditer(r"<tr>(.*?)</tr>", block, re.S):
            cell = tr.group(1)
            cm = re.search(r"unify/r/([01])\.(\d{6})", cell)
            nm = re.search(r"<td class='tol'><a[^>]*>([^<]+)</a>", cell)
            wm = re.search(r"<td class='tor'>([\d.]+)%</td>", cell)
            if not (cm and nm and wm):
                continue
            weight = float(wm.group(1))
            # 剔除零权重持仓：ETF 中报/年报会残留调样过渡期已基本清仓的
            # 尾仓（实测 22 只合计权重 0.00%），不属于指数成分股，
            # 计入会污染成分股计数与整体法 PE/PB
            if weight <= 0:
                continue
            exchange = {"1": "sh", "0": "sz"}.get(cm.group(1), "bj")
            rows_b.append((exchange + cm.group(2), nm.group(1), weight, None))
        if rows_b and (best is None or len(rows_b) > best[2]):
            best = (dm.group(1), rows_b, len(rows_b))
    if not best or best[2] < 30:
        raise ValueError("未找到 515450 全持仓报告期（中报/年报尚未披露）")
    print(f"标普指数画像：使用 {SP_PROXY_FUND_CODE} 全持仓 {best[2]} 只（权重日 {best[0]}，滞后一个报告期）。")
    return best[1], best[0]


def fetch_wind_history(code, last_saved_date, last_saved_value=None):
    """获取万得微盘股指数（8841431.WI）的最新收盘（推算口径）。

    依据代理指数的每日涨跌幅倒推，填充锚点之后的所有交易日。
    优先东方财富「微盘股」板块指数（90.BK1158，日均绝对误差约 0.09 个百分点），
    被限流时回退到同花顺「微盘股」板块指数（883418，日均绝对误差约 0.28 个百分点）。
    短期倒推可靠；误差会随交易日累积，属"大致推算"口径，可接受。
    只返回晚于本地已保存日期的交易日。
    """
    result = {}
    if last_saved_value is not None:
        result = _extrapolate_wind_from_bk1158(last_saved_date, last_saved_value)
        if not result:
            result = _extrapolate_wind_from_ths(last_saved_date, last_saved_value)
    if result:
        latest = max(result)
        print(f"万得微盘股指数{code}：本次推算 {len(result)} 个新交易日，最新 {latest} / {result[latest]:.2f}")
    return result


EASTMONEY_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
# 东方财富「微盘股」板块指数，作为万得微盘股（868008.WI）每日涨幅的近似代理
WIND_PROXY_SECID = "90.BK1158"
# 同花顺「微盘股」板块指数（883418），作为万得微盘股（8841431.WI）每日涨幅的备用代理。
# 实测与万得微盘日涨跌幅相关系数约 0.982、日均绝对误差约 0.28 个百分点，短期倒推可靠。
# 当东财 push2 系列行情接口被数据中心 IP 拦截（403）时启用此备用源。
THS_KLINE_URL = "https://d.10jqka.com.cn/v6/line/bk_883418/01/last.js"
THS_HEADERS = {
    "User-Agent": TENCENT_HEADERS["User-Agent"],
    "Referer": "http://stockpage.10jqka.com.cn/",
    "Cookie": "v=AAAAAAAAAAAA; hexin-v=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
}
# PB 水位区间（万得微盘日频等权 8841431.WI 近10年 PB 的最低/最高，据用户提供的准确数据）
WIND_PB_MIN, WIND_PB_MAX = 1.3429, 5.2516

# 西部证券《微盘周报》：每周发布，公开可抓，正文含「万得微盘股指数成分股 PB 中位数」
SINA_REPORT_LIST = "https://stock.finance.sina.com.cn/stock/go.php/vReport_List/kind/search/index.phtml"
SINA_REPORT_SHOW = "https://stock.finance.sina.com.cn/stock/go.php/vReport_Show/kind/search/rptid/{rptid}/index.phtml"
SINA_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://stock.finance.sina.com.cn/"}


def _gbk_urlencode(text):
    """新浪研报页面为 GBK 编码，标题参数需按 GBK 做百分号编码。"""
    return "".join("%%%02X" % b for b in text.encode("gbk"))


def _fetch_latest_weekly_report_text():
    """抓取最新一期西部证券《微盘周报》正文文本（GBK 已解码），失败返回 None。"""
    try:
        url = SINA_REPORT_LIST + "?t1=5&title=" + _gbk_urlencode("微盘周报")
        r = requests.get(url, headers=SINA_HEADERS, timeout=20)
        r.raise_for_status()
        html = r.content.decode("gbk", errors="replace")
        m = re.search(r'rptid/(\d+)', html)
        if not m:
            print("西部证券微盘周报：标题搜索未找到报告。")
            return None
        r2 = requests.get(SINA_REPORT_SHOW.format(rptid=m.group(1)), headers=SINA_HEADERS, timeout=20)
        r2.raise_for_status()
        return r2.content.decode("gbk", errors="replace")
    except Exception as e:
        print(f"西部证券微盘周报抓取失败: {e}")
        return None


def _parse_report_date(text):
    """解析周报正文里的「截至 YYYY 年 MM 月 DD 日」，失败返回 None。"""
    m = re.search(r'截至\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日', text)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return None


def fetch_wind_pb_from_report():
    """从西部证券《微盘周报》抓取万得微盘股指数 PB 中位数（每周更新一次）。

    链路：新浪研报标题搜索「微盘周报」→ 取最新一期 → 解析正文里的
    「微盘股指数成分股pb中位数」与「截至YYYY年MM月DD日」。
    返回 (pb, "YYYY-MM-DD")；任一步失败返回 (None, None)。
    """
    text = _fetch_latest_weekly_report_text()
    if not text:
        return None, None
    pb_m = re.search(r'微盘股指数成分股pb\s*中位数[为是：:]*\s*([\d.]+)', text)
    if not pb_m:
        print("西部证券微盘周报：正文未解析到 pb 中位数。")
        return None, None
    pb = float(pb_m.group(1))
    d = _parse_report_date(text) or now_cn().strftime("%Y-%m-%d")
    print(f"西部证券微盘周报：PB 中位数 {pb}（{d}）")
    return pb, d


def fetch_wind_crowding_from_report():
    """从西部证券《微盘周报》抓取 市值中位数 与 成交额占比（每周更新一次）。

    返回 (mcap_median, turnover_share, "YYYY-MM-DD")；某项缺失时为 None；
    任一步失败返回 (None, None, None)。
    """
    text = _fetch_latest_weekly_report_text()
    if not text:
        return None, None, None
    d = _parse_report_date(text) or now_cn().strftime("%Y-%m-%d")
    mcap = None
    m1 = re.search(r'微盘股指数成分股总市值中位数[为是：:]*\s*([\d.]+)\s*亿元', text)
    if m1:
        mcap = float(m1.group(1))
    turnover = None
    m2 = re.search(r'微盘股成交占比\s*([\d.]+)\s*%', text)
    if m2:
        turnover = float(m2.group(1))
    if mcap is None and turnover is None:
        print("西部证券微盘周报：正文未解析到 市值中位数/成交占比。")
        return None, None, None
    print(f"西部证券微盘周报：市值中位数 {mcap} 亿元｜成交占比 {turnover}%（{d}）")
    return mcap, turnover, d


def _apply_wind_pb(data_obj):
    """万得微盘 PB：从西部证券《微盘周报》抓取最新 PB 中位数并重算水位。

    每周新报告出来后，若「截至日期」晚于页面内记录，则更新 PB 与水位；
    抓取失败或未出新报告时保持原值（不产生无意义提交）。
    """
    pb, pb_date = fetch_wind_pb_from_report()
    if pb is None or not pb_date:
        return False
    val = data_obj.get("valuation") or {}
    old_date = val.get("data_date") or ""
    if pb_date <= old_date:
        print(f"万得微盘 PB：周报日期 {pb_date} 不新于已有 {old_date}，跳过。")
        return False
    new_pct = round((pb - WIND_PB_MIN) / (WIND_PB_MAX - WIND_PB_MIN) * 100, 1)
    val["pb"] = pb
    val["pb_percentile_10y"] = new_pct
    val["data_date"] = pb_date
    val["weight_date"] = pb_date.replace("-", "")
    val["source"] = "西部证券微盘周报·PB中位数"
    print(f"万得微盘股指数PB 更新为 {pb}（{pb_date}，水位 {new_pct}%）")

    # 同步把新一周的 PB 追加到周度序列（微盘股仪表盘的 PB 图用），并裁剪到近10年
    pw = data_obj.get("pb_weekly")
    if pw and pw.get("dates") and pb_date > pw["dates"][-1]:
        if pb_date in data_obj["dates"]:
            pt = data_obj["values"][data_obj["dates"].index(pb_date)]
        else:
            pt = data_obj["values"][-1]
        pw["dates"].append(pb_date)
        pw["pb"].append(pb)
        pw["point"].append(pt)
        cutoff = (pd.Timestamp(pw["dates"][-1]) - pd.DateOffset(years=10)).strftime("%Y-%m-%d")
        while pw["dates"] and pw["dates"][0] < cutoff:
            pw["dates"].pop(0)
            pw["pb"].pop(0)
            pw["point"].pop(0)
        print(f"周度 PB 序列已追加 {pb_date}（现 {len(pw['dates'])} 周，起点 {pw['dates'][0]}）")
    return True


def _apply_wind_crowding(data_obj):
    """微盘股拥挤度：从西部证券《微盘周报》抓取 市值中位数 / 成交额占比，
    追加到各自周度序列（滚动近10年窗口），供微盘股仪表盘使用。
    """
    mcap, turnover, d = fetch_wind_crowding_from_report()
    if (mcap is None and turnover is None) or not d:
        return False
    changed = False
    for key, val in (("mcap_median_weekly", mcap), ("turnover_weekly", turnover)):
        if val is None:
            continue
        series = data_obj.get(key)
        if not series or not series.get("dates"):
            continue
        if d > series["dates"][-1]:
            series.setdefault("point", [])
            # 取不超过该日期的最近交易日点位
            prev_pts = [(dt, v) for dt, v in zip(data_obj["dates"], data_obj["values"]) if dt <= d]
            pt = round(float(prev_pts[-1][1]), 2) if prev_pts else None
            series["dates"].append(d)
            series["value"].append(round(float(val), 4))
            series["point"].append(pt)
            cutoff = (pd.Timestamp(d) - pd.DateOffset(years=10)).strftime("%Y-%m-%d")
            while series["dates"] and series["dates"][0] < cutoff:
                series["dates"].pop(0)
                series["value"].pop(0)
                if series["point"]:
                    series["point"].pop(0)
            changed = True
    if changed:
        print(f"微盘股拥挤度周度序列已更新（{d}）")
    return changed


def _extrapolate_wind_from_bk1158(last_saved_date, last_saved_value):
    """用东财微盘板块（BK1158）的每日涨跌幅倒推万得微盘股指数点位。

    实测：BK1158 与万得微盘的日涨跌幅平均绝对误差约 0.09 个百分点，短期倒推可靠；
    误差会随交易日累积，长期会逐步偏离真实值（推算口径，可接受）。
    """
    try:
        beg = (datetime.strptime(last_saved_date, "%Y-%m-%d") - timedelta(days=10)).strftime("%Y%m%d")
        end = now_cn().strftime("%Y%m%d")
        params = {
            "secid": WIND_PROXY_SECID,
            "ut": "fa5fd1943c7b386f172d6893dbfba10b",
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f53",
            "klt": "101",
            "fqt": "1",
            "beg": beg,
            "end": end,
        }
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://quote.eastmoney.com/",
        }
        r = requests.get(EASTMONEY_KLINE_URL, params=params, headers=headers, timeout=20)
        r.raise_for_status()
        klines = ((r.json().get("data") or {}).get("klines")) or []
        closes = {}
        for line in klines:
            p = line.split(",")
            if len(p) >= 3 and p[0]:
                closes[p[0]] = float(p[2])

        if last_saved_date not in closes:
            print(f"东财微盘板块倒推：K 线中找不到锚点日期 {last_saved_date}，跳过。")
            return {}

        out = {}
        prev_d = last_saved_date
        prev_v = float(last_saved_value)
        days = [d for d in sorted(closes) if d > last_saved_date]
        if len(days) > 30:
            print(f"东财微盘板块倒推：距锚点已 {len(days)} 个交易日，推算累计漂移会偏大。")
        for d in days:
            prev_v = prev_v * (closes[d] / closes[prev_d])
            out[d] = round(prev_v, 2)
            prev_d = d
        if out:
            print(f"东财微盘板块倒推：{last_saved_date} → {days[-1]} 共 {len(days)} 个交易日（代理 90.BK1158）。")
        return out
    except Exception as e:
        print(f"东财微盘板块倒推失败: {e}")
        return {}


def _extrapolate_wind_from_ths(last_saved_date, last_saved_value):
    """用同花顺「微盘股」板块指数（883418）的每日涨跌幅倒推万得微盘股指数点位。

    该接口返回最近约 140 个交易日的日 K 线（JSONP），收盘价位于每行第 5 个字段。
    仅填充锚点之后的新交易日；实测与万得微盘日涨跌幅相关系数约 0.982，
    短期倒推可靠；误差会随交易日累积，属"大致推算"口径，可接受。
    """
    try:
        response = _request_with_retry(THS_KLINE_URL, headers=THS_HEADERS, timeout=20)
        text = response.text
        payload = json.loads(text[text.find("(") + 1: text.rfind(")")])
        closes = {}
        for row in (payload.get("data") or "").split(";"):
            parts = row.split(",")
            if len(parts) >= 5 and parts[0]:
                d = f"{parts[0][:4]}-{parts[0][4:6]}-{parts[0][6:]}"
                closes[d] = float(parts[4])

        if last_saved_date not in closes:
            print(f"同花顺微盘板块倒推：K 线中找不到锚点日期 {last_saved_date}，跳过。")
            return {}

        out = {}
        prev_d = last_saved_date
        prev_v = float(last_saved_value)
        days = [d for d in sorted(closes) if d > last_saved_date]
        if len(days) > 30:
            print(f"同花顺微盘板块倒推：距锚点已 {len(days)} 个交易日，推算累计漂移会偏大。")
        for d in days:
            prev_v = prev_v * (closes[d] / closes[prev_d])
            out[d] = round(prev_v, 2)
            prev_d = d
        if out:
            print(f"同花顺微盘板块倒推：{last_saved_date} → {days[-1]} 共 {len(days)} 个交易日（代理 bk_883418）。")
        return out
    except Exception as e:
        print(f"同花顺微盘板块倒推失败: {e}")
        return {}

# 滚动分位线规格：字段名 -> (窗口年数, 分位 q)。
# Excel 口径：实际分位参数 p = (100-q)/100（如「50%分位」用 p=0.25），
# 窗口 5年=1218 天、10年=2431 天。update_index_calculations 与
# build_etf_block（ETF 价格分位线只需末值）共用此规格。
PERCENTILE_SPECS = {
    "滚动五年50%分位": (5, 50),
    "滚动五年70%分位": (5, 70),
    "滚动五年90%分位": (5, 90),
    "滚动十年50%分位": (10, 50),
    "滚动十年70%分位": (10, 70),
    "滚动十年90%分位": (10, 90),
}


def _calc_drawdowns(values):
    """由点位序列计算回撤百分比序列（与 numpy 口径一致，供脚本内部使用）。

    序列 drawdowns/percentiles/reg_high/reg_low 等不再内嵌进 HTML
    （体积优化，前端由 values 实时重建），脚本仅在内部计算标量时使用。
    """
    peak = float("-inf")
    out = []
    for v in values:
        if v > peak:
            peak = v
        out.append((v / peak - 1) * 100)
    return out


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

# 统计历次回撤时长时的最小跌幅门槛：峰→谷跌幅不足该值的回调视为噪音，不计入样本
MIN_DD_PCT = 5.0


def update_index_calculations(data_obj):
    values = data_obj["values"]
    prices = np.array(values)
    n_total = len(prices)
    
    # 1. 计算回撤（drawdowns 序列仅内部使用，不写入 HTML，前端实时重建）
    max_prices = np.maximum.accumulate(prices)
    drawdowns = (prices / max_prices - 1) * 100
    data_obj["max_drawdown"] = round(float(np.min(drawdowns)), 2)

    # 1.1 回撤时长（单位：交易日）
    # 回撤/修复天数中位数：近 10 年内所有「已完成的回撤轮次」的峰→谷、谷→峰时长。
    # 轮次口径：谷底后成功突破前高，且峰→谷跌幅 ≥ MIN_DD_PCT（过滤 1-3 天的噪音波动，
    # 否则大量微型回调会把中位数拉到无参考价值的 2 天左右）。
    # 用中位数而非极值/均值：时长分布右偏严重（个别轮回拖数年），中位数更稳健，
    # 代表"一轮回撤通常多久见底、多久修复"的常态水平；进行中的轮回不计入
    #（其峰谷时长已单独展示在「已回撤天数 / 已回撤修复天数」卡片）。
    date_series = pd.to_datetime(data_obj["dates"])
    win_cutoff = date_series[-1] - pd.DateOffset(years=10)
    win_start = int(np.flatnonzero(date_series >= win_cutoff)[0]) if len(date_series) else 0
    win_prices = prices[win_start:]
    dd_spans, repair_spans = [], []
    repair_peak = 0
    for i in range(1, len(win_prices)):
        if win_prices[i] >= win_prices[repair_peak]:
            if i - repair_peak > 1:            # 中间确有回撤才算一轮轮回
                segment = win_prices[repair_peak + 1: i]
                trough_i = repair_peak + 1 + int(np.argmin(segment))
                if (1 - win_prices[trough_i] / win_prices[repair_peak]) * 100 >= MIN_DD_PCT:
                    dd_spans.append(trough_i - repair_peak)
                    repair_spans.append(i - trough_i)
            repair_peak = i
    data_obj["median_drawdown_days"] = int(np.median(dd_spans)) if dd_spans else 0
    data_obj["median_repair_days"] = int(np.median(repair_spans)) if repair_spans else 0

    data_obj.pop("max_recovery_days", None)   # 旧口径字段已废弃
    data_obj.pop("avg_recovery_days", None)
    # 已回撤天数 / 已回撤修复天数：当前这轮回撤的两段时长（交易日）。
    # 峰 = 最近一次刷新历史新高的交易日；谷 = 峰后至今的最低点。
    # 已回撤天数 = 峰→谷；已回撤修复天数 = 谷→最新交易日（尚未突破前高）。
    # 最新点位已创新高（不在回撤中）时，两段均为 0。
    running_max = np.maximum.accumulate(prices)
    # 浮点精度保护：用微小容差避免漏判新高
    fresh_highs = np.flatnonzero(prices + 1e-6 >= running_max)
    last_peak_idx = int(fresh_highs[-1]) if len(fresh_highs) else 0
    if n_total - 1 > last_peak_idx:
        cur_trough_idx = last_peak_idx + 1 + int(np.argmin(prices[last_peak_idx + 1:]))
        data_obj["current_drawdown_days"] = int(cur_trough_idx - last_peak_idx)   # 峰→谷
        data_obj["current_repair_days"] = int(n_total - 1 - cur_trough_idx)       # 谷→最新
    else:
        data_obj["current_drawdown_days"] = 0
        data_obj["current_repair_days"] = 0
    
    # 2. 滚动分位线已不再写入 HTML（前端由 values 实时重建，见 rebuildDerivedSeries），
    #    仅清理历史遗留序列字段（见下方 pop）。

    # 序列字段（percentiles / reg_high / reg_low / regression / drawdowns /
    # quantile_lines / log_values / top_line / bottom_line / high/low_points）
    # 已不再内嵌 HTML——体积优化，前端由 values 实时重建（见 HTML 内
    # rebuildDerivedSeries()）。这里仅清理历史 HTML 中遗留的序列字段，
    # 避免旧文件跑脚本时残留（单指数约省 270KB）。
    for legacy_key in ("percentiles", "reg_high", "reg_low", "regression",
                       "drawdowns", "quantile_lines", "log_values",
                       "top_line", "bottom_line", "high_points", "low_points"):
        data_obj.pop(legacy_key, None)
    
    # 3. 对数回归通道：严格使用“最新交易日向前滚动10个日历年”的样本。
    # 这样每次新增交易日后，拟合窗口会同步向前滚动，而非固定使用2520个交易日。
    # 通道序列本身不写入 HTML（前端实时重建），这里只保留年化收益标量。
    date_index = pd.to_datetime(data_obj["dates"])
    cutoff_date = date_index[-1] - pd.DateOffset(years=10)
    valid_indices = np.flatnonzero(date_index >= cutoff_date)
    start_idx = int(valid_indices[0]) if len(valid_indices) else 0
    prices_10y = values[start_idx:]

    def get_annual_return(pts):
        x = np.array([p[0] for p in pts])
        y = np.array([p[1] for p in pts])
        slope, _, _, _, _ = stats.linregress(x, y)
        return round(float((np.exp(slope * 252) - 1) * 100), 2)

    data_obj["annual_return_high"] = get_annual_return(find_points(prices_10y, is_high=True))
    data_obj["annual_return_low"] = get_annual_return(find_points(prices_10y, is_high=False))
    data_obj["regression_start_index"] = start_idx
    data_obj["regression_start_date"] = data_obj["dates"][start_idx]
    data_obj["regression_window_years"] = 10

    # 序列字段（percentiles / reg_high / reg_low / regression / drawdowns /
    # quantile_lines / log_values / top_line / bottom_line / high/low_points）
    # 已不再内嵌 HTML——体积优化，前端由 values 实时重建（见 HTML 内
    # rebuildDerivedSeries()）。这里清理历史 HTML 中遗留的序列字段，
    # 避免旧文件跑脚本时残留（单指数约省 330KB）。
    for legacy_key in ("percentiles", "reg_high", "reg_low", "regression",
                       "drawdowns", "quantile_lines", "log_values",
                       "top_line", "bottom_line", "high_points", "low_points"):
        data_obj.pop(legacy_key, None)

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


def _to_float(raw):
    """把行情字段转成浮点数（允许负值，如亏损股的负市盈率），无效值返回 None。"""
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _find_column(columns, *candidates):
    """按候选子串依次查找列名，找不到时抛出清晰的 ValueError。"""
    for pat in candidates:
        for col in columns:
            if pat in col:
                return col
    raise ValueError(f"未在列 {columns} 中找到匹配 {candidates} 的列")


def fetch_csi_weights(code):
    """下载并解析中证指数成分股权重，返回 ([(symbol, 名称, 权重%)], 权重日期)。"""
    response = _request_with_retry(
        CSI_WEIGHT_URL.format(code=code), headers=TENCENT_HEADERS, timeout=30)
    tmp_path = os.path.join(tempfile.gettempdir(), f"closeweight_{code}.xls")
    with open(tmp_path, "wb") as handle:
        handle.write(response.content)

    frame = pd.read_excel(tmp_path, header=0)
    columns = list(frame.columns)
    col_code = _find_column(columns, "成份券代码", "代码")
    col_exchange = _find_column(columns, "交易所")
    col_weight = _find_column(columns, "权重")
    col_name = _find_column(columns, "成份券名称", "名称")
    col_date = _find_column(columns, "日期")

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
    """国证的样本代码不带交易所前缀，按代码段推断所属市场。

    北交所常见代码段：43xxxx / 83xxxx / 87xxxx / 920xxx。
    """
    if code[0] in "69" or code.startswith("920"):
        return "sh" if code[0] in "69" else "bj"
    if code[0] in "023":
        return "sz"
    if code[0] in "438":
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
    col_code = _find_column(columns, "样本代码", "代码")
    col_weight = _find_column(columns, "权重")
    col_name = _find_column(columns, "样本简称", "简称", "名称")
    col_date = _find_column(columns, "日期")
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


def build_valuation_block(rows, quotes, weight_date, source="CSI"):
    """按成分股整体法计算指数的 PE-TTM / PB，股息率沿用指数权重算术加权。

    PE / PB 口径与中证/国证指数官网及主流第三方平台一致（成分股加权平均）：
        PE-TTM = Σ总市值 / Σ净利润TTM = Σ总市值 / Σ(总市值/PE)
        PB     = Σ总市值 / Σ净资产    = Σ总市值 / Σ(总市值/PB)
    二者等价于按总市值加权的调和平均。

    注意：不能用指数权重加权——红利类指数按股息率加权、红利低波按
    波动率加权，指数权重 ≠ 总市值权重，用指数权重做调和/算术平均会
    系统性偏离官网整体法口径（实测中证红利 PE 10.73 vs 官网 9.01）。

    细节：
    - 亏损股（PE-TTM 为负）的负盈利计入分母，与官网口径一致；
    - PE / PB 各自独立取有效样本，个股任一项缺失不影响另一项；
    - 股息率维持原口径：指数权重算术加权，且个股需 PE / PB / 股息率
      三者同时有效才计入。
    """
    caps_pe, earnings = [], []      # (总市值, 净利润TTM=市值/PE)
    caps_pb, book_values = [], []   # (总市值, 净资产=市值/PB)
    total_weight = 0.0              # 股息率：指数权重算术加权（保持原口径）
    sum_dividend = 0.0
    valid_dy = 0
    quote_dates = []
    for symbol, _name, weight, *_rest in rows:
        fields = quotes.get(symbol)
        if not fields:
            continue
        # 记录行情所属交易日（各成分股取最新），即估值数据的实际日期
        raw_time = fields[TENCENT_FIELD_TIME] if len(fields) > TENCENT_FIELD_TIME else ""
        if len(raw_time) >= 8 and raw_time[:8].isdigit():
            quote_dates.append(raw_time[:8])
        cap = _to_positive_float(fields[TENCENT_FIELD_MARKET_CAP])
        pe = _to_float(fields[TENCENT_FIELD_PE])            # 允许负值（亏损股）
        pb = _to_positive_float(fields[TENCENT_FIELD_PB])
        dividend = _to_positive_float(fields[TENCENT_FIELD_DY])

        # PE / PB：整体法，独立取有效样本（含亏损股负盈利）
        if cap is not None:
            if pe:
                caps_pe.append(cap)
                earnings.append(cap / pe)
            if pb:
                caps_pb.append(cap)
                book_values.append(cap / pb)

        # 股息率：保持原口径（指数权重算术加权；个股需三项指标同时有效）
        if pe is None or pe <= 0 or pb is None or dividend is None:
            continue
        total_weight += weight
        sum_dividend += weight * dividend
        valid_dy += 1

    if not (caps_pe or caps_pb or total_weight):
        return {}

    return {
        "pe_ttm": round(sum(caps_pe) / sum(earnings), 2) if earnings else None,
        "pb": round(sum(caps_pb) / sum(book_values), 2) if book_values else None,
        "dividend_yield": round(sum_dividend / total_weight, 2) if total_weight else None,
        "weight_date": weight_date,
        # 个股行情所属交易日：估值数据本身的日期
        "data_date": _normalize_date(max(quote_dates)) if quote_dates else weight_date,
        "source": {"CSI": "中证指数官网", "CNI": "国证指数官网"}.get(source, "天天基金·ETF全持仓"),
        "component_count": len(rows),
        "valid_count": valid_dy,
        "updated_at": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _history_sheet_name(index_name):
    """指数名 → 历史数据键名（去掉「全收益」后缀）。"""
    return index_name[:-3] if index_name.endswith("全收益") else index_name


def load_pepb_history(content):
    """从 HTML 文本中解析内嵌的 pepbHistory 历史序列。

    返回 {指数名: {"dates": [...], "pe": [...], "pb": [...]}}，日期升序。
    找不到内嵌块时返回空字典。
    """
    match = re.search(r'const pepbHistory = (\{.*?\});', content, re.DOTALL)
    if not match:
        return {}
    try:
        return json.loads(match.group(1))
    except (ValueError, TypeError):
        return {}


def dump_pepb_history(history):
    """把历史序列序列化成 HTML 内嵌的 JS 常量文本。"""
    body = json.dumps(history, ensure_ascii=False, separators=(",", ":"))
    return f"const pepbHistory = {body};"


def append_history_entry(history, index_name, trade_date, pe, pb):
    """把最新估值追加进内存中的历史序列（日期升序），返回是否写入。幂等：同日期跳过。"""
    key = _history_sheet_name(index_name)
    entry = history.setdefault(key, {"dates": [], "pe": [], "pb": []})
    if trade_date in entry["dates"]:
        return False
    entry["dates"].append(trade_date)
    entry["pe"].append(pe)
    entry["pb"].append(pb)
    # 保持日期升序
    order = sorted(range(len(entry["dates"])), key=lambda i: entry["dates"][i])
    entry["dates"] = [entry["dates"][i] for i in order]
    entry["pe"] = [entry["pe"][i] for i in order]
    entry["pb"] = [entry["pb"][i] for i in order]
    return True


def percentile_in_10y(dates, values, current):
    """current 在近10年历史序列中的分位%（0~100，越高代表当前估值越贵）。

    与工作簿内 F/G 列公式口径一致：100 × 小于当前值的个数 ÷ (有效值个数 − 1)。
    """
    if not dates or len(values) < 2 or current is None:
        return None
    latest = dates[-1]
    try:
        year, month, day = (int(x) for x in latest.split("-"))
        cutoff = date(year - 10, month, day).isoformat()
    except ValueError:  # 2月29日往前推10年不是闰日 → 回退 2月28日
        cutoff = f"{int(latest[:4]) - 10}-{latest[5:10]}"
    window = [v for dt, v in zip(dates, values) if dt >= cutoff]
    if len(window) < 2:
        return None
    below = sum(1 for v in window if v < current)
    return round(below / (len(window) - 1) * 100, 1)


def fetch_index_metrics():
    """计算全部已配置指数的估值 / 市值分布 / 行业分布，返回 {指数名: {...}}。

    每个指数只下载一次成分股权重、抓取一次个股行情，供估值、市值分布
    与行业分布共用，避免重复请求被数据源限流。
    """
    result = {}
    for name, (code, source) in VALUATION_CONFIG.items():
        try:
            if source == "SP":
                # 标普指数：无公开成分股文件，用跟踪 ETF 全持仓近似
                rows, weight_date = fetch_sp_weights()
            else:
                rows, weight_date = (
                    fetch_cni_weights(code) if source == "CNI" else fetch_csi_weights(code)
                )
            quotes = fetch_stock_quotes([row[0] for row in rows])
        except Exception as e:  # 第三方文件结构变化不应中断整体更新
            print(f"⚠️ {name} 成分股数据抓取失败: {e}")
            continue

        block = build_valuation_block(rows, quotes, weight_date, source)
        if not block:
            continue

        market_cap = build_market_cap_block(rows, quotes)
        try:
            industry = build_industry_block(rows, code, source, weight_date, name)
        except Exception as e:
            print(f"⚠️ {name} 行业分布计算失败: {e}")
            industry = {}

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

    # 更新回测「无风险利率」默认值：中债10年期国债最新到期收益率
    rf_val, rf_date = fetch_risk_free_rate()
    if rf_val:
        new_content = re.sub(r'let RF_DEFAULT = [\d.]+;', f'let RF_DEFAULT = {rf_val};', content)
        if new_content != content:
            print(f"回测无风险利率默认值更新为 {rf_val}%（中债国债收益率曲线 {rf_date}）")
            content = new_content
            # allData 替换基于 match 坐标，RF 替换不改变其位置（等宽文本），此处直接重定位更稳妥
            match = re.search(r'let allData = (\{.*?\});', content, re.DOTALL)
            updated_any = True
        else:
            print(f"回测无风险利率默认值保持 {rf_val}%（中债 {rf_date}）")

    # 先批量取一次全部 ETF 行情，避免逐只请求被限流
    batch_quotes = fetch_etf_quotes_batch_sina()

    for item in INDEX_CONFIG:
        code, name, source = item["code"], item["name"], item["source"]
        if name not in all_data: continue
            
        dates, values = all_data[name]["dates"], all_data[name]["values"]
        last_saved_date = max(dates) if dates else None
        recent_data = {}
        try:
            if source == "CSI":
                recent_data = fetch_csi_history(code, last_saved_date)
            elif source == "CNI":
                recent_data = fetch_cni_history(code)
            elif source == "SP":
                # 标普指数：平时按 515450 累计净值日频推算；每周用 ypingcn 的
                # 官方周频点位校正——清除官方末点之后的推算日频点、只保留官方
                # 周频数据，再从最新官方点重新推算至今（漂移每周归零）。
                try:
                    official = _fetch_sp_official_weekly()
                except Exception as e:
                    official = {}
                    # 失效标记写入 HTML：前端在指数页面展示提醒（用户不看 Actions 日志也能发现）
                    err_today = now_cn().strftime("%Y-%m-%d")
                    prev_err = all_data[name].get("sp_official_error")
                    if not prev_err or prev_err.get("date") != err_today:
                        all_data[name]["sp_official_error"] = {"date": err_today, "msg": str(e)[:200]}
                        updated_any = True
                    print(f"⚠️ 标普指数 ypingcn 官方周频抓取失败（本次全部按推算处理）: {e}")
                else:
                    # 恢复后自动清除失效标记
                    if all_data[name].pop("sp_official_error", None) is not None:
                        updated_any = True
                        print("标普指数：ypingcn 官方周频校正已恢复，失效标记已清除。")
                official_end = (
                    all_data[name].get("sp_official_end")
                    or (max(dates) if dates else None)
                )
                # 清除官方数据末点之后的推算日频点
                drop = [i for i, d in enumerate(dates) if official_end and d > official_end]
                if drop:
                    keep = set(range(len(dates))) - set(drop)
                    dates = [dates[i] for i in sorted(keep)]
                    values = [values[i] for i in sorted(keep)]
                    print(f"标普指数：清除官方末点 {official_end} 之后的 {len(drop)} 个推算日频点。")
                # 官方周频点就地覆盖/追加（只处理晚于官方末点的新周点）
                if official:
                    for d, v in sorted(official.items()):
                        if d > official_end:
                            if d in dates:
                                values[dates.index(d)] = v
                            else:
                                dates.append(d)
                                values.append(v)
                    # 官方末点只增不减：即使本次无新周点也落盘，
                    # 防止个别运行抓取失败后误把官方历史日频当推算点清除
                    prev_end = all_data[name].get("sp_official_end")
                    all_data[name]["sp_official_end"] = max(
                        max(official), prev_end or "")
                    print(f"标普指数：官方周频数据校正至 {all_data[name]['sp_official_end']}。")
                all_data[name]["dates"] = dates
                all_data[name]["values"] = values
                # 从最新末点（官方周点或官方历史末点）起重新推算日频
                recent_data = _extrapolate_sp_from_fund(
                    max(dates) if dates else None, values[-1] if values else None)
            else:  # WIND
                # 万得微盘为推算口径：固定锚点（准确数据最后一天）+ 代理指数最新收盘，
                # 每次重推锚点之后的全部交易日并覆盖旧值。这样即便某次运行抓到了
                # 代理源盘后未定稿的坏值，下次运行也会自动纠正，不会持续累积误差。
                anchor = all_data[name].get("wind_anchor")
                if not anchor:
                    anchor = {"date": max(dates) if dates else None,
                              "value": values[-1] if values else None}
                    all_data[name]["wind_anchor"] = anchor
                recent_data = fetch_wind_history(code, anchor["date"], anchor["value"])
        except Exception as e:
            print(f"⚠️ {name}（{code}）历史数据抓取失败，保留本地数据: {e}")
        if not recent_data:
            # 抓取失败或数据源尚未发布最新数据：明示保留旧数据，下次运行会自动补齐
            #（增量窗口为已保存日期前 14 天，缺的日子下次成功抓取时自动回补）
            print(f"⚠️ {name}（{code}）本次未获取到任何新数据，点位仍停留在 {last_saved_date}。")
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
        # 强制检查：如果标量派生数据缺失，触发重新计算
        #（序列字段已不内嵌 HTML，无需再检查其长度）
        if (
            index_updated
            or "regression_start_index" not in all_data[name]
            or all_data[name].get("regression_window_years") != 10
            or "median_drawdown_days" not in all_data[name]
            or "median_repair_days" not in all_data[name]
            or "current_drawdown_days" not in all_data[name]
            or "current_repair_days" not in all_data[name]
        ):
            combined = sorted(zip(dates, values), key=lambda x: x[0])
            all_data[name]["dates"] = [x[0] for x in combined]
            all_data[name]["values"] = [x[1] for x in combined]
            
            print(f"正在为 {name} 重新计算所有图表指标...")
            update_index_calculations(all_data[name])
            updated_any = True

        # 万得微盘（推算）：PB 同步为西部证券周报最新准确值并重算水位
        if source == "WIND":
            if _apply_wind_pb(all_data[name]):
                updated_any = True
            if _apply_wind_crowding(all_data[name]):
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
    pepb_history = load_pepb_history(content)  # 从 HTML 内嵌块加载历史序列
    history_changed = False

    for name, blocks in fetch_index_metrics().items():
        if name not in all_data:
            continue
        block = blocks["valuation"]

        # 近10年估值分位：当前整体法 PE/PB 在内嵌历史序列中的位置
        hist = pepb_history.get(_history_sheet_name(name)) or {}
        if hist.get("dates"):
            block["pe_percentile_10y"] = percentile_in_10y(
                hist["dates"], hist["pe"], block["pe_ttm"])
            block["pb_percentile_10y"] = percentile_in_10y(
                hist["dates"], hist["pb"], block["pb"])

        # 把最新估值追加进内嵌历史序列（幂等），供后续滚动计算分位
        if block.get("pe_ttm") and block.get("pb") and block.get("data_date"):
            if append_history_entry(pepb_history, name, block["data_date"],
                                    block["pe_ttm"], block["pb"]):
                history_changed = True
                print(f"{name} 已追加 {block['data_date']} 估值至内嵌历史序列")

        previous_valuation = all_data[name].get("valuation") or {}
        # 只比较实质字段，忽略每次都会变的 updated_at，避免无意义的提交
        if (
            previous_valuation.get("pe_ttm") != block["pe_ttm"]
            or previous_valuation.get("pb") != block["pb"]
            or previous_valuation.get("dividend_yield") != block["dividend_yield"]
            or previous_valuation.get("weight_date") != block["weight_date"]
            or previous_valuation.get("source") != block["source"]
            or previous_valuation.get("data_date") != block["data_date"]
            or             previous_valuation.get("pe_percentile_10y") != block.get("pe_percentile_10y")
            or previous_valuation.get("pb_percentile_10y") != block.get("pb_percentile_10y")
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

    # A500 风格对比数据（大盘价值/成长）：独立内嵌块，随 Action 每日增量更新
    a500_js = build_a500_style_js(content)

    if updated_any or history_changed or a500_js:
        if updated_any:
            new_json = json.dumps(all_data, ensure_ascii=False)
            # 用完整匹配段替换，避免 JSON 子串与正文其他文本重复时误替换
            content = content[:match.start()] + f"let allData = {new_json};" + content[match.end():]
        if history_changed:
            hist_match = re.search(r'const pepbHistory = \{.*?\};', content, re.DOTALL)
            if hist_match:
                content = (content[:hist_match.start()] + dump_pepb_history(pepb_history)
                           + content[hist_match.end():])
            else:
                print("⚠️ HTML 中未找到 pepbHistory 块，历史序列未写回")
        if a500_js:
            a5_match = re.search(r'let a500StyleData = \{.*?\};', content, re.DOTALL)
            if a5_match:
                content = content[:a5_match.start()] + a500_js + content[a5_match.end():]
            else:
                print("⚠️ HTML 中未找到 a500StyleData 块，A500 风格数据未写回")
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
        print("🎉 所有指数及图表指标已成功同步更新。")
    else:
        print("ℹ️ 所有数据及指标已是最新。")

if __name__ == "__main__":
    update_html("指数全收益定投策略.html")

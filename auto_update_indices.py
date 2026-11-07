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
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

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
    # 中证国信价值全收益（H21052）：价格指数 931052，全收益 H21052，均走中证官网
    # 免费接口（与其他中证指数同一套更新方式），不消耗 Wind 积分。
    {"code": "H21052", "name": "国信价值全收益", "source": "CSI"},
    {"code": "480081", "name": "价值100全收益", "source": "CNI"},
    {"code": "480080", "name": "成长100全收益", "source": "CNI"},
    {"code": "H21187", "name": "中证科技100全收益", "source": "CSI"},
    # 中证2000（全收益口径 932000CNY010）：点位、成分股权重、行业分布、PE/PB/股息率
    # 全部走中证指数官网的免费接口，不消耗 Wind 积分（free_only）。
    # 仅「近10年 PE/PB 历史序列」（用于估值分位）中证官网不提供，首次嵌入时用
    # Wind 一次性补 2023-08 上市以来的日频序列，之后每天自算追加，不再调用 Wind。
    {"code": "932000CNY010", "name": "中证2000全收益", "source": "CSI", "free_only": True},
    {"code": "868008.WI", "name": "万得微盘股指数", "source": "WIND"},
    # 标普中国A股大盘红利低波50指数（总收益）：官网有 Akamai 防护无法程序化抓取。
    # 平时按跟踪 ETF（515450）累计净值的日频涨跌幅推算；ypingcn.com 内嵌与官网
    # 一致的周频官方点位，每周自动校正一次（清除官方末点之后的推算日频点，
    # 只保留官方周频数据），保证推算漂移每周归零。
    {"code": "SPCNALV50TR", "name": "标普红利低波50全收益", "source": "SP"},
    # 纳斯达克100指数（价格收益 NDX）：美股指数，数据源为腾讯行情日 K。
    # 注意为价格指数口径（不含股息再投资），与 A 股全收益指数口径不同。
    {"code": "usNDX", "name": "纳斯达克100指数", "source": "US"},
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
    "国信价值全收益": ("931052", "CSI"),
    "价值100全收益": ("480081", "CNI"),
    "成长100全收益": ("480080", "CNI"),
    "中证科技100全收益": ("931187", "CSI"),
    "中证2000全收益": ("932000", "CSI"),
    # 标普官方不公开指数权重文件（需 SPICE 账号）：成份股/权重/总市值/申万行业
    # 一律走 Wind（见 WIND_COMPONENT_INDICES），按 ISO 周缓存、每周只刷一次省积分；
    # 515450 全持仓仅作 Wind 失败时的回退（滞后一个报告期，且是占净值比）。
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
# 腾讯行情日 K 接口（proxy 域名，web.ifzq.gtimg.cn 主站有 WAF 校验）：
# 用于美股指数（如纳斯达克100）的日线抓取，返回 [[date, open, close, high, low, volume], ...]。
US_KLINE_URL = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
US_KLINE_HEADERS = {
    "User-Agent": TENCENT_HEADERS["User-Agent"],
    "Referer": "https://gu.qq.com/",
}

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
    "国信价值全收益": "931052.CSI",
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
            # 403/429 多为中证官网的反爬限流，退避后重试通常能过
            if response.status_code in (403, 429):
                time.sleep(sleep_seconds * (attempt + 2))
                continue
            # 4xx 属于客户端错误，重试通常无效，直接失败
            if 400 <= response.status_code < 500:
                break
        except requests.RequestException as e:
            last_error = str(e)
        if attempt < tries - 1:
            time.sleep(sleep_seconds)
    raise requests.RequestException(last_error or "请求失败")


# ===== Wind Alice 官方数据通道（MCP over HTTP）=====
# 用 API Key 直连 Wind 数据服务，取官方日频点位、加权 PB、成份股总市值中位数。
# Key 只从环境变量读取：本地 export WIND_API_KEY=...，GitHub Actions 用仓库 Secret。
# 该通道是「准确数据」的首选源；任何一步失败都自动回退到原有的推算口径，不会中断更新。
WIND_MCP_URL = "https://mcp.wind.com.cn/vserver_wind_tools/mcp/"
WIND_API_KEY = os.environ.get("WIND_API_KEY", "").strip()
# 标普中国A股大盘红利低波50 全收益指数（Wind 代码）：官方日频，替代 515450 净值推算
SP_WINDCODE = "SPCLLHCT.SPI"
# 各指数的 Wind 官方代码：日频点位、加权估值走这条通道（ETF 行情不在此列，用新浪免费接口）。
# 价值100 / 成长100 的 Wind 代码带 R 后缀（全收益），即 480081.CNI / 480080.CNI；
# 98 开头（980081/980080）是同名的价格指数，口径不同，不可混用。
WIND_CODES = {
    "中证红利全收益": "H00922.CSI",
    "智选高股息全收益": "932305CNY010.CSI",
    "红利低波全收益": "H20269.CSI",
    "红利低波100全收益": "H20955.CSI",
    "A500红利低波全收益": "932422CNY010.CSI",
    "国信价值全收益": "H21052.CSI",
    "价值100全收益": "480081.CNI",
    "成长100全收益": "480080.CNI",
    "中证科技100全收益": "H21187.CSI",
    # 中证2000：日常「官方加权 PB」与「首次补齐历史 PE/PB 序列」都用这个代码。
    # 这里必须用价格指数 932000.CSI，不能用全收益 932000CNY010.CSI ——
    # Wind 对全收益代码只给 PB、PE 返回空，补历史时会丢一半数据。
    "中证2000全收益": "932000.CSI",
    "万得微盘股指数": "868008.WI",
    "标普红利低波50全收益": "SPCLLHCT.SPI",
    "纳斯达克100指数": "NDX.GI",
    "沪深300全收益": "H00300.CSI",
}
# 走免费数据源的指数（INDEX_CONFIG 里标了 free_only 的）：点位、估值、成分股、行业
# 全部用中证官网免费接口，一律不调用 Wind，省积分。
FREE_ONLY_INDICES = {item["name"] for item in INDEX_CONFIG if item.get("free_only")}
# 这些指数的 PE 用「剔除亏损股」口径（与中证官网一致，避免 2000 只小盘股里
# 600 只亏损股的负盈利把整体法 PE 拉到 100 倍以上），股息率分母用全部权重。
PE_EXCLUDE_LOSSES = {"中证2000全收益"}
DY_TOTAL_WEIGHT = {"中证2000全收益"}

# ===== 估值水位表的指标口径（同时决定要花多少 Wind 积分）=====
# 页面列：PB｜PB 10年分位｜加权股息率｜成分股｜更新日期。各指数「只算、只存」自己
# 需要的那几列，不需要的指标不取数、不写进 HTML（页面单元格显示「—」占位）：
#   PB_ONLY_INDICES  成长/科技/中证2000 —— 只算 PB（PE 仅内部用来续算分位历史）
#   DY_ONLY_INDICES  红利/高股息类 + 价值100 / 国信价值 —— 只算加权股息率，PE/PB 彻底移除
#   纳斯达克100指数   —— 不展示任何估值，且不在 VALUATION_CONFIG 中，不产生请求
#   万得微盘股指数    —— 不在 VALUATION_CONFIG，其 PB 由 _apply_micro_daily 单独处理
PB_ONLY_INDICES = {
    "成长100全收益", "中证科技100全收益", "中证2000全收益",
}
DY_ONLY_INDICES = {
    "中证红利全收益", "智选高股息全收益", "红利低波全收益",
    "红利低波100全收益", "A500红利低波全收益", "标普红利低波50全收益",
    "价值100全收益", "国信价值全收益",
}
# 仍需向 Wind 取「官方加权 PB」的指数（其余指数不再调用 Wind 估值接口）：
#   成长100R / 中证科技100 —— 整块估值都取 Wind 官方加权；
#   中证2000 —— 点位与成份股仍走中证官网免费接口，只有 PB 直接用 Wind 官方值，
#               不再按成份股自算（见 FREE_ONLY_PB_OVERRIDE）。
#   万得微盘股指数 —— 不在本循环，其 PB 由 _apply_micro_daily 单独取 Wind。
WIND_PB_INDICES = {
    "成长100全收益", "中证科技100全收益", "中证2000全收益",
}
# 「免费源算 PE/股息率 + Wind 覆盖 PB」的指数：中证2000 属于此类
FREE_ONLY_PB_OVERRIDE = {n for n in WIND_PB_INDICES if n in FREE_ONLY_INDICES}
# 成份股/行业仍取 Wind 的指数：标普的 515450 持仓是滞后一期披露，
# 用 Wind 的实时成份股更准（其余指数一律走中证/国证官网免费权重文件）
WIND_COMPONENT_INDICES = {"标普红利低波50全收益"}


def valuation_metrics(name):
    """该指数在估值表里需要「计算并保存」的指标集合：{'pb'} / {'dy'} / 空集。

    与页面端 VALUATION_FIELDS 一一对应，两边要一起改。
    """
    if name in PB_ONLY_INDICES:
        return {"pb"}
    if name in DY_ONLY_INDICES:
        return {"dy"}
    return set()

# ===== Wind 调用的重试与熔断策略 =====
# Wind MCP 网关偶发超时、返回空响应、SSE 被截断都很常见，绝大多数重试一次就能拿到。
# 直接回退的话，那项数据就停在上一交易日/免费源，只能靠人工重跑一次 Action 来补，
# 代价远高于在本地多重试几次。
WIND_MAX_ATTEMPTS = 3           # 瞬时故障（5xx/429/空响应/SSE 截断）的总尝试次数
WIND_TIMEOUT_ATTEMPTS = 2       # 超时类只多试 1 次：每次都要等满 timeout，成本太高
WIND_RETRY_DELAYS = (3, 8)      # 第 1、2 次重试前的等待秒数（指数退避，避免打到同一个坏节点）
WIND_CONNECT_TIMEOUT = 15       # 连接超时单独设短：网络不通时快速失败，而不是干等 180s
WIND_MAX_CONSECUTIVE_FAILS = 2  # 连续几个调用彻底失败即熔断当轮 Wind，防止网关宕机拖死流程
WIND_FAIL_TIME_BUDGET = 240     # 本轮因失败累计耗时上限（秒），超过即熔断

# 本次运行内 Wind 通道是否已被判定不可用（鉴权失败/额度不足/连续失败等），避免每个指数重复试错
_WIND_STATE = {"ok": True, "reason": "", "consecutive_fail": 0, "failed_seconds": 0.0}
# 本次运行的 Wind 调用计数（工具名 -> 次数），结束时打印，便于核对积分消耗
_WIND_USAGE = {}
# 本轮取数失败清单：{工具｜参数: (原因, 类型)}。类型 retry = 瞬时故障（值得自动补抓一轮），
# fatal = 参数类错误（补抓也没用，只为提示）
_WIND_FAILURES = {}

# 自动补抓用的环境变量：子进程里置 1，保证「整轮补抓」最多只做一次，不会无限递归
WIND_AUTO_RETRY_ENV = "WIND_AUTO_RETRY_ROUND"
WIND_AUTO_RETRY_DELAY = 30      # 补抓前等待秒数，给网关留出恢复时间


def _record_wind_failure(tool_name, params, reason, kind="retry"):
    """登记一处取数失败（同一请求只记一次），供结束时汇总与自动补抓判断。"""
    key = f"{tool_name}｜{json.dumps(params, ensure_ascii=False)[:90]}"
    _WIND_FAILURES.setdefault(key, (str(reason)[:140], kind))


def wind_has_retryable_failure():
    """是否有值得再跑一轮的失败：只认瞬时故障，参数类错误补也没用。"""
    return any(kind == "retry" for _reason, kind in _WIND_FAILURES.values())


def wind_failures_report():
    """汇总本轮未取到的 Wind 数据：打印清单，并在 Actions 里打一条黄色告警。"""
    if not _WIND_FAILURES:
        return
    print(f"\n⚠️ 本轮有 {len(_WIND_FAILURES)} 处 Wind 取数未成功，"
          f"对应数据已回退到免费源或上一交易日：")
    for key, (reason, _kind) in _WIND_FAILURES.items():
        print(f"   • {key}｜{reason}")
    print("   这些数据下次运行会自动补齐；若想立刻补齐，再点一次 Run workflow 即可。")
    # 这一行会被 GitHub Actions 渲染成黄色告警，比在一大堆日志里翻更醒目
    print(f"::warning::Wind 取数有 {len(_WIND_FAILURES)} 处失败，"
          f"部分估值/点位已回退，建议再运行一次补齐")


def wind_usage_report():
    """打印本次运行的 Wind 调用汇总（次数），方便对照账户积分明细。"""
    if not _WIND_USAGE:
        return
    total = sum(_WIND_USAGE.values())
    detail = "、".join(f"{k} × {v}" for k, v in
                      sorted(_WIND_USAGE.items(), key=lambda kv: -kv[1]))
    print(f"📊 本次运行 Wind 调用合计 {total} 次：{detail}")


def wind_configured():
    """是否配置了 API Key 且通道未被判定失效。"""
    return bool(WIND_API_KEY) and _WIND_STATE["ok"]


def _parse_wind_response(resp):
    """解析 Wind MCP 网关响应，并判断这次失败值不值得重试。

    返回 (status, text)：
      ok     —— 成功（text 为工具输出，允许空字符串：该区间本来就没有数据）
      retry  —— 瞬时故障：5xx/429/空响应/SSE 被截断/限流，重试通常即可成功
      auth   —— 鉴权失败，整轮停用 Wind
      quota  —— 积分或额度不足，整轮停用 Wind
      fatal  —— 其它业务错误（参数不对等），重试无意义
    """
    if resp.status_code != 200:
        if resp.status_code in (401, 403):
            return "auth", f"HTTP {resp.status_code}"
        if resp.status_code == 402:
            return "quota", "HTTP 402（积分不足）"
        if resp.status_code >= 500 or resp.status_code == 429:
            return "retry", f"HTTP {resp.status_code}"
        return "fatal", f"HTTP {resp.status_code}"

    # 服务端返回 SSE（text/event-stream），requests 对这类响应默认按
    # Latin-1 解码，中文会变乱码，必须强制 UTF-8。
    resp.encoding = "utf-8"
    body = resp.text or ""
    if not body.strip():
        return "retry", "返回空响应"

    msg = None
    # 正常取数走 SSE；出错时（如积分余额不足）服务端直接回普通 JSON，两种格式都要能解析，
    # 否则真实原因会被「空响应」掩盖。
    if body.lstrip().startswith("{"):
        try:
            msg = json.loads(body)
        except json.JSONDecodeError:
            msg = None
    if msg is None:
        # 响应体较大时单个 JSON 会被拆成多条 data: 行，按 SSE 规范用换行拼接后再解析。
        events, cur = [], []
        for ln in body.splitlines():
            if ln.startswith("data:"):
                cur.append(ln[5:].lstrip())
            elif not ln.strip() and cur:
                events.append(cur)
                cur = []
        if cur:
            events.append(cur)
        if not events:
            return "retry", "返回空响应"
        for chunk in ("\n".join(events[0]), "".join(events[0])):
            try:
                msg = json.loads(chunk)
                break
            except json.JSONDecodeError:
                continue
    if msg is None:
        # SSE 被截断或拼接失败：重试一次基本就能恢复
        return "retry", "响应内容无法解析为 JSON"

    if msg.get("error"):
        err = msg["error"]
        text = (json.dumps(err, ensure_ascii=False) if isinstance(err, dict)
                else str(err))[:200]
        low = text.lower()
        if any(k in low for k in ("rate limit", "too many", "限流", "频率", "busy", "timeout")):
            return "retry", text
        return "fatal", text

    result = msg.get("result") or {}
    content = result.get("content") or [{}]
    text = (content[0] or {}).get("text", "") or ""
    if result.get("isError"):
        # 积分余额不足 / 额度用尽等账户级问题：本次运行整体停用 Wind 通道
        code = (result.get("structuredContent") or {}).get("code")
        if code == 402 or any(k in text for k in ("余额不足", "积分", "额度", "余额")):
            return "quota", text[:200]
        return "fatal", text[:200] or "Wind 工具返回错误"
    return "ok", text


def _wind_call(tool_name, params, timeout=120):
    """调用 Wind MCP 网关上的数据工具，返回其文本结果；失败抛异常。

    瞬时故障（超时、5xx、429、空响应、SSE 截断）按指数退避重试：
    这类抖动多半重试一次就能拿到，比直接回退、再人工重跑一次 Action 划算得多。
    鉴权失败与积分不足不重试，直接熔断当轮 Wind 通道。
    """
    if not WIND_API_KEY:
        raise RuntimeError("未配置 WIND_API_KEY")
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "market_call_tool",
                          "arguments": {"toolName": tool_name,
                                        "arguments": json.dumps(params, ensure_ascii=False)}}}
    headers = {"Authorization": "Bearer " + WIND_API_KEY,
               "Accept": "application/json, text/event-stream"}

    last_error = "未知错误"
    attempt = 0
    max_attempts = WIND_MAX_ATTEMPTS
    while attempt < max_attempts:
        attempt += 1
        started = time.monotonic()
        try:
            resp = requests.post(WIND_MCP_URL, json=payload, headers=headers,
                                 timeout=(WIND_CONNECT_TIMEOUT, timeout))
            status, text = _parse_wind_response(resp)
        except requests.Timeout:
            # 超时要等满 timeout 才返回，重试成本高，只多试一次
            status, text = "retry", f"请求超时（等待 {timeout}s 无响应）"
            max_attempts = min(max_attempts, WIND_TIMEOUT_ATTEMPTS)
        except requests.RequestException as e:
            # 连接被拒 / DNS 失败等，通常很快返回，按普通瞬时故障处理
            status, text = "retry", f"{type(e).__name__}: {str(e)[:120]}"

        if status == "ok":
            _WIND_STATE["consecutive_fail"] = 0
            _WIND_USAGE[tool_name] = _WIND_USAGE.get(tool_name, 0) + 1
            return text

        if status == "auth":
            _WIND_STATE.update(ok=False, reason=f"鉴权失败（{text}）")
            raise RuntimeError(f"Wind 鉴权失败（{text}），本次改用免费/推算口径")

        if status == "quota":
            _WIND_STATE.update(ok=False, reason=f"积分不足（{text[:80]}）")
            raise RuntimeError(
                f"Wind 积分余额不足，本次改用其他数据源（官网/推算）：{text[:120]}")

        if status == "fatal":
            # 参数类错误重试也没用；只影响这一项，不熔断整轮，也不触发自动补抓
            _record_wind_failure(tool_name, params, text, kind="fatal")
            raise RuntimeError(text)

        # status == "retry"
        last_error = text
        _WIND_STATE["failed_seconds"] += time.monotonic() - started
        if attempt < max_attempts:
            delay = WIND_RETRY_DELAYS[min(attempt - 1, len(WIND_RETRY_DELAYS) - 1)]
            print(f"   ↻ Wind {tool_name} 第 {attempt} 次尝试失败（{text}），{delay}s 后重试")
            time.sleep(delay)

    # 重试耗尽：登记失败清单，并在连续失败/超时预算耗尽时熔断当轮 Wind
    _WIND_STATE["consecutive_fail"] += 1
    _record_wind_failure(tool_name, params, last_error)
    if (_WIND_STATE["consecutive_fail"] >= WIND_MAX_CONSECUTIVE_FAILS
            or _WIND_STATE["failed_seconds"] >= WIND_FAIL_TIME_BUDGET):
        _WIND_STATE.update(
            ok=False,
            reason=f"连续 {_WIND_STATE['consecutive_fail']} 次失败（{last_error[:60]}）")
        print(f"⚠️ Wind 通道连续失败 {_WIND_STATE['consecutive_fail']} 次"
              f"（累计耗时 {_WIND_STATE['failed_seconds']:.0f}s），"
              f"本轮剩余数据改用免费源/推算，不再继续空等。")
    raise requests.RequestException(f"Wind 调用失败（{tool_name}）：{last_error}")


def wind_index_kline(windcode, begin_date, end_date):
    """取指数日频收盘点位，返回 {YYYY-MM-DD: 收盘}；无数据返回空 dict。"""
    text = _wind_call("get_index_kline", {
        "windcode": windcode, "begin_date": begin_date,
        "end_date": end_date, "period": "10", "count": 0,
    })
    doc = json.loads(text)
    block = doc.get("data") or {}
    if isinstance(block, dict) and isinstance(block.get("data"), dict):
        block = block["data"]
    rows = (block or {}).get("rows") or []
    cols = [c.get("name") for c in (block or {}).get("columns") or []]
    i_time = cols.index("TIME") if "TIME" in cols else 0
    i_close = cols.index("MATCH") if "MATCH" in cols else 2
    out = {}
    for row in rows:
        try:
            d = str(row[i_time])[:10]
            v = float(row[i_close])
        except (IndexError, TypeError, ValueError):
            continue
        if v > 0:
            out[d] = v
    return out


def _norm_wind_date(raw):
    """把 Wind 的 20261105 / 2026-11-05T15:00:02 统一成 YYYY-MM-DD。"""
    s = str(raw).strip()
    m = re.match(r"^(\d{4})-?(\d{2})-?(\d{2})", s)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def wind_index_pb_series(windcode="868008.WI", begin_date=None, end_date=None):
    """取指数日频 PB 序列，返回 {YYYY-MM-DD: PB}。

    逐日序列是各交易日收盘后重算值，与指数点位同步变动；而「某日市净率」
    的单日快照查询可能返回滞后一日的值（实测 2026-11-05 快照给 2.0287，
    序列给 2.0873，后者才与当日 +2.93% 的涨幅吻合），因此 PB 一律以本函数为准。
    单次最多返回 100 个交易日，长区间需由调用方分段。
    """
    question = (f"请逐行列出万得微盘股指数({windcode})在{begin_date}至{end_date}"
                f"期间每个交易日的市净率PB，按日期升序，不要汇总")
    doc = json.loads(_wind_call("get_index_fundamentals", {"question": question}))
    blocks = (doc.get("data") or {}).get("data") or []
    out = {}
    for blk in blocks:
        names = [c.get("name", "") for c in blk.get("columns") or []]
        for row in blk.get("rows") or []:
            d, pb = None, None
            for name, val in zip(names, row):
                if name == "日期" and val:
                    d = _norm_wind_date(val)
                elif "市净率" in name and isinstance(val, (int, float)):
                    pb = float(val)
            if d and pb and pb > 0:
                out[d] = round(pb, 4)
    return out


def wind_index_valuation_full(name, trade_date=None):
    """取指数官方加权 PE / PB / 股息率，返回 {"pe","pb","dy","date"}。

    Wind 的 get_index_fundamentals 按成份股加权聚合返回一行指数数据；当指数
    层面缺某项指标时（例如成长100 没有股息率），结果会下钻成个股明细，因此
    解析时只认「首列等于目标 Wind 代码」的那一行，避免把个股值当成指数值。
    """
    code = WIND_CODES.get(name)
    if not code:
        return None
    when = trade_date or "最新交易日"
    text = _wind_call("get_index_fundamentals", {
        "question": f"查询{code}在{when}的市盈率PE、市净率PB、股息率"}, timeout=150)
    blocks = (json.loads(text).get("data") or {}).get("data") or []
    out = {"pe": None, "pb": None, "dy": None, "date": None}
    for blk in blocks:
        names = [c.get("name", "") for c in blk.get("columns") or []]
        for row in blk.get("rows") or []:
            if not row or str(row[0]).strip().upper() != code.upper():
                continue
            for col, val in zip(names, row):
                try:
                    fv = float(val)
                except (TypeError, ValueError):
                    fv = None
                if fv:  # 0 视为缺失（如标普指数无 PE 时 Wind 返回 0）
                    if "市盈率" in col and out["pe"] is None:
                        out["pe"] = fv
                    elif "市净率" in col and out["pb"] is None:
                        out["pb"] = fv
                    elif "股息率" in col and out["dy"] is None:
                        out["dy"] = fv
                m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", col)
                if m and not out["date"]:
                    out["date"] = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    if out["pb"] is None and out["pe"] is None:
        return None
    return out


def wind_index_pepb_series(windcode, begin_date, end_date, page_rows=100,
                           max_calls=30, freq="每个交易日"):
    """取指数 PE / PB 序列，返回 ({date: pe}, {date: pb})。

    与 wind_index_pb_series 同源，但一次问答同时取 PE 与 PB；接口单次最多返回
    100 行，因此按日期窗口滚动翻页。仅用于「第一次把某个指数纳入工具时」补齐
    历史估值序列（中证2000 之类官网不提供历史 PE/PB 的指数），日常更新不调用。

    PE 必须写明「市盈率PE_TTM_剔除亏损股」这一固定问法：只写「市盈率PE」时，
    Wind 返回的是含亏损股负盈利的整体法（中证2000 会到 127 倍），与官网口径
    （约 37 倍）差 3 倍多，也会让估值分位完全失真。
    """
    pe_map, pb_map = {}, {}
    cursor = begin_date
    for _ in range(max_calls):
        question = (f"请逐行列出指数({windcode})在{cursor}至{end_date}期间{freq}的"
                    f"市盈率PE_TTM_剔除亏损股、市净率PB，按日期升序，不要汇总")
        doc_text = _wind_call("get_index_fundamentals", {"question": question},
                              timeout=180)
        try:
            doc = json.loads(doc_text)
        except ValueError:
            # 区间内没有数据时 Wind 直接回一句「没找到数据」，按空结果处理
            print(f"Wind 历史估值：{doc_text[:40] or '空响应'}（{cursor} 起），停止翻页")
            break
        blocks = (doc.get("data") or {}).get("data") or []
        got = 0
        newest = None
        for blk in blocks:
            names = [c.get("name", "") for c in blk.get("columns") or []]
            for row in blk.get("rows") or []:
                d, pe, pb = None, None, None
                for name, val in zip(names, row):
                    if name == "日期" and val:
                        d = _norm_wind_date(val)
                    elif "市盈率" in name and isinstance(val, (int, float)):
                        pe = float(val)
                    elif "市净率" in name and isinstance(val, (int, float)):
                        pb = float(val)
                if not d:
                    continue
                got += 1
                newest = max(newest or d, d)
                if pe and pe > 0:
                    pe_map[d] = round(pe, 4)
                if pb and pb > 0:
                    pb_map[d] = round(pb, 4)
        if got < page_rows or not newest or newest >= end_date:
            break
        # 下一页从"最新一天"继续（含当天，接口按自然日截断即可，重复点会被覆盖）
        cursor = newest
    return pe_map, pb_map


def seed_pepb_history_from_wind(name, begin_date="2015-01-01"):
    """一次性用 Wind 补齐某指数的历史 PE/PB 序列（仅首次纳入工具时调用）。

    中证官网只提供最近 20 个交易日的市盈率/股息率文件，没有长历史 PE/PB，
    因此「近10年估值分位」必须借助 Wind 补一段序列。补齐一次即可：之后每天由
    脚本按中证官网免费权重文件 + 个股行情自算最新值并追加，不再调用 Wind。
    """
    code = WIND_CODES.get(name)
    if not code:
        print(f"⚠️ {name}：未配置 Wind 代码，跳过历史估值补齐")
        return None
    if not wind_configured():
        print(f"⚠️ {name}：Wind 通道不可用（未配置密钥或额度不足），估值分位暂时留空")
        return None
    end = now_cn().strftime("%Y-%m-%d")
    try:
        # 按「日频」补历史：微盘拥挤度用「微盘 PB / 中证2000 PB」比值，需要与微盘的
        # pb_daily（日频）逐日对齐，周频样本会让比值曲线前段稀疏、分位失真。
        # 代价是一次性约 8 次调用（3 年多约 780 个交易日，接口每页 100 行），
        # 之后每天由中证官网数据追加，不再调用 Wind。
        pe_map, pb_map = wind_index_pepb_series(code, begin_date, end,
                                                freq="每个交易日")
    except Exception as e:
        print(f"⚠️ {name}：Wind 历史 PE/PB 补齐失败（{str(e)[:70]}），估值分位暂时留空，下次运行会重试")
        return None
    dates = sorted(set(pe_map) & set(pb_map))
    if len(dates) < 100:
        print(f"⚠️ {name}：Wind 只返回 {len(dates)} 个交易日的历史 PE/PB，估值分位暂不计算")
        return None
    print(f"{name}：Wind 一次性补齐 {len(dates)} 个日频历史点（PE/PB）"
          f"（{dates[0]} ~ {dates[-1]}），此后按中证官网免费数据每日追加，不再调用 Wind")
    return {"dates": dates,
            "pe": [pe_map[d] for d in dates],
            "pb": [pb_map[d] for d in dates]}


def wind_index_valuation(windcode="868008.WI", trade_date=None):
    """取指数加权 PB、PE 与成份股总市值中位数。

    返回 {"pb","pe","mcap","date"}；不传 trade_date 时取最新交易日。
    解析不到 PB 时返回 None，由调用方回退到自算口径。

    注意：该接口按自然语言解析取数口径，问法必须固定。实测「加权市净率」
    会被解析成另一套口径（返回 5.07，与万得终端的 2.03 不符），
    因此这里固定用「市净率PB、市盈率PE、成份股总市值中位数」的模板问法。
    """
    if trade_date:
        y, m, d = trade_date.split("-")
        when = f"{int(y)}年{int(m)}月{int(d)}日"
    else:
        when = "最新交易日"
    question = (f"查询万得微盘股指数({windcode}){when}的市净率PB、市盈率PE，"
                f"以及成份股总市值中位数")
    doc = json.loads(_wind_call("get_index_fundamentals", {"question": question}))
    blocks = (doc.get("data") or {}).get("data") or []
    pb = pe = mcap = None
    data_date = None
    for blk in blocks:
        names = [c.get("name", "") for c in blk.get("columns") or []]
        for row in blk.get("rows") or []:
            for name, val in zip(names, row):
                # 「交易时间」形如 20261105 15:00:02，比「日期」列更常返回，优先采信
                if "交易时间" in name and val:
                    data_date = _norm_wind_date(val) or data_date
                elif name == "日期" and val:
                    data_date = _norm_wind_date(val) or data_date
                elif "市值中位数" in name and isinstance(val, (int, float)):
                    mcap = float(val)
                elif "市净率" in name and isinstance(val, (int, float)):
                    pb = float(val)
                elif "市盈率" in name and isinstance(val, (int, float)):
                    pe = float(val)
    if pb is None:
        return None
    # PB 改用逐日序列取当日收盘值：单日快照查询会滞后一日（见 wind_index_pb_series）
    if trade_date:
        try:
            begin = (datetime.strptime(trade_date, "%Y-%m-%d")
                     - timedelta(days=12)).strftime("%Y-%m-%d")
            series = wind_index_pb_series(windcode, begin, trade_date)
            if series.get(trade_date):
                pb = series[trade_date]
        except Exception as e:
            print(f"微盘估值：PB 序列查询失败，沿用快照值 {pb}: {e}")
    return {"pb": round(pb, 4), "pe": round(pe, 4) if pe is not None else None,
            "mcap": round(mcap, 4) if mcap is not None else None, "date": data_date}


def fetch_risk_free_rate():
    """抓取中债国债收益率曲线最新的 10 年期到期收益率（%）。

    只用中国债券信息网（yield.chinabond.com.cn）的免费收益率曲线接口，
    不再调用 Wind EDB（省积分：该值与 Wind 完全同源，无需付费）。
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
    """一次性批量抓取全部 ETF 行情（纯免费源，不消耗 Wind 积分）。

    用新浪批量接口：一次请求即可拿到全部代码的收盘价与前收盘
    （逐只请求极易触发 Forbidden 限流）。个别缺失的在 fetch_etf_quote
    里再按「东财 → 新浪单只」兜底。
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
    基金成立日永不改变、年度涨幅一年才变一次，都做缓存以省请求；
    阶段涨幅是滚动区间、每日随净值变化，保持每次抓取。
    """
    code = etf_meta["code"]
    prev = data_obj.get("tracking") or {}
    index_returns = compute_index_returns(data_obj)
    fund_periods = fetch_fund_period_returns(code)

    # 成立日永不变：已有则直接复用，省 1 次请求
    inception = prev.get("inception") or fetch_fund_inception(code)

    # 年度涨幅一年才变一次：同一年内复用上次结果（缓存键 = 刷新年份）
    cur_year = now_cn().year
    if prev.get("years_year") == cur_year and prev.get("years"):
        fund_years = {str(item["year"]): item["fund"] for item in prev["years"]
                      if item.get("fund") is not None}
    else:
        fund_years = fetch_fund_year_returns(code)

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
        "years_year": cur_year,
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

def fetch_us_history(code, last_saved_date=None):
    """抓取美股指数（如纳斯达克100）的最近交易日收盘点位。

    数据源：腾讯行情日 K 接口（proxy.finance.qq.com 的 newfqkline 接口）。
    日 K 返回 [[date, open, close, high, low, volume, ...], ...]，收盘价在 index 2。
    拉取最近 60 个交易日，既能补齐节假日/漏跑，又不会用远端数据覆盖网页历史序列。
    """
    try:
        param = f"{code},day,,,60,qfq"
        r = _request_with_retry(US_KLINE_URL, params={"param": param},
                                headers=US_KLINE_HEADERS, timeout=20)
        payload = r.json()
        data = payload.get("data")
        if not isinstance(data, dict):
            return {}
        rows = (data.get(code) or {}).get("day") or []
        result = {}
        for row in rows:
            if len(row) >= 3 and row[0] and row[2] is not None:
                result[str(row[0])] = float(row[2])
        # 丢弃当日尚未收盘定稿的 K 线：美股常规收盘 16:00 ET（夏令时=北京时间次日
        # 04:00、冬令时=次日 05:00），统一以次日 05:30 为安全阈值——早于该时刻的
        # 当日 K 线仍是盘中价，不能当作收盘价写进网页。
        if result:
            last_date = max(result)
            try:
                d = datetime.strptime(last_date, "%Y-%m-%d").date()
                final_at = datetime(d.year, d.month, d.day, 5, 30, 0, tzinfo=CN_TZ) + timedelta(days=1)
                if now_cn() < final_at:
                    result.pop(last_date, None)
                    print(f"美股指数 {code}：{last_date} 当日 K 线尚未收盘定稿，暂不写入。")
            except (TypeError, ValueError):
                pass
        if result:
            latest = max(result)
            print(f"美股指数 {code}：获取 {len(result)} 条数据，最新 {latest} / {result[latest]:.2f}")
        return result
    except Exception as e:
        print(f"抓取美股指数 {code} 历史行情失败: {e}")
    return {}

# A500 风格对比（价值&成长仪表盘「大盘风格」视图）：A500价值全收益 / A500成长全收益。
# 两者均走中证官网全收益代码（CNY010 后缀 = 全收益；CNY020 = 净收益，勿混用）：
#   932497CNY010 = 中证A500价值全收益
#   932494CNY010 = 中证A500成长全收益
# 该数据独立内嵌为 a500StyleData 块（不进入 allData，避免污染估值仪表盘/指数选择器）。
A500_VALUE_CODE = "932497CNY010"
A500_GROWTH_CODE = "932494CNY010"
# 对应的 Wind 代码（A500 价值 / 成长全收益）
A500_VALUE_WIND = "932497CNY010.CSI"
A500_GROWTH_WIND = "932494CNY010.CSI"


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

    # 中证官网免费接口即可拿到全收益日频，不调用 Wind（省积分）
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
#      TR 日收益相关系数 0.981、日均绝对误差约 0.08 个百分点（与微盘的
#      东财代理同量级）；误差随交易日累积（累计净值未按再投资复利计息）。
#   2) 每周：ypingcn.com 内嵌与 S&P 官网一致的周频官方点位，自动校正一次——
#      清除官方末点之后（含同日）的推算日频点、只保留官方周频数据，再从最新
#      官方点重新推算至今，使推算漂移每周归零。官方值优先级恒高于推算值：
#      与官方末点同日的推算值也会被官方点覆盖（可从异常状态自愈）；
#      只要有官方校正发生，本次运行必定写回并重算指标。
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

    失败兜底与诊断：
    - 两组请求头依次尝试（部分 WAF 会拦截缺少 Accept 头的请求）；
    - 解析失败时把 HTTP 状态码与页面文本片段写进异常消息，
      便于在 Actions 日志/前端警告里直接看到 ypingcn 到底返回了什么
      （GitHub Actions 数据中心 IP 曾被该站间歇性 403 拦截）。
    """
    header_sets = [
        {"User-Agent": TENCENT_HEADERS["User-Agent"]},
        {   # 完整浏览器头兜底
            "User-Agent": TENCENT_HEADERS["User-Agent"],
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
        },
    ]
    r, last_err = None, None
    for hs in header_sets:
        try:
            r = _request_with_retry(YPINGCN_URL, timeout=25, headers=hs)
            break
        except Exception as e:
            last_err = e
    if r is None:
        raise last_err

    def _diag():
        # 诊断信息：状态码 + 去标签后的页面文本前 150 字
        snippet = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", r.text)).strip()
        return f"HTTP {r.status_code}，页面片段: {snippet[:150]!r}"

    chunks = re.findall(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', r.text)
    if not chunks:
        raise ValueError("ypingcn 页面未找到 RSC payload（" + _diag() + "）")
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
        raise ValueError("ypingcn chart_series 解析失败（dates/prices 缺失或不等长；" + _diag() + "）")
    return {d: float(v) for d, v in zip(dates, prices)}


def fetch_sp_weights():
    """从天天基金 jjcc 接口解析跟踪 ETF（515450）最新一期「全持仓」明细。

    **仅在 Wind 取不到标普指数权重时作回退**（Wind 正常时这条路径不会执行）：
    标普道琼斯不公开权重文件，515450 的中报/年报全持仓是唯一免费近似，
    但它滞后一个报告期，且披露的是「占基金净值比」而非指数权重。

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
    """获取微盘股指数（868008.WI）的最新收盘。

    首选 Wind 官方日频 API（准确值，无推算误差）：从本地末点回退 5 天起取数，
    返回的点会就地覆盖已有值，因此上一次运行若留下推算值，这次能用官方值自愈。
    Wind 通道不可用（未配置 Key、鉴权失败、额度不足、网络异常）时，自动回退到
    代理指数倒推：以 2026-11-05（万得官方数据最后一天）为固定锚点，按同花顺
    「微盘股」板块指数（883418）涨跌幅外推，同花顺异常时再退到东方财富
    90.BK1158 兜底。倒推误差随交易日累积，属"大致推算"口径。
    """
    try:
        if wind_configured() and last_saved_date:
            begin = (datetime.strptime(last_saved_date, "%Y-%m-%d")
                     - timedelta(days=5)).strftime("%Y-%m-%d")
            data = wind_index_kline(code, begin, now_cn().strftime("%Y-%m-%d"))
            fresh = {d: round(v, 2) for d, v in data.items() if d >= last_saved_date}
            if fresh:
                latest = max(fresh)
                print(f"微盘股指数{code}：Wind 官方日频取到 {len(fresh)} 个交易日，最新 {latest} / {fresh[latest]:.2f}")
                return fresh
            print(f"微盘股指数{code}：Wind 官方接口无 {last_saved_date} 之后的新数据。")
    except Exception as e:
        print(f"⚠️ 微盘股指数{code} Wind 官方接口失败，回退推算口径: {e}")

    result = {}
    if last_saved_value is not None:
        result = _extrapolate_wind_from_ths(last_saved_date, last_saved_value)
        if not result:
            result = _extrapolate_wind_from_bk1158(last_saved_date, last_saved_value)
    if result:
        latest = max(result)
        print(f"微盘股指数{code}：本次推算 {len(result)} 个新交易日，最新 {latest} / {result[latest]:.2f}")
    return result


EASTMONEY_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
# 东方财富「微盘股」板块指数，作为微盘股（868008.WI）每日涨幅的近似代理
WIND_PROXY_SECID = "90.BK1158"
# 同花顺「微盘股」板块指数（883418），作为微盘股（868008.WI）每日涨幅的备用代理。
# 实测与微盘日涨跌幅相关系数约 0.982、日均绝对误差约 0.28 个百分点，短期倒推可靠。
# 当东财 push2 系列行情接口被数据中心 IP 拦截（403）时启用此备用源。
THS_KLINE_URL = "https://d.10jqka.com.cn/v6/line/bk_883418/01/last.js"
THS_HEADERS = {
    "User-Agent": TENCENT_HEADERS["User-Agent"],
    "Referer": "http://stockpage.10jqka.com.cn/",
    "Cookie": "v=AAAAAAAAAAAA; hexin-v=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
}


# ===== 微盘股每日估值自算（PB / 市值中位数） =====
# 万得 868008 的官方 PB、市值中位数只更新到 2026-11-05；此后每个交易日由脚本自算。
# 数据源用腾讯行情全市场快照（一次 60 只批量），不依赖任何成分股名单：
# 直接按总市值升序取前 400 只（剔除 ST/PT/退市/停牌），与万得「市值最小的 400 只」选样一致。
# 注意：行情地址模板统一用文件顶部的 TENCENT_QUOTE_URL（含 {symbols} 占位符），
# 这里不要再重新赋值，否则覆盖模板会让 fetch_stock_quotes 拼出空查询串。
# 自算整体法 PB（Σ总市值 / Σ净资产）与万得加权 PB 的口径校准系数：
# 2026-11-05 实测 Wind 2.0287 / 自算 1.9748 = 1.0273。仅用于消除口径系统差，避免曲线跳变。
MICRO_PB_CALIB = 1.0273
MICRO_TOP_N = 400
# 官方数据（Wind 868008）最后覆盖日；该日及之前一律用官方值，自算从次一交易日起生效。
MICRO_OFFICIAL_END = "2026-11-05"


def _tencent_batch(batch):
    """取一批股票（最多 60 只）的 名称/总市值/市净率/现价/成交量。"""
    try:
        resp = _request_with_retry(TENCENT_QUOTE_URL.format(symbols=",".join(batch)),
                                   headers=TENCENT_HEADERS, timeout=15)
        resp.encoding = "gbk"
        out = []
        for line in resp.text.split(";"):
            if "=" not in line:
                continue
            f = line.split("=", 1)[1].strip().strip('"').split("~")
            if len(f) < 48:
                continue
            name = f[1]
            try:
                price = float(f[3] or 0)
                vol = float(f[6] or 0)
                mc = float(f[45])   # 总市值（亿元）
                pb = float(f[46])   # 市净率
            except (TypeError, ValueError):
                continue
            upper = name.upper()
            if mc <= 0 or pb <= 0 or price <= 0 or vol <= 0:
                continue
            if "ST" in upper or "PT" in upper or "退" in name or "N" in upper or "C" in upper or "U" in upper:
                continue
            out.append((name, mc, pb))
        return out
    except Exception:
        return []


def fetch_micro_valuation_snapshot():
    """全市场快照 -> 万得微盘股口径的 (PB, 市值中位数)。

    PB 用法：Σ总市值 / Σ(总市值/市净率)，即整体法；再乘校准系数对齐万得加权 PB。
    返回 {"pb": float, "mcap": float, "n": int}，抓取不足时返回 None。
    """
    codes = []
    for lo, hi in ((600000, 604000), (605000, 606000), (688000, 689000)):
        codes += [f"sh{i}" for i in range(lo, hi)]
    codes += [f"sz{i:06d}" for i in range(1, 4000)]
    codes += [f"sz{i}" for i in range(300000, 303000)]
    batches = [codes[i:i + 60] for i in range(0, len(codes), 60)]

    rows = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for part in pool.map(_tencent_batch, batches):
            rows.extend(part)
    if len(rows) < 3000:
        print(f"微盘估值自算：仅取到 {len(rows)} 只，疑似行情源异常，跳过本次更新。")
        return None

    rows.sort(key=lambda x: x[1])
    top = rows[:MICRO_TOP_N]
    mcap_med = float(np.median([r[1] for r in top]))
    pb_agg = sum(r[1] for r in top) / sum(r[1] / r[2] for r in top)
    print(f"微盘估值自算：{len(rows)} 只中取最小 {len(top)} 只，"
          f"整体PB {pb_agg:.4f}（校准后 {pb_agg * MICRO_PB_CALIB:.4f}），市值中位 {mcap_med:.2f} 亿元")
    return {"pb": round(pb_agg * MICRO_PB_CALIB, 4), "mcap": round(mcap_med, 2), "n": len(top)}


def _apply_micro_daily(data_obj, trade_date):
    """写入当日 PB / 市值中位数到 pb_daily、mcap_daily（同日覆盖），并同步估值表。

    首选 Wind 官方加权 PB 与成份股总市值中位数（一次接口调用同时取到）；
    Wind 不可用时回退：官方区间内跳过，之后用全市场市值最小 400 只自算。
    """
    snap = None
    source = ""
    if wind_configured():
        try:
            wv = wind_index_valuation("868008.WI", trade_date)
            if wv and wv.get("pb"):
                # 接口未按指定日期返回（拿到的是上一交易日）时不予采信，避免把旧值当当日值
                if wv.get("date") and wv["date"] != trade_date:
                    print(f"微盘估值：Wind 返回日期 {wv['date']} 与目标 {trade_date} 不符，本次不采用。")
                elif wv.get("mcap") is None:
                    print("微盘估值：Wind 未返回市值中位数，本次不采用。")
                else:
                    snap = {"pb": wv["pb"], "mcap": wv["mcap"], "pe": wv.get("pe")}
                    source = "Wind 官方（868008.WI 加权PB）"
        except Exception as e:
            print(f"⚠️ 微盘估值：Wind 官方接口失败，改走自算: {e}")

    if snap is None:
        if trade_date <= MICRO_OFFICIAL_END:
            print(f"微盘估值：{trade_date} 仍在官方数据区间（≤{MICRO_OFFICIAL_END}），跳过。")
            return False
        snap = fetch_micro_valuation_snapshot()
        if not snap:
            return False
        source = "脚本自算·全市场市值最小400只"

    changed = False

    # 当日指数点位（用于图右侧点位线）
    point = None
    if trade_date in data_obj.get("dates", []):
        point = data_obj["values"][data_obj["dates"].index(trade_date)]

    pbd = data_obj.get("pb_daily")
    if pbd is not None:
        if pbd["dates"] and pbd["dates"][-1] == trade_date:
            if abs(pbd["pb"][-1] - snap["pb"]) > 0.0005:
                pbd["pb"][-1] = snap["pb"]
                pbd["point"][-1] = point
                changed = True
        elif not pbd["dates"] or trade_date > pbd["dates"][-1]:
            pbd["dates"].append(trade_date)
            pbd["pb"].append(snap["pb"])
            pbd["point"].append(point)
            changed = True

    md = data_obj.get("mcap_daily")
    if md is None:
        md = data_obj["mcap_daily"] = {"dates": [], "value": [], "point": []}
    if md["dates"] and md["dates"][-1] == trade_date:
        if abs(md["value"][-1] - snap["mcap"]) > 0.005:
            md["value"][-1] = snap["mcap"]
            md["point"][-1] = point
            changed = True
    elif not md["dates"] or trade_date > md["dates"][-1]:
        md["dates"].append(trade_date)
        md["value"].append(snap["mcap"])
        md["point"].append(point)
        changed = True

    val = data_obj.setdefault("valuation", {})
    # 分位样本一律取「近 10 年」。pb_daily 已覆盖 2016-11 起的日频，直接用；
    # 若历史尚未补齐（点数过少），回退到「周频历史 + 近年日频」拼接，避免口径缩水。
    daily_pb = list(pbd.get("pb") or []) if pbd else []
    if len(daily_pb) >= 1500:
        base = daily_pb
    else:
        base = [p for d, p in zip((data_obj.get("pb_weekly") or {}).get("dates", []),
                                  (data_obj.get("pb_weekly") or {}).get("pb", []))
                if d <= "2024-12-31"]
        base += [p for d, p in zip(pbd["dates"], pbd["pb"]) if d >= "2025-01-01"] if pbd else []
    pct = None
    if len(base) >= 2:
        cur = snap["pb"]
        # 与 percentile_in_10y 统一口径：100 × 严格小于当前值的个数 ÷ (样本数 − 1)
        below = sum(1 for v in base if v < cur)
        pct = round(below / (len(base) - 1) * 100, 1)
    if (val.get("pb") != snap["pb"] or val.get("data_date") != trade_date
            or val.get("source") != source):
        val["pb"] = snap["pb"]
        val["pb_percentile_10y"] = pct
        val["data_date"] = trade_date
        val["weight_date"] = trade_date.replace("-", "")
        val["source"] = source
        changed = True
    print(f"微盘估值已写入 {trade_date}：PB {snap['pb']}（分位 {pct}%），"
          f"市值中位 {snap['mcap']} 亿元，来源 {source}")
    return changed


def _extrapolate_wind_from_bk1158(last_saved_date, last_saved_value):
    """用东财微盘板块（BK1158）的每日涨跌幅倒推微盘股指数点位。

    实测：BK1158 与微盘的日涨跌幅平均绝对误差约 0.09 个百分点，短期倒推可靠；
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
    """用同花顺「微盘股」板块指数（883418）的每日涨跌幅倒推微盘股指数点位。

    该接口返回最近约 140 个交易日的日 K 线（JSONP），收盘价位于每行第 5 个字段。
    仅填充锚点之后的新交易日；实测与微盘日涨跌幅相关系数约 0.982，
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
        CSI_WEIGHT_URL.format(code=code), headers=TENCENT_HEADERS, timeout=60)
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


def _to_tencent_symbol(symbol):
    """把 Wind 代码（600000.SH / 000001.SZ / 830799.BJ）转成腾讯行情代码（sh600000）。

    已经是腾讯格式（sh600000）的原样返回；无法识别的（如美股代码）返回空串。
    """
    raw = str(symbol).strip()
    if re.match(r"^(sh|sz|bj)\d{6}$", raw):
        return raw
    matched = re.match(r"^(\d{6})\.(SH|SZ|BJ)$", raw.upper())
    if not matched:
        return ""
    return matched.group(2).lower() + matched.group(1)


def fetch_quotes_for_rows(rows):
    """为成份股批量抓个股行情，返回 {原始代码: 腾讯字段列表}（保持 rows 里的代码格式）。"""
    mapping = {}
    for row in rows:
        converted = _to_tencent_symbol(row[0])
        if converted:
            mapping[converted] = str(row[0])
    if not mapping:
        return {}
    quotes = fetch_stock_quotes(list(mapping.keys()))
    return {mapping[k]: v for k, v in quotes.items() if k in mapping}


def compute_weighted_dividend_yield(rows, quotes, denominator="valid",
                                    require_pe_pb=True):
    """加权股息率＝成分股股息率按「指数权重」算术加权。

    与 PE/PB 的整体法不同，股息率走的是「指数权重 × 个股股息率」求和：
    指数自己用什么规则加权（红利类按股息率、红利低波按波动率、国信价值按等权），
    官网权重文件给的就是这套规则落地的实时权重，直接拿来加权即等价于复现指数。
    例如国信价值是等权编制（权重因子令样本等权、每只约 1%、半年调样一次），
    官方权重文件实测均值 1.000%、区间 0.41%~1.39%（调样间隔内的价格漂移），
    按它加权就等于等权加权，不需要再人为摊成 1/N。

    require_pe_pb=True（默认）：个股需 PE>0、PB 与股息率同时有效才计入，剔除
    亏损/缺数据的个股。只算股息率的指数（红利类、价值100、国信价值）应传
    False——它们的 PE/PB 根本不出现在页面上，用 PE>0 当门槛会把「亏损但仍在
    分红」的个股错误剔除。实测各指数影响在 0.03 个百分点以内（国信价值本次
    100 只全部计入即 4.29%，与官网单张 4.45% 的差异来自估值日不同），但口径上
    不该有这层无谓耦合。
    返回 (股息率%, 计入只数)。

    denominator="valid"（默认）把权重归一化到「计入个股」上；成分股多达 2000 只、
    亏损或数据缺失个股占比很高时（如中证2000，只有 6 成个股三项齐全），归一化会
    把股息率系统性放大（实测 1.42% vs 官网 0.90%），此时应传 "total"：
    分母用全部成分股权重，缺失个股按 0 计（实测 0.85%，与官网 0.90% 接近）。
    """
    total_weight = 0.0
    all_weight = 0.0
    sum_dividend = 0.0
    valid = 0
    for row in rows:
        symbol, weight = row[0], row[2]
        all_weight += weight
        fields = quotes.get(symbol)
        if not fields or len(fields) <= TENCENT_FIELD_DY:
            continue
        dividend = _to_positive_float(fields[TENCENT_FIELD_DY])
        if dividend is None:
            continue
        if require_pe_pb:
            pe = _to_float(fields[TENCENT_FIELD_PE])
            pb = _to_positive_float(fields[TENCENT_FIELD_PB])
            if pe is None or pe <= 0 or pb is None:
                continue
        total_weight += weight
        sum_dividend += weight * dividend
        valid += 1
    base = all_weight if denominator == "total" else total_weight
    if not base:
        return None, 0
    return round(sum_dividend / base, 2), valid


def build_market_cap_block(rows, quotes):
    """按成分股总市值分档统计（大盘/中盘/小盘），复用已抓取的行情。

    除算术平均外还算「加权市值」= Σ(指数权重 × 总市值) ÷ Σ权重：
    它才是指数真实的市值暴露——等权指数两者相等，市值/股息率加权的指数会明显
    偏向大市值（红利类尤其明显），用来看「整体偏大盘还是偏小盘」比算术平均准。
    """
    caps = []
    for row in rows:
        symbol, weight = row[0], row[2]
        fields = quotes.get(symbol)
        if not fields or len(fields) <= TENCENT_FIELD_MARKET_CAP:
            continue
        cap = _to_positive_float(fields[TENCENT_FIELD_MARKET_CAP])
        if cap is None:
            continue
        caps.append((cap, _to_positive_float(weight) or 0.0))
    if not caps:
        return {}
    return _summarize_market_cap(caps)


def _wind_code_value_map(doc, key_hint="市值"):
    """从 Wind 自然语言结果里抽出 {股票代码: 数值}，列名含 key_hint 的取数值。"""
    out = {}
    for blk in (doc.get("data") or {}).get("data") or []:
        names = [c.get("name", "") for c in blk.get("columns") or []]
        for row in blk.get("rows") or []:
            sym, val = None, None
            for col, v in zip(names, row):
                if isinstance(v, str) and re.match(r"^\d{6}\.(SZ|SH|BJ)$", v.strip()):
                    sym = v.strip()
                elif key_hint in str(col):
                    try:
                        val = float(v)
                    except (TypeError, ValueError):
                        pass
            if sym and val is not None:
                out[sym] = val
    return out


def _components_cache_ok(cached, week_key):
    """本周缓存能否直接复用：必须是 Wind 取的权重，且刷新周是本周。

    ETF 回退结果（source=etf）即使标了本周也不能复用——否则一次 Wind 失败
    会把 515450 的滞后持仓锁死一整周，下周之前都不会再尝试 Wind。
    """
    return bool(cached.get("rows")
                and cached.get("refreshed_week") == week_key
                and (cached.get("source") or "wind") == "wind")


def wind_index_components(name):
    """取指数成份股（代码 / 简称 / 权重）与各股总市值，返回 (rows, mcap, weight_date)。

    rows 形如 [(代码, 简称, 权重%)]，与中证/国证权重文件的解析结果同构，
    可直接喂给原有市值分档逻辑；mcap 为 {代码: 总市值(亿元)}。
    部分指数一次问「权重 + 市值」只会返回权重，此时单独补一次市值查询。
    """
    code = WIND_CODES.get(name)
    if not code:
        return [], {}, None
    question = (f"查询{code}的成分股，逐只列出股票代码、简称、"
                f"指数权重(%)、总市值(亿元)")
    doc = json.loads(_wind_call("get_financial_data", {"question": question}, timeout=180))
    rows, mcap, weight_date = [], {}, None
    dropped = 0
    for blk in (doc.get("data") or {}).get("data") or []:
        names = [c.get("name", "") for c in blk.get("columns") or []]
        for row in blk.get("rows") or []:
            rec = dict(zip(names, row))
            sym = rec.get("指数成份代码")
            if not sym:
                continue
            try:
                weight = float(rec.get("指数成份权重"))
            except (TypeError, ValueError):
                continue
            # 剔除零权重/负权重：Wind 会把调样过渡期已摘出、或曾属成分的个股一并列出
            # （实测 2026-11 标普列表尾部挂着一只权重 0.00% 的贵州茅台）。
            # 这类行没有权重贡献，却会把总市值和成分股只数算大 8%。
            if weight <= 0:
                dropped += 1
                continue
            rows.append((str(sym), str(rec.get("指数成份简称") or ""), weight))
            try:
                mcap[str(sym)] = float(rec.get("总市值"))
            except (TypeError, ValueError):
                pass
            if not weight_date and rec.get("日期"):
                weight_date = _norm_wind_date(rec["日期"])
    if rows and len(mcap) < len(rows) // 2:
        try:
            doc2 = json.loads(_wind_call(
                "get_financial_data",
                {"question": f"查询{code}成分股的总市值（亿元），逐只列出股票代码与总市值"},
                timeout=180))
            mcap = {**mcap, **_wind_code_value_map(doc2, "市值")}
        except Exception as e:
            print(f"⚠️ {name}：成分股总市值补充查询失败（{str(e)[:60]}）")
    if rows:
        wsum = sum(w for _, _, w in rows)
        print(f"{name}：Wind 成份股 {len(rows)} 只，权重合计 {wsum:.2f}%，"
              f"总市值覆盖 {len(mcap)}/{len(rows)}（权重日 {weight_date or '—'}）"
              + (f"，剔除零权重 {dropped} 只" if dropped else ""))
        # 自检：指数权重合计应接近 100。明显偏离说明 Wind 回的是「ETF 占净值比」
        # 或只回了部分成份，此时代码不能当指数权重用，交由上层回退免费源。
        if not (90 <= wsum <= 110):
            raise ValueError(f"Wind 权重合计 {wsum:.2f}% 异常（应接近 100%），疑似非指数权重")
    return rows, mcap, weight_date


def wind_index_industry(name, weight_date=None):
    """取指数按申万一级/二级行业的权重分布，返回与 build_industry_block 同构的字典。"""
    code = WIND_CODES.get(name)
    if not code:
        return {}
    levels = {}
    for key, label in (("L1", "申万一级"), ("L2", "申万二级")):
        try:
            doc = json.loads(_wind_call(
                "get_financial_data",
                {"question": f"查询{code}按{label}行业的权重分布"}, timeout=180))
        except Exception:
            continue
        items = []
        for blk in (doc.get("data") or {}).get("data") or []:
            for row in blk.get("rows") or []:
                if len(row) < 2:
                    continue
                try:
                    items.append({"name": str(row[0]), "weight": round(float(row[1]), 2)})
                except (TypeError, ValueError):
                    continue
        if items:
            items.sort(key=lambda x: -x["weight"])
            levels[key] = {"label": label, "items": items}
    if not levels:
        return {}
    default_level = "L2" if "L2" in levels else next(iter(levels))
    return {
        "items": levels[default_level]["items"],
        "levels": levels,
        "default_level": default_level,
        "standard": levels[default_level]["label"],
        "date": weight_date,
        "source": "Wind（申万行业分类）",
    }


def build_market_cap_from_map(rows, mcap):
    """用 {代码: 总市值(亿元)} 与成份股权重做市值分档，口径同 build_market_cap_block。"""
    pairs = [(mcap[s], _to_positive_float(w) or 0.0) for s, _, w in rows if s in mcap]
    if not pairs:
        return {}
    return _summarize_market_cap(pairs)


def _summarize_market_cap(pairs):
    """把 [(总市值亿元, 权重%)] 汇总成 {total, average, weighted, weight_sum, count, buckets}。

    weighted = Σ(权重 × 市值) ÷ Σ权重，即指数按自身权重配置后的「每单位权重的平均市值」；
    weight_sum 是参与计算的权重合计，<100 说明部分样本缺市值（页面据此标注覆盖率）。

    零权重行（调样过渡期残留）在这里统一剔除：它们对加权市值没有贡献，
    却会把总市值和成分股只数算大（标普曾因此把总市值多算 1.8 万亿）。
    """
    pairs = [(c, w) for c, w in pairs if w > 0]
    if not pairs:
        return {}
    total = sum(c for c, _ in pairs)
    weight_sum = sum(w for _, w in pairs)
    weighted = (sum(c * w for c, w in pairs) / weight_sum) if weight_sum else None
    buckets = []
    for label, low, high in MARKET_CAP_BUCKETS:
        picked = [
            (c, w) for c, w in pairs
            if (low is None or c >= low) and (high is None or c < high)
        ]
        buckets.append({
            "name": label,
            "count": len(picked),
            "weight": round(sum(w for _, w in picked), 2),
        })
    return {
        "total": round(total, 2),                 # 成分股合计总市值（亿元）
        "average": round(total / len(pairs), 2),   # 算术平均市值（亿元）
        "weighted": round(weighted, 2) if weighted is not None else None,  # 加权平均市值（亿元）
        "weight_sum": round(weight_sum, 2),        # 参与计算的权重合计（%）
        "count": len(pairs),
        "buckets": buckets,
    }


def build_valuation_block(rows, quotes, weight_date, source="CSI",
                          exclude_losses=False, dy_total_weight=False,
                          want_pb=True, want_dy=True):
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
    - 股息率口径：指数权重算术加权（见 compute_weighted_dividend_yield）。
      只展示股息率的指数（want_pb=False）不设 PE/PB 门槛，避免把亏损但仍分红
      的个股漏掉；同时需要 PE/PB 的指数仍按三者齐全计入，与历史口径一致。

    want_pb / want_dy 控制「只算页面要展示的指标」：只展示 PB 的指数不再白算
    股息率，红利/高股息类（只展示股息率）则完全不算 PE/PB，结果里也不会出现
    这两个字段（页面对应单元格直接留空）。
    """
    caps_pe, earnings = [], []      # (总市值, 净利润TTM=市值/PE)
    caps_pb, book_values = [], []   # (总市值, 净资产=市值/PB)
    quote_dates = []
    for symbol, _name, weight, *_rest in rows:
        fields = quotes.get(symbol)
        if not fields:
            continue
        # 记录行情所属交易日（各成分股取最新），即估值数据的实际日期
        raw_time = fields[TENCENT_FIELD_TIME] if len(fields) > TENCENT_FIELD_TIME else ""
        if len(raw_time) >= 8 and raw_time[:8].isdigit():
            quote_dates.append(raw_time[:8])
        pe = _to_float(fields[TENCENT_FIELD_PE])            # 允许负值（亏损股）
        cap = _to_positive_float(fields[TENCENT_FIELD_MARKET_CAP])
        pb = _to_positive_float(fields[TENCENT_FIELD_PB])

        # PE / PB：整体法，独立取有效样本（含亏损股负盈利）；不需要 PB 时不取这两项
        if want_pb and cap is not None:
            if pe and (pe > 0 or not exclude_losses):
                caps_pe.append(cap)
                earnings.append(cap / pe)
            if pb:
                caps_pb.append(cap)
                book_values.append(cap / pb)

    # 股息率：指数权重算术加权（亏损股、PE/PB 缺失不影响——只算股息率时不设 PE/PB 门槛）
    dy_value, valid_dy = ((None, 0) if not want_dy else
                          compute_weighted_dividend_yield(
                              rows, quotes, "total" if dy_total_weight else "valid",
                              require_pe_pb=want_pb))

    if not (earnings or book_values or dy_value is not None):
        return {}

    block = {
        "weight_date": weight_date,
        # 个股行情所属交易日：估值数据本身的日期
        "data_date": _normalize_date(max(quote_dates)) if quote_dates else weight_date,
        "source": {"CSI": "中证指数官网", "CNI": "国证指数官网",
                   "WIND": "Wind 官方成份股"}.get(source, "天天基金·ETF全持仓"),
        "component_count": len(rows),
    }
    # 只展示股息率的指数（红利/高股息类）不落库 PE/PB
    if want_pb:
        block["pe_ttm"] = round(sum(caps_pe) / sum(earnings), 2) if earnings else None
        block["pb"] = round(sum(caps_pb) / sum(book_values), 2) if book_values else None
    if want_dy:
        block["dividend_yield"] = dy_value
        block["valid_count"] = valid_dy
    block["updated_at"] = now_cn().strftime("%Y-%m-%d %H:%M:%S")
    return block


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
    """把最新估值追加进内存中的历史序列（日期升序），返回是否写入。

    幂等但可纠正：同日期若已有记录，数值一致则跳过；数值不同则用本次结果覆盖。
    早期版本写的是「同日期一律跳过」，导致当天第一次抓到的临时值（如 Wind 盘中口径）
    永久留在序列里，与估值表展示的收盘口径对不上（分位也会跟着偏）。
    """
    key = _history_sheet_name(index_name)
    entry = history.setdefault(key, {"dates": [], "pe": [], "pb": []})
    if trade_date in entry["dates"]:
        i = entry["dates"].index(trade_date)
        same_pe = (entry["pe"][i] == pe) or (
            entry["pe"][i] is not None and pe is not None and abs(entry["pe"][i] - pe) < 1e-9)
        same_pb = (entry["pb"][i] == pb) or (
            entry["pb"][i] is not None and pb is not None and abs(entry["pb"][i] - pb) < 1e-9)
        if same_pe and same_pb:
            return False
        entry["pe"][i] = pe
        entry["pb"][i] = pb
        return True
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


def fetch_index_metrics(all_data=None):
    """计算全部已配置指数的估值 / 市值分布 / 行业分布，返回 {指数名: {...}}。

    数据源以免费接口为主：成份股权重、个股行情、行业分布全部取自中证/国证官网
    权重文件 + 腾讯个股行情 + 红色火箭/CICS，不消耗 Wind 积分。
    Wind 只用三处：
      1) 成长100R / 中证科技100 的官方加权 PB（PB 是这几只唯一展示的估值）；
      2) 中证2000 的官方加权 PB（其余指标仍走中证官网免费接口，不按成份股自算 PB）；
      3) 标普红利低波50 的实时成份股与申万行业（515450 持仓滞后一个报告期）。
    万得微盘股指数不在此循环，其 PB 由 update_html 里的 _apply_micro_daily 单独处理。
    各指数只算自己展示的那一项指标（valuation_metrics），不需要的指标不取数、不落库。
    任一环节失败自动回退成份股自算，不中断更新。
    """
    result = {}
    week_key = "%d-W%02d" % now_cn().isocalendar()[:2]
    for name, (code, source) in VALUATION_CONFIG.items():
        trade_date = None
        if all_data and name in all_data:
            ds = all_data[name].get("dates") or []
            trade_date = ds[-1] if ds else None

        # 这只指数要算哪一项：pb=只算 PB（含 PE 内部续算）、dy=只算股息率、空集=不展示
        metrics = valuation_metrics(name)
        want_pb, want_dy = "pb" in metrics, "dy" in metrics

        wind_val, rows, mcap, industry = None, [], {}, {}
        # 行业分布（申万权重）一周才变一次，按 ISO 周缓存：
        # 同一周内重复运行直接沿用上次结果，省掉 2 次 get_financial_data/指数。
        cached_industry = {}
        if all_data:
            cached_industry = (((all_data.get(name) or {}).get("profile") or {})
                               .get("industry") or {})
        # 命中条件只看刷新周，不要求 items 非空：取数失败时也会写入一条带
        # refreshed_week 的空记录，本周内不再反复重试（下周自动重试），省积分。
        if cached_industry.get("refreshed_week") == week_key:
            industry = cached_industry
            if cached_industry.get("items"):
                print(f"{name}：行业分布本周已刷新（{cached_industry.get('refreshed_at') or '—'}），"
                      f"沿用上次结果")
            else:
                print(f"{name}：行业分布本周已尝试但未取到，本周跳过（下周自动重试）")
        weight_date = trade_date
        # 只有需要「官方加权 PB」的指数才调用 Wind 估值接口（成长100R / 中证科技100 /
        # 中证2000），其余指数一律用中证/国证官网权重 + 个股行情自算，不耗积分。
        if want_pb and name in WIND_PB_INDICES and wind_configured() and name in WIND_CODES:
            try:
                wind_val = wind_index_valuation_full(name, trade_date)
            except Exception as e:
                print(f"⚠️ {name}：Wind 官方 PB 获取失败（{str(e)[:70]}），回退成份股自算。")
        elif name in FREE_ONLY_INDICES:
            print(f"{name}：点位/成份股用中证官网免费数据（不耗积分）")

        # 成份股：默认走中证/国证官网权重文件（免费）；标普官方不公开权重文件、
        # 515450 的持仓又滞后一个报告期且是「占净值比」而非指数权重，因此改用 Wind
        # 的实时指数成份股。标普半年才调样一次，成份股/市值/行业一律按 ISO 周缓存：
        # 同一周内重复运行（哪怕一天跑好几次）不再调用 Wind，一周固定只花
        # 1 次 get_financial_data（权重+市值）+ 2 次（申万一级/二级行业）。
        cached_components = {}
        if all_data:
            cached_components = (((all_data.get(name) or {}).get("profile") or {})
                                 .get("components") or {})
        components_reused = False
        components_from_wind = False
        if name in WIND_COMPONENT_INDICES and wind_configured() and name in WIND_CODES:
            # 只有「Wind 取的、且是本周的」缓存才复用。回退到 515450 的那次结果
            # 也存进 components（前端据此标注口径），但 source=etf，下次运行必须
            # 重新尝试 Wind——否则一次失败会把 ETF 权重锁死一整周。
            cache_ok = _components_cache_ok(cached_components, week_key)
            if cache_ok:
                rows = cached_components["rows"]
                mcap = cached_components.get("mcap") or {}
                weight_date = cached_components.get("weight_date") or trade_date
                components_reused = True
                components_from_wind = True
                print(f"{name}：Wind 成份股本周已刷新（{cached_components.get('refreshed_at') or '—'}），"
                      f"沿用上次结果（本周不再调用 Wind）")
                # 权重命中缓存但行业没取到（上次行业调用失败）时补一次：否则会掉到
                # 「免费行业源」分支——标普没有免费源，画像行业分布会一直空白。
                if not industry:
                    try:
                        industry = wind_index_industry(name, weight_date) or {
                            "items": [], "levels": {}, "date": weight_date,
                            "source": "Wind（申万行业分类）· 本周取数失败"}
                    except Exception as e:
                        print(f"⚠️ {name}：行业分布补取失败（{str(e)[:60]}），本周跳过。")
                        industry = {"items": [], "levels": {}, "date": weight_date,
                                    "source": "Wind（申万行业分类）· 本周取数失败"}
            else:
                try:
                    rows, mcap, wd = wind_index_components(name)
                    weight_date = wd or trade_date
                    components_from_wind = bool(rows)
                    if not industry:
                        industry = wind_index_industry(name, weight_date)
                except Exception as e:
                    print(f"⚠️ {name}：Wind 成份股/行业获取失败（{str(e)[:70]}），回退免费权重文件。")
                    rows, mcap = [], {}

        quotes = {}
        market_cap = {}
        val_source = source
        if not rows:
            try:
                if source == "SP":
                    rows, weight_date = fetch_sp_weights()
                else:
                    rows, weight_date = (
                        fetch_cni_weights(code) if source == "CNI" else fetch_csi_weights(code)
                    )
                quotes = fetch_stock_quotes([row[0] for row in rows])
            except Exception as e:  # 第三方文件结构变化不应中断整体更新
                print(f"⚠️ {name} 成分股数据抓取失败: {e}")
                if not wind_val:
                    continue
            market_cap = build_market_cap_block(rows, quotes) if quotes else {}
        else:
            # 成份股来自 Wind（目前只有标普走这条）：估值来源标签随之改为 Wind 成份股
            val_source = "WIND"

        # 股息率沿用原口径：按成分股（指数权重）算术加权，不用 Wind 直接给的加权值。
        # Wind 成份股代码是 600000.SH 形式，需转成腾讯行情代码再抓个股行情。
        if rows and not quotes:
            try:
                quotes = fetch_quotes_for_rows(rows)
            except Exception as e:
                print(f"⚠️ {name}：成份股行情抓取失败，股息率留空: {str(e)[:60]}")
                quotes = {}
        # 市值口径统一用腾讯行情，与其它指数保持一致；Wind 的 mcap 只在行情缺失时兜底。
        # 不能直接用 Wind 的「总市值」：它是 A 股股本口径，A+H 公司会明显偏小
        # （如中国移动 Wind 1.54 万亿 vs 腾讯 2.3 万亿），混用后加权市值与其他指数不可比。
        if rows and not market_cap:
            market_cap = (build_market_cap_block(rows, quotes) if quotes
                          else (build_market_cap_from_map(rows, mcap) if mcap else {}))
        # 只算页面要展示的那一项：只展示 PB 的指数不再白算股息率，红利类不再算 PE/PB
        dy_calc, dy_valid = ((None, 0) if not want_dy or not quotes
                             else compute_weighted_dividend_yield(rows, quotes))

        block = None
        # 整块估值都取 Wind 官方加权的指数（成长100R / 中证科技100）；
        # 中证2000 不在其中——它只覆盖 PB，其余走免费源（见下方 FREE_ONLY_PB_OVERRIDE）。
        if wind_val and name not in FREE_ONLY_PB_OVERRIDE:
            vdate = wind_val.get("date") or trade_date
            # 部分指数（如标普红利低波50）单日快照不给 PE，日频序列里却有值，
            # 此时沿用上一交易日的 PE，避免每天把 PE 写成空
            pe_val = wind_val["pe"]
            if pe_val is None:
                prev_valuation = (all_data.get(name) or {}).get("valuation") or {}
                pe_val = prev_valuation.get("pe_ttm")
            block = {
                "pe_ttm": round(pe_val, 4) if pe_val else None,
                "pb": round(wind_val["pb"], 4) if wind_val["pb"] is not None else None,
                "weight_date": (vdate or "").replace("-", ""),
                "data_date": vdate,
                "component_count": len(rows) or None,
                "source": "Wind 官方加权",
            }
            if want_dy:
                # 个股行情全拿不到时（如美股成份股）才退回 Wind 的官方加权值
                block["dividend_yield"] = dy_calc if dy_calc is not None else (
                    round(wind_val["dy"], 4) if wind_val["dy"] is not None else None)
                block["valid_count"] = dy_valid or len(rows) or None
        if not block:
            block = build_valuation_block(
                rows, quotes, weight_date, val_source,
                exclude_losses=name in PE_EXCLUDE_LOSSES,
                dy_total_weight=name in DY_TOTAL_WEIGHT,
                want_pb=want_pb, want_dy=want_dy)
            if not block:
                continue
            # 中证2000：PB 直接用 Wind 官方加权值覆盖自算值（用户要求，不做成分股自算）
            if (name in FREE_ONLY_PB_OVERRIDE and want_pb and wind_val
                    and wind_val.get("pb") is not None):
                block["pb"] = round(wind_val["pb"], 4)
                block["pb_source"] = "Wind 官方加权"
                print(f"{name}：PB 改用 Wind 官方加权值 {block['pb']}")

        if not industry:
            try:
                industry = build_industry_block(rows, code, source, weight_date, name)
            except Exception as e:
                print(f"⚠️ {name} 行业分布计算失败: {e}")
                # 写一条本周内的「失败占位」，避免同一天重复跑时反复抓同一个坏接口
                industry = {"items": [], "levels": {}, "date": weight_date,
                            "source": "行业分类取数失败"}

        # 本次新取的行业分布打上刷新周，下周同一周内直接复用
        if industry and industry is not cached_industry:
            industry["refreshed_week"] = week_key
            industry["refreshed_at"] = now_cn().strftime("%Y-%m-%d")

        # 标普本次新取的成份股/市值打上刷新周，同一周内复用（省 Wind 调用）。
        # source 记录这份权重的真实出处：wind=Wind 实时指数权重（可整周复用）；
        # etf=515450 全持仓占比（滞后一个报告期，仅当周展示，下次仍要重试 Wind）。
        components = {}
        if name in WIND_COMPONENT_INDICES and rows:
            components = cached_components if components_reused else {
                "rows": rows,
                "mcap": mcap,
                "weight_date": weight_date,
                "source": "wind" if components_from_wind else "etf",
                "refreshed_week": week_key,
                "refreshed_at": now_cn().strftime("%Y-%m-%d"),
            }

        # 估值反映的是「该指数最新收盘日」的值，但 Wind/官网有时把日期标成当天
        # （当天尚未收盘时更明显，会出现 11-06 对应 11-05 收盘值的情况）。
        # 统一截到点位最后交易日：pepbHistory 是只追加不覆盖的，这类假点一旦写进去
        # 就永久留在历史序列里，稀释估值分位。
        if trade_date and block.get("data_date") and block["data_date"] > trade_date:
            block["data_date"] = trade_date

        result[name] = {"valuation": block, "market_cap": market_cap,
                        "industry": industry, "components": components}
        shown = ([f"PB {block['pb']}"] if "pb" in block else []) + (
            [f"加权股息率 {block['dividend_yield']}%"] if "dividend_yield" in block else [])
        print(
            f"{name} 估值：" + ("｜".join(shown) if shown else "不展示估值指标")
            + f"（{block['source']}，权重日 {block['weight_date']}）"
        )
        if market_cap:
            bucket_text = " / ".join(
                f"{b['name']} {b['count']}只" for b in market_cap["buckets"]
            )
            weighted_text = (f"｜加权 {market_cap['weighted']:.2f} 亿"
                             if market_cap.get("weighted") is not None else "")
            print(f"    市值分布：合计 {market_cap['total']/10000:.2f} 万亿｜"
                  f"算术平均 {market_cap['average']:.2f} 亿{weighted_text}｜{bucket_text}")
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
    # 文件缺失必须显式报错：以前这里静默 return，会让 Action 显示「成功」但页面
    # 一个字都没更新（例如把网页改过名、却没同步改脚本里的文件名）。
    if not os.path.exists(file_path):
        siblings = sorted(p for p in os.listdir(".") if p.lower().endswith((".html", ".htm")))
        hint = (f"同目录下现有的网页文件：{'、'.join(siblings)}" if siblings
                else "当前目录下没有任何 .html 文件")
        raise FileNotFoundError(
            f"未找到网页文件「{file_path}」（当前目录 {os.getcwd()}；{hint}）。"
            f"请确认仓库里的文件名与 update_html() 的参数一致。")

    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    match = re.search(r'let allData = (\{.*?\});', content, re.DOTALL)
    if not match:
        raise ValueError(f"网页「{file_path}」中找不到内嵌数据块 let allData = {{...}};，"
                         f"无法写回数据，请确认文件未被破坏。")
    all_data = json.loads(match.group(1))
    updated_any = False

    # 更新回测「无风险利率」默认值：中债10年期国债最新到期收益率
    # 数据源是中债官网免费接口（不耗 Wind 积分）；该值月度变化极小，
    # 仍按「自然月」缓存：同一月份内重复运行直接沿用上次结果。
    cur_month = now_cn().strftime("%Y-%m")
    rf_matched = re.search(r"let RF_DATE = '([^']*)';", content)
    last_rf_date = rf_matched.group(1) if rf_matched else ""
    if last_rf_date[:7] == cur_month:
        print(f"无风险利率：本月已取值（{last_rf_date}），跳过中债官网查询")
    else:
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
            # 记录取值日期，供下个月判断是否需要刷新
            stamp = rf_date or now_cn().strftime("%Y-%m-%d")
            before_rf = content
            if rf_matched:  # 已有该行（可能是空值），原位替换
                content = re.sub(r"let RF_DATE = '[^']*';",
                                 lambda _m: f"let RF_DATE = '{stamp}';", content, count=1)
            else:  # 老版本 HTML 没有 RF_DATE，补一行
                content = re.sub(
                    r"let RF_DEFAULT = [\d.]+;",
                    lambda m: f"{m.group(0)}\n        let RF_DATE = '{stamp}';",
                    content, count=1)
            if content != before_rf:
                updated_any = True

    # 先批量取一次全部 ETF 行情，避免逐只请求被限流
    batch_quotes = fetch_etf_quotes_batch_sina()

    for item in INDEX_CONFIG:
        code, name, source = item["code"], item["name"], item["source"]
        if name not in all_data: continue
            
        dates, values = all_data[name]["dates"], all_data[name]["values"]
        last_saved_date = max(dates) if dates else None
        recent_data = {}
        sp_corrected = False  # 标普官方周频校正是否改写了数据（改写则必须写回）
        wind_done = False  # 本次是否已由 Wind 官方通道取到点位
        # 点位数据源：中证/国证官网、腾讯美股行情全部免费且准确，只有「万得微盘股指数」
        # 和「标普红利低波50全收益」在下面走 Wind 官方日频（无免费官方源），
        # 因此这里不再对 CSI/CNI/US 指数发起 Wind 请求，省积分。
        try:
            if not wind_done and source == "CSI":
                recent_data = fetch_csi_history(code, last_saved_date)
            elif not wind_done and source == "CNI":
                recent_data = fetch_cni_history(code)
            elif source == "SP":
                # 首选 Wind 官方日频（SPCLLHCT.SPI 标普中国A股大盘红利低波50全收益）：
                # 准确点位，不再依赖 515450 累计净值推算，也不必等 ypingcn 的周频校正。
                # 接口不可用时回退原逻辑：515450 净值日频推算 + ypingcn 周频校正。
                wind_done = False
                if wind_configured():
                    try:
                        begin = ((datetime.strptime(last_saved_date, "%Y-%m-%d")
                                  - timedelta(days=5)).strftime("%Y-%m-%d")
                                 if last_saved_date else "2015-01-01")
                        data = wind_index_kline(SP_WINDCODE, begin, now_cn().strftime("%Y-%m-%d"))
                        if data:
                            recent_data = {d: round(v, 2) for d, v in data.items()
                                           if not last_saved_date or d >= last_saved_date}
                            newest = max(data)
                            if all_data[name].get("sp_official_end") != newest:
                                all_data[name]["sp_official_end"] = newest
                                sp_corrected = True
                            if all_data[name].pop("sp_official_error", None) is not None:
                                sp_corrected = True
                            wind_done = True
                            print(f"标普指数：Wind 官方日频取到 {len(recent_data)} 个交易日，"
                                  f"最新 {newest} / {data[newest]:.2f}")
                        else:
                            print("标普指数：Wind 官方接口未返回数据。")
                    except Exception as e:
                        print(f"⚠️ 标普指数 Wind 官方接口失败，回退 515450 推算: {e}")
                if not wind_done:
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
                        sp_corrected = True
                        print(f"标普指数：清除官方末点 {official_end} 之后的 {len(drop)} 个推算日频点。")
                    # 官方周频点就地覆盖/追加。注意用 >=：与官方末点同日的值如果是
                    # 上次运行残留的推算值（例如上次官方抓取失败、本次才恢复），
                    # 也要被官方值覆盖，否则官方末点只增不减会把同日推算值永久锁死
                    if official:
                        for d, v in sorted(official.items()):
                            if official_end is None or d >= official_end:
                                if d in dates:
                                    idx0 = dates.index(d)
                                    if abs(values[idx0] - v) > 0.001:
                                        old_v = values[idx0]
                                        values[idx0] = v
                                        sp_corrected = True
                                        print(f"标普指数：官方点覆盖 {d} 推算值 {old_v} → {v}。")
                                else:
                                    dates.append(d)
                                    values.append(v)
                                    sp_corrected = True
                        # 官方末点只增不减：即使本次无新周点也落盘，
                        # 防止个别运行抓取失败后误把官方历史日频当推算点清除
                        prev_end = all_data[name].get("sp_official_end")
                        new_end = max(max(official), prev_end or "")
                        if new_end != prev_end:
                            all_data[name]["sp_official_end"] = new_end
                            sp_corrected = True
                        print(f"标普指数：官方周频数据校正至 {all_data[name]['sp_official_end']}。")
                    all_data[name]["dates"] = dates
                    all_data[name]["values"] = values
                    # 从最新末点（官方周点或官方历史末点）起重新推算日频
                    recent_data = _extrapolate_sp_from_fund(
                        max(dates) if dates else None, values[-1] if values else None)
            elif not wind_done and source == "US":
                # 美股指数（如纳斯达克100）：Wind 不可用时回退腾讯行情日 K
                recent_data = fetch_us_history(code, last_saved_date)
            elif not wind_done:  # WIND：微盘为推算口径：固定锚点（准确数据最后一天）+ 代理指数最新收盘，
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
        if source == "SP":
            # 官方周频校正即使没带来新推算点（如周末运行），只要改写了数据
            # （清除推算点/官方点覆盖或追加），就必须写回并重算指标，
            # 否则校正结果会被“无变化不写回”机制丢弃，造成官方值永久丢失
            index_updated = sp_corrected
        
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

        # 万得微盘股指数：每日同步 PB 与市值中位数（同日覆盖）
        if source == "WIND":
            # Wind 可用时取官方加权 PB 与成份股总市值中位数；不可用时回退
            # 全市场「市值最小 400 只」自算（官方值截至 2026-11-05，之后为自算口径）。
            if _apply_micro_daily(all_data[name], all_data[name]["dates"][-1]):
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
                or previous_tracking.get("years_year") != tracking.get("years_year")
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

    # 自愈：剔除历史序列里晚于「该指数最新点位日」的脏点。当天尚未收盘时，Wind 会把
    # 上一笔成交标成当天日期；pepbHistory 只追加不覆盖，不清掉就会永久留着一个与
    # 前一日完全相同的平点，稀释估值分位（比价图也会多算一天）。
    for _name, _obj in all_data.items():
        _h = pepb_history.get(_history_sheet_name(_name))
        _last_pt = (_obj.get("dates") or [""])[-1]
        if not _h or not _h.get("dates") or not _last_pt:
            continue
        if len(_h.get("pe") or []) != len(_h["dates"]) or len(_h.get("pb") or []) != len(_h["dates"]):
            continue   # 序列长度不一致说明数据已损坏，跳过而不是继续裁
        _keep = [i for i, d in enumerate(_h["dates"]) if d <= _last_pt]
        if len(_keep) != len(_h["dates"]) and _keep:
            _n = len(_h["dates"]) - len(_keep)
            _h["dates"] = [_h["dates"][i] for i in _keep]
            _h["pe"] = [_h["pe"][i] for i in _keep]
            _h["pb"] = [_h["pb"][i] for i in _keep]
            history_changed = True
            print(f"{_name}：历史序列剔除 {_n} 个晚于点位末日的脏点（> {_last_pt}）")

    # 免费数据源指数（中证2000）：中证官网没有历史 PE/PB，估值分位与微盘拥挤度比值
    # 都需要一段历史。首次纳入工具时用 Wind 一次性补齐为「日频」（约 780 个交易日，
    # 约 8 次调用），之后每天追加，日常运行不再调用 Wind。
    # 阈值取 600：中证2000 2023-08 发布至今约 780 个交易日，补齐后必然超过它；
    # 只有历史被清空、或首次运行时 Wind 不可用导致没补成功，才会再次触发。
    FREE_ONLY_HISTORY_MIN = 600
    for _name in FREE_ONLY_INDICES:
        _key = _history_sheet_name(_name)
        _cached = pepb_history.get(_key) or {}
        if len(_cached.get("dates") or []) >= FREE_ONLY_HISTORY_MIN:
            continue
        _seed = seed_pepb_history_from_wind(
            _name, ((all_data.get(_name) or {}).get("dates") or ["2015-01-01"])[0])
        if _seed:
            _pe = dict(zip(_seed["dates"], _seed["pe"]))
            _pb = dict(zip(_seed["dates"], _seed["pb"]))
            for _d, _pev, _pbv in zip(_cached.get("dates") or [],
                                      _cached.get("pe") or [],
                                      _cached.get("pb") or []):
                _pe[_d], _pb[_d] = _pev, _pbv      # 已有（较新）的自算值优先
            _alld = sorted(_pe)
            pepb_history[_key] = {"dates": _alld,
                                  "pe": [_pe[d] for d in _alld],
                                  "pb": [_pb[d] for d in _alld]}
            history_changed = True

    for name, blocks in fetch_index_metrics(all_data).items():
        if name not in all_data:
            continue
        block = blocks["valuation"]

        cur_pe, cur_pb = block.get("pe_ttm"), block.get("pb")

        # 先把当日估值写进内嵌历史序列（同日期数值有变则纠正），再算分位。
        # 顺序很重要：反过来的话，当天重复运行会拿「当天第一次抓到的旧值」当样本，
        # 分位与估值表展示的收盘口径就对不上。
        if cur_pe and cur_pb and block.get("data_date"):
            if append_history_entry(pepb_history, name, block["data_date"],
                                    cur_pe, cur_pb):
                history_changed = True
                print(f"{name} 已写入/纠正 {block['data_date']} 估值至内嵌历史序列")

        # 近10年估值分位：当前整体法 PE/PB 在内嵌历史序列中的位置
        # （只对页面展示的指标算；红利类不再有 PE/PB，自然也不写分位）
        hist = pepb_history.get(_history_sheet_name(name)) or {}
        if hist.get("dates"):
            if cur_pe is not None:
                block["pe_percentile_10y"] = percentile_in_10y(
                    hist["dates"], hist["pe"], cur_pe)
            if cur_pb is not None:
                block["pb_percentile_10y"] = percentile_in_10y(
                    hist["dates"], hist["pb"], cur_pb)

        previous_valuation = all_data[name].get("valuation") or {}
        # 只比较实质字段，忽略每次都会变的 updated_at，避免无意义的提交
        if (
            previous_valuation.get("pe_ttm") != block.get("pe_ttm")
            or previous_valuation.get("pb") != block.get("pb")
            or previous_valuation.get("dividend_yield") != block.get("dividend_yield")
            or previous_valuation.get("weight_date") != block["weight_date"]
            or previous_valuation.get("source") != block["source"]
            or previous_valuation.get("data_date") != block["data_date"]
            or previous_valuation.get("pe_percentile_10y") != block.get("pe_percentile_10y")
            or previous_valuation.get("pb_percentile_10y") != block.get("pb_percentile_10y")
            or previous_valuation.get("valid_count") != block.get("valid_count")
        ):
            all_data[name]["valuation"] = block
            updated_any = True

        # 成分股画像：市值分布 + 行业分布 + 标普成份股周缓存
        profile = {
            "market_cap": blocks.get("market_cap") or {},
            "industry": blocks.get("industry") or {},
        }
        if blocks.get("components"):
            profile["components"] = blocks["components"]
        if blocks.get("market_cap") or blocks.get("industry") or blocks.get("components"):
            previous_profile = all_data[name].get("profile") or {}
            if (
                previous_profile.get("market_cap") != profile["market_cap"]
                or previous_profile.get("industry") != profile["industry"]
                or previous_profile.get("components") != profile.get("components")
            ):
                all_data[name]["profile"] = profile
                updated_any = True

    # A500 风格对比数据（大盘价值/成长）：独立内嵌块，随 Action 每日增量更新
    a500_js = build_a500_style_js(content)

    if updated_any or history_changed or a500_js:
        if updated_any:
            new_json = json.dumps(all_data, ensure_ascii=False, separators=(",", ":"))
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
    wind_usage_report()
    wind_failures_report()

def main(html_file="指数分析工具.html"):
    """跑一轮完整更新；若有 Wind 瞬时故障就等一会儿自动补抓一轮（最多一次）。"""
    if os.environ.get(WIND_AUTO_RETRY_ENV) == "1":
        print("\n" + "=" * 64)
        print("🔁 自动补抓轮：上一轮有 Wind 瞬时故障，本轮重新取一遍缺失数据")
        print("=" * 64)
    try:
        update_html(html_file)
    except Exception:
        # 任何未被兜住的异常：先把本轮已发生的 Wind 失败打出来（正常路径由
        # update_html 结尾统一打印），再原样抛出，保证 Actions 能正确标红。
        wind_failures_report()
        raise

    # 自动补抓：本轮存在「瞬时故障」导致的缺失、且 Wind 通道仍可用时，等一会儿再整体
    # 跑一轮（最多一次）。这样偶发抖动不用人工再点一次 Run workflow。
    # 写回逻辑是增量的，补抓只会把缺的那几天补上，不会重复拉历史。
    if (wind_has_retryable_failure() and wind_configured()
            and os.environ.get(WIND_AUTO_RETRY_ENV) != "1"):
        print(f"\n⏳ 本轮有 {len(_WIND_FAILURES)} 处 Wind 瞬时故障，"
              f"等 {WIND_AUTO_RETRY_DELAY}s 后自动补抓一轮（仅补一次）…")
        sys.stdout.flush()
        time.sleep(WIND_AUTO_RETRY_DELAY)
        env = dict(os.environ, **{WIND_AUTO_RETRY_ENV: "1"})
        os.execve(sys.executable,
                  [sys.executable, "-u", os.path.abspath(__file__)], env)


if __name__ == "__main__":
    main()

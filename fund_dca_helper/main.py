"""基金定投辅助工具。

程序每月运行一次，完成以下工作：
1. 优先通过 AkShare 查询三个指数近十年的 PE 分位；
2. AkShare 失败时，调用配置文件中的备用公开接口；
3. 根据沪深300 PE 分位计算三只基金的本月定投金额；
4. 更新本地 Excel 记录；
5. 生成简单直白的微信提醒，并优先通过 Server酱发送。

代码尽量使用普通 Python 写法，并保留中文注释，方便后续修改规则。
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import httpx
import yaml
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill


LOGGER = logging.getLogger("fund_dca_helper")

# 本工具统一使用以下估值口径：
# PE-TTM（滚动市盈率）、最近 10 年历史百分位、指数整体法、剔除 PE <= 0 的历史值。
UNIFIED_PE_DEFINITION = "PE-TTM，近10年历史百分位，整体法，剔除负值"


@dataclass
class Valuation:
    """一个指数的估值结果。percentile 的单位是百分比，例如 57.2。"""

    key: str
    name: str
    percentile: float
    source: str
    as_of: str = ""


def load_config(path: Path) -> dict[str, Any]:
    """读取 YAML 配置，并展开 ${环境变量} 占位符。"""
    text = path.read_text(encoding="utf-8")
    text = re.sub(
        r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}",
        lambda match: os.getenv(match.group(1), match.group(0)),
        text,
    )
    config = yaml.safe_load(text) or {}
    validate_config(config)
    return config


def validate_config(config: dict[str, Any]) -> None:
    """启动时检查容易写错的配置，避免金额悄悄算错。"""
    funds = config.get("funds", {})
    required = {"hs300", "chinext", "bond"}
    missing = required - funds.keys()
    if missing:
        raise ValueError(f"funds 缺少配置: {', '.join(sorted(missing))}")

    total_weight = sum(float(funds[key]["weight"]) for key in required)
    if abs(total_weight - 1) > 0.0001:
        raise ValueError(f"基金配置比例之和必须为 1，目前是 {total_weight}")

    if float(config.get("basic_monthly_amount", 0)) <= 0:
        raise ValueError("basic_monthly_amount 必须大于 0")

    if float(config.get("min_purchase_amount", 0)) < 0:
        raise ValueError("min_purchase_amount 不能小于 0")

    manual_value = config.get("portfolio", {}).get("manual_current_value", 0)
    if float(manual_value or 0) < 0:
        raise ValueError("portfolio.manual_current_value 不能小于 0")


def money(value: float) -> int:
    """定投金额按元取整，便于直接照着支付宝操作。"""
    return int(round(value))


def calculate_amounts(config: dict[str, Any], hs300_percentile: float | None) -> dict[str, int]:
    """按规则计算三只基金金额。

    正常档：500 * 40%/30%/30% = 200/150/150。
    低估档：三只基金都翻倍，默认总投入从 500 元增加到 1000 元，
    这是主动增加投入，不是程序计算错误。
    高估档：沪深300和创业板减半，释放出来的钱全部补给纯债。
    """
    amounts, _ = calculate_amounts_with_warnings(config, hs300_percentile)
    return amounts


def calculate_amounts_with_warnings(
    config: dict[str, Any],
    hs300_percentile: float | None,
) -> tuple[dict[str, int], list[str]]:
    """计算金额，并执行最低申购金额保护。

    低估时三只基金都按配置中的 low_multiplier 翻倍，默认总投入从 500 元
    增加到 1000 元。高估时股票类基金按 high_multiplier 减半，省下的钱
    放入纯债。所有倍数都来自配置文件，不在代码中写死。
    """
    total = float(config["basic_monthly_amount"])
    funds = config["funds"]
    rules = config["valuation_rules"]
    low = float(rules["low_threshold"])
    high = float(rules["high_threshold"])
    low_multiplier = float(rules["low_multiplier"])
    high_multiplier = float(rules["high_multiplier"])

    base = {
        key: total * float(funds[key]["weight"])
        for key in ("hs300", "chinext", "bond")
    }

    if hs300_percentile is None:
        raw_amounts = base
    elif hs300_percentile < low:
        raw_amounts = {
            "hs300": base["hs300"] * low_multiplier,
            "chinext": base["chinext"] * low_multiplier,
            "bond": base["bond"] * low_multiplier,
        }
    elif hs300_percentile > high:
        hs300 = money(base["hs300"] * high_multiplier)
        chinext = money(base["chinext"] * high_multiplier)
        # 用总额减去两只股票基金，避免四舍五入后总金额不等于预算。
        raw_amounts = {
            "hs300": hs300,
            "chinext": chinext,
            "bond": total - hs300 - chinext,
        }
    else:
        raw_amounts = base

    minimum = float(config.get("min_purchase_amount", 0))
    amounts: dict[str, int] = {}
    warnings: list[str] = []
    for key, raw_amount in raw_amounts.items():
        # 先比较未取整的计算金额，避免 0.4 元先四舍五入成 0 元后漏掉保护。
        if 0 < raw_amount < minimum:
            adjusted = money(minimum)
            amounts[key] = adjusted
            warnings.append(
                f"注意：{funds[key]['name']}因减半后金额过低，"
                f"已自动调整为最低申购金额{adjusted}元"
            )
        else:
            amounts[key] = money(raw_amount)
    return amounts, warnings


def valuation_description(percentile: float | None, config: dict[str, Any]) -> tuple[str, str]:
    """返回消息中使用的估值状态和操作建议。"""
    if percentile is None:
        return "估值数据获取失败", "按正常金额投就行"

    rules = config["valuation_rules"]
    if percentile < float(rules["low_threshold"]):
        return "估值偏低，加倍投！", "沪深300和创业板加倍"
    if percentile > float(rules["high_threshold"]):
        return "估值偏高，少投点！", "沪深300和创业板减半，省下的钱加到纯债"
    return "估值正常", "按正常金额投"


def _number(value: Any) -> float | None:
    """把接口返回的数字、百分数字符串统一转成 float。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))
    return float(match.group(0)) if match else None


def _normal_key(key: Any) -> str:
    return re.sub(r"[\s_()%（）\-]", "", str(key)).lower()


def _date_value(record: dict[str, Any]) -> str:
    for key, value in record.items():
        normalized = _normal_key(key)
        if any(token in normalized for token in ("date", "日期", "时间", "交易日")):
            return str(value)
    return ""


def _iter_dicts(value: Any) -> Iterable[dict[str, Any]]:
    """递归遍历 JSON，兼容不同公开接口的嵌套结构。"""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _iter_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_dicts(child)


def _extract_overall_pe_ttm_history(
    records: Iterable[dict[str, Any]],
) -> list[tuple[str, float]]:
    """提取整体法 PE-TTM 历史值，不混入等权、静态 PE 或 PEG。

    AkShare 底层接口同时返回 ttmPe 和 addTtmPe：
    ttmPe 是等权滚动市盈率，addTtmPe 才是整体法滚动市盈率。
    因此必须优先读取 addTtmPe；只有已经被接口转换成“滚动市盈率”的记录，
    才读取对应中文字段。
    """
    result: list[tuple[str, float]] = []
    for record in records:
        pe = None
        # 先取整体法字段，避免按返回顺序误取等权 ttmPe。
        for key in ("addTtmPe", "rollingPe", "滚动市盈率", "滚动市盈率TTM", "市盈率TTM"):
            if key in record:
                pe = _number(record[key])
                break
        if pe is not None and math.isfinite(pe) and pe > 0:
            result.append((_date_value(record), pe))
    return result


def _calculate_pe_ttm_percentile(
    history: Iterable[tuple[str, float]],
    as_of: date,
) -> tuple[float, str]:
    """按统一口径计算 PE-TTM 近10年历史百分位。

    采用指数整体法的 PE-TTM 历史序列；只取截至 as_of 的最近 10 年数据，
    剔除 PE <= 0、空值和非有限数。百分位 = 历史中小于等于当前 PE 的样本占比。
    """
    start = as_of.replace(year=as_of.year - 10)
    valid: list[tuple[date, float]] = []
    for raw_date, pe in history:
        try:
            item_date = datetime.fromisoformat(str(raw_date)[:10]).date()
        except ValueError:
            continue
        if start <= item_date <= as_of and math.isfinite(pe) and pe > 0:
            valid.append((item_date, pe))
    if not valid:
        raise ValueError(f"没有可用的 {UNIFIED_PE_DEFINITION} 历史数据")
    valid.sort(key=lambda item: item[0])
    latest_date, latest_pe = valid[-1]
    percentile = sum(pe <= latest_pe for _, pe in valid) / len(valid) * 100
    return percentile, latest_date.isoformat()


def _extract_csindex_pe_ttm_history(records: Iterable[dict[str, Any]]) -> list[tuple[str, float]]:
    """提取中证指数官网接口的 PE-TTM 历史值。

    中证接口字段名为 peg，但 AkShare 对同一公开接口的解析将该字段标注为
    “滚动市盈率”。这里把它显式转换为 PE-TTM 使用；不把它当作 PEG 指标。
    该接口的指数估值为官方指数整体口径，之后仍按统一的近10年和剔除负值规则重算。
    """
    result: list[tuple[str, float]] = []
    for record in records:
        pe_ttm = _number(record.get("peg"))
        if pe_ttm is not None and math.isfinite(pe_ttm) and pe_ttm > 0:
            result.append((_date_value(record), pe_ttm))
    return result


def _extract_html_percentile(html: str) -> float | None:
    """从公开指数页面中提取“当前分位”百分比。"""
    match = re.search(
        r"当前分位.{0,800}?>(\d+(?:\.\d+)?)%</span>",
        html,
        flags=re.DOTALL,
    )
    return float(match.group(1)) if match else None


def load_trade_dates() -> set[date]:
    """读取 A 股交易日历。

    优先调用需求指定的 ak.tool_trade_date_hist_sz()。当前部分 AkShare
    版本尚未提供该函数，因此兼容使用同样返回 A 股交易日的
    ak.tool_trade_date_hist_sina()，不改变交易日判断规则。
    """
    import akshare as ak

    calendar_func = getattr(ak, "tool_trade_date_hist_sz", None)
    if calendar_func is None:
        calendar_func = getattr(ak, "tool_trade_date_hist_sina", None)
    if calendar_func is None:
        raise RuntimeError("当前 AkShare 版本没有可用的 A 股交易日历接口")

    frame = calendar_func()
    date_column = next(
        (column for column in frame.columns if _normal_key(column) in {"tradedate", "交易日"}),
        None,
    )
    if date_column is None:
        raise ValueError("交易日历返回结果中找不到 trade_date/交易日 字段")

    dates: set[date] = set()
    for value in frame[date_column].tolist():
        if isinstance(value, datetime):
            dates.add(value.date())
        elif isinstance(value, date):
            dates.add(value)
        else:
            try:
                dates.add(datetime.fromisoformat(str(value)[:10]).date())
            except ValueError:
                continue
    if not dates:
        raise ValueError("交易日历为空")
    return dates


def first_trading_day_of_month(run_date: date, trade_dates: set[date]) -> date:
    """返回指定月份的第一个 A 股交易日。"""
    month_dates = [
        item for item in trade_dates
        if item.year == run_date.year and item.month == run_date.month
    ]
    if not month_dates:
        raise ValueError(f"交易日历中找不到 {run_date:%Y-%m} 的交易日")
    return min(month_dates)


def is_first_trading_day(run_date: date, trade_dates: set[date]) -> tuple[bool, date]:
    """判断今天是否是本月第一个交易日，并返回该交易日日期。"""
    first_day = first_trading_day_of_month(run_date, trade_dates)
    return run_date == first_day, first_day


class ValuationFetcher:
    """估值查询适配器：AkShare 优先，公开接口备用。"""

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.indexes = config["index_symbols"]

    def fetch_all(self, as_of: date | None = None) -> dict[str, Valuation]:
        results: dict[str, Valuation] = {}
        for key in ("hs300", "chinext", "csi500"):
            try:
                results[key] = self.fetch_one(key, as_of)
            except Exception as exc:
                LOGGER.warning("%s 估值查询失败: %s", key, exc)
        return results

    def fetch_one(self, key: str, as_of: date | None = None) -> Valuation:
        index = self.indexes[key]
        try:
            return self._fetch_with_akshare(key, index, as_of)
        except Exception as ak_exc:
            LOGGER.warning("%s AkShare 查询失败，尝试备用接口: %s", key, ak_exc)
            if not self.config.get("fallback", {}).get("enabled", True):
                raise
            return self._fetch_with_fallback(key, index, as_of)

    def _fetch_with_akshare(
        self,
        key: str,
        index: dict[str, Any],
        as_of: date | None = None,
    ) -> Valuation:
        """通过 AkShare 的指数 PE 数据源获取整体法 PE-TTM 历史。

        AkShare 底层返回 ttmPe（等权滚动市盈率）和 addTtmPe（整体法滚动市盈率）。
        本工具只读取 addTtmPe，把它作为整体法 PE-TTM；不读取 ttmPe、静态 PE 或中位数 PE。
        最后统一按近10年、剔除 PE <= 0 的规则计算历史百分位。
        """
        import requests
        from akshare.stock_feature import stock_a_pe_and_pb as pe_module
        from py_mini_racer import MiniRacer

        index_code = index.get("akshare_index_code")
        if not index_code:
            raise ValueError("缺少 AkShare PE-TTM 指数代码配置")

        js_engine = MiniRacer()
        js_engine.eval(pe_module.hash_code)
        token = js_engine.call("hex", date.today().isoformat()).lower()
        source_url = "https://legulegu.com/api/stockdata/index-basic-pe"
        response = requests.get(
            source_url,
            params={"token": token, "indexCode": index_code},
            timeout=float(self.config["fallback"].get("timeout_seconds", 20)),
            **pe_module.get_cookie_csrf(
                url="https://legulegu.com/stockdata/sz50-ttm-lyr"
            ),
        )
        response.raise_for_status()
        history = _extract_overall_pe_ttm_history(response.json().get("data", []))
        if not history:
            raise ValueError(f"AkShare 返回结果中找不到符合 {UNIFIED_PE_DEFINITION} 的历史值")
        percentile, latest_date = _calculate_pe_ttm_percentile(
            history,
            as_of or date.today(),
        )
        return Valuation(
            key,
            self._display_name(key),
            round(percentile, 2),
            "AkShare PE-TTM整体法",
            latest_date,
        )

    def _fetch_with_fallback(
        self,
        key: str,
        index: dict[str, Any],
        as_of: date | None = None,
    ) -> Valuation:
        fallback = self.config["fallback"]
        today = as_of or date.today()
        start_date = today.replace(year=today.year - 10).strftime("%Y%m%d")
        end_date = today.strftime("%Y%m%d")
        timeout = float(fallback.get("timeout_seconds", 20))
        response = httpx.get(
            fallback["url_template"],
            params={
                "indexCode": index["index_code"],
                "startDate": start_date,
                "endDate": end_date,
            },
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "fund-dca-helper/1.0"},
        )
        response.raise_for_status()
        payload = response.json()
        records = list(_iter_dicts(payload))
        # 中证接口的 peg 字段实际对应“滚动市盈率”，这里显式转换为 PE-TTM。
        # 不使用接口中的静态 PE、PEG 或现成的非统一百分位字段。
        history = _extract_csindex_pe_ttm_history(records)
        if history:
            percentile, latest_date = _calculate_pe_ttm_percentile(history, today)
            return Valuation(
                key,
                self._display_name(key),
                round(percentile, 2),
                "中证指数PE-TTM整体法",
                latest_date,
            )
        if not fallback.get("secondary_enabled", True):
            raise ValueError(f"备用接口返回结果中找不到符合 {UNIFIED_PE_DEFINITION} 的历史值")
        return self._fetch_with_secondary(key, index)

    def _fetch_with_secondary(self, key: str, index: dict[str, Any]) -> Valuation:
        """处理官方接口没有覆盖的指数。

        该公开页面提供的是等权 PE-TTM，不是本工具要求的整体法 PE-TTM。
        因此即使页面有百分位，也不能直接使用，必须拒绝混用并进入失败保底提醒。
        """
        fallback = self.config["fallback"]
        url = fallback["secondary_url_template"].format(
            fallback_market=index.get("fallback_market", "SZSE"),
            index_code=index["index_code"],
            key=key,
        )
        response = httpx.get(
            url,
            timeout=float(fallback.get("timeout_seconds", 20)),
            follow_redirects=True,
            headers={"User-Agent": "fund-dca-helper/1.0"},
        )
        response.raise_for_status()
        html = response.content.decode("utf-8", errors="replace")
        if _extract_html_percentile(html) is None:
            raise ValueError("备用指数页面中找不到 PE-TTM 当前分位")
        raise ValueError("备用指数页面仅提供等权 PE-TTM，不符合整体法口径，已拒绝混用")

    @staticmethod
    def _display_name(key: str) -> str:
        return {"hs300": "沪深300", "chinext": "创业板指", "csi500": "中证500"}[key]


def format_percentile(value: float | None) -> str:
    return "未知" if value is None else f"{value:.0f}%"


def format_chinese_date(value: date) -> str:
    """生成兼容 Windows 和 Linux 的中文日期，避免使用 %-m 这类平台格式。"""
    return f"{value.year}年{value.month}月{value.day}日"


def build_investment_message(
    run_date: date,
    config: dict[str, Any],
    valuations: dict[str, Valuation],
    amounts: dict[str, int],
    purchase_warnings: list[str] | None = None,
) -> str:
    """拼接用户可以直接照做的微信消息。"""
    hs300 = valuations.get("hs300")
    hs300_percentile = hs300.percentile if hs300 else None
    status, advice = valuation_description(hs300_percentile, config)
    funds = config["funds"]

    if hs300 is None:
        lines = [
            f"📋 今日定投提醒（{format_chinese_date(run_date)}）",
            "",
            "当前估值数据获取失败",
            "操作建议：按正常金额投就行",
            "",
            "去支付宝分别买入：",
        ]
    else:
        lines = [
            f"📋 今日定投提醒（{format_chinese_date(run_date)}）",
            "",
            f"当前沪深300 PE百分位：{format_percentile(hs300_percentile)}，{status}",
            f"操作建议：{advice}",
            "",
            "去支付宝分别买入：",
        ]

        if hs300_percentile < float(config["valuation_rules"]["low_threshold"]):
            lines.extend(
                [
                    "",
                    f"本月大盘便宜，总投入增加到{sum(amounts.values())}元，多投多赚~",
                ]
            )

    for key in ("hs300", "chinext", "bond"):
        fund = funds[key]
        lines.append(f"• {fund['name']}（{fund['code']}）{amounts[key]}元")
    if purchase_warnings:
        lines.extend(["", *purchase_warnings])
    if hs300_percentile is not None and hs300_percentile > float(
        config["valuation_rules"]["high_threshold"]
    ):
        lines.extend(["", "大盘有点贵，少买股票多买债，稳住就行~"])
    else:
        lines.extend(["", "记得操作哦~"])

    csi500 = valuations.get("csi500")
    monitor = config["monitor"]
    if csi500 and csi500.percentile < float(monitor["csi500_cheap_threshold"]):
        lines.extend(
            [
                "",
                f"📌 中证500现在很便宜（PE百分位{csi500.percentile:.0f}%），可以考虑加入定投组合",
                "建议配置比例：沪深300 30%、创业板 30%、中证500 20%、纯债 20%",
            ]
        )
    elif csi500 and csi500.percentile <= float(monitor["csi500_watch_threshold"]):
        lines.extend(
            [
                "",
                f"📌 中证500估值开始回落到合理区间（PE百分位{csi500.percentile:.0f}%），"
                f"可以继续观察，等跌到{float(monitor['csi500_cheap_threshold']):.0f}%以下再考虑加入",
            ]
        )
    return "\n".join(lines)


def build_manual_value_reminder(config: dict[str, Any]) -> str:
    """生成每月都要附在消息末尾的市值填写提醒。"""
    manual_value = float(config.get("portfolio", {}).get("manual_current_value", 0) or 0)
    lines: list[str] = []
    if manual_value <= 0:
        lines.append("你还没填写当前市值，止盈提醒暂停")
    lines.append("请在配置文件中更新你的当前总市值（manual_current_value字段），否则止盈提醒无法生效")
    return "\n\n".join(lines)


def _ensure_workbook(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "定投记录"
    headers = [
        "月份",
        "沪深300PE百分位",
        "创业板指PE百分位",
        "中证500PE百分位",
        "估值数据来源",
        "沪深300投入",
        "创业板投入",
        "纯债投入",
        "当月投入合计",
        "累计投入",
        "当前总市值",
        "总收益率",
        "备注",
    ]
    sheet.append(headers)
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="4472C4")
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = "A1:M1"
    workbook.save(path)


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_alert_history(path: Path) -> dict[str, bool]:
    """读取止盈提醒历史；文件不存在时使用尚未提醒过的默认状态。"""
    default = {"20_percent_alerted": False, "30_percent_alerted": False}
    if not path.exists():
        return default
    saved = json.loads(path.read_text(encoding="utf-8"))
    return {
        key: bool(saved.get(key, value))
        for key, value in default.items()
    }


def _save_alert_history(path: Path, history: dict[str, bool]) -> None:
    """把已成功推送过的止盈提醒写入仓库中的 JSON 文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")


def update_workbook(
    path: Path,
    run_date: date,
    config: dict[str, Any],
    valuations: dict[str, Valuation],
    amounts: dict[str, int],
) -> dict[str, Any]:
    """新增或更新当月记录，并返回收益统计所需的信息。"""
    _ensure_workbook(path)
    workbook = load_workbook(path)
    sheet = workbook["定投记录"]
    month = run_date.strftime("%Y-%m")

    existing_row = None
    for row in range(2, sheet.max_row + 1):
        if sheet.cell(row, 1).value == month:
            existing_row = row
            break
    row = existing_row or sheet.max_row + 1

    previous_total = 0.0
    for row_number in range(2, sheet.max_row + 1):
        if row_number != row:
            previous_total += float(sheet.cell(row_number, 9).value or 0)
    monthly_total = sum(amounts.values())
    cumulative = previous_total + monthly_total
    market_value = config.get("portfolio", {}).get("manual_current_value", 0)
    market_value = float(market_value or 0)
    if market_value <= 0:
        market_value = None
    # 本工具未考虑基金分红再投资的影响，收益率仅供参考，实际收益以支付宝显示为准。
    return_rate = (
        round((market_value / cumulative - 1) * 100, 2)
        if market_value is not None and cumulative
        else None
    )

    values = [
        month,
        valuations.get("hs300").percentile if valuations.get("hs300") else None,
        valuations.get("chinext").percentile if valuations.get("chinext") else None,
        valuations.get("csi500").percentile if valuations.get("csi500") else None,
        ", ".join(sorted({item.source for item in valuations.values()})) or "失败",
        amounts["hs300"],
        amounts["chinext"],
        amounts["bond"],
        monthly_total,
        cumulative,
        market_value,
        return_rate,
        "估值查询失败时按正常金额计算" if "hs300" not in valuations else "",
    ]
    for column, value in enumerate(values, 1):
        sheet.cell(row, column).value = value
    workbook.save(path)
    return {
        "monthly_total": monthly_total,
        "cumulative": cumulative,
        "market_value": market_value,
        "return_rate": return_rate,
        "row_count": sheet.max_row - 1,
    }


def build_extra_alerts(
    config: dict[str, Any],
    stats: dict[str, Any],
    workbook_path: Path,
    state_path: Path,
    alert_history_path: Path,
    run_date: date,
) -> tuple[list[str], dict[str, Any], dict[str, bool]]:
    """生成与当月定投提醒合并发送的季度报告和止盈提醒。

    manual_current_value 为 0 时，update_workbook 会把 return_rate 设为 None，
    因此这里会自动跳过止盈判断和季度收益报告。
    """
    alerts: list[str] = []
    state = _load_state(state_path)
    alert_history = _load_alert_history(alert_history_path)
    return_rate = stats["return_rate"]

    if return_rate is not None:
        first = float(config["monitor"]["take_profit_first_threshold"])
        second = float(config["monitor"]["take_profit_second_threshold"])
        reset = float(config["monitor"]["take_profit_first_reset_threshold"])
        # 20% 止盈提醒只有首次达到时发送。收益率必须先跌破 15%（可在配置调整）
        # 才会重新允许下一次达到 20% 时提醒；30% 提醒始终只发送一次。
        if return_rate < reset:
            alert_history["20_percent_alerted"] = False
        if return_rate >= second and not alert_history["30_percent_alerted"]:
            alerts.append(
                f"你的基金总收益已经达到{second:.0f}%了，建议再卖出1/3的股票类基金，剩余1/3继续持有"
            )
            alert_history["30_percent_alerted"] = True
            alert_history["20_percent_alerted"] = True
        elif return_rate >= first and not alert_history["20_percent_alerted"]:
            alerts.append(
                f"你的基金总收益已经达到{first:.0f}%了，建议卖出1/3的股票类基金落袋为安，钱转到纯债基金里"
            )
            alert_history["20_percent_alerted"] = True

    every = int(config["monitor"]["quarter_report_every_months"])
    current_month = run_date.strftime("%Y-%m")
    if (
        run_date.month in (3, 6, 9, 12)
        and stats["market_value"] is not None
        and state.get("last_quarter_report_month") != current_month
    ):
        workbook = load_workbook(workbook_path, read_only=True)
        sheet = workbook["定投记录"]
        start = max(2, sheet.max_row - every + 1)
        invested = sum(float(sheet.cell(row, 9).value or 0) for row in range(start, sheet.max_row + 1))
        profit = stats["market_value"] - stats["cumulative"]
        rate = profit / stats["cumulative"] * 100 if stats["cumulative"] else 0
        alerts.append(
            f"过去{every}个月你一共投入{money(invested)}元，当前总市值{money(stats['market_value'])}元，"
            f"赚了{money(profit)}元，收益率{rate:.1f}%，继续加油~\n"
            "（注：收益率未考虑分红影响，仅供参考）"
        )
        state["last_quarter_report_month"] = current_month
    return alerts, state, alert_history


def send_serverchan(config: dict[str, Any], title: str, desp: str) -> bool:
    """通过 Server酱发送消息；调用方负责在失败时切换到 PushPlus。"""
    sendkey = str(config.get("serverchan", {}).get("sendkey", "")).strip()
    if not sendkey or sendkey.startswith("${"):
        return False
    endpoint = config["serverchan"]["endpoint"].format(sendkey=sendkey)
    response = httpx.post(endpoint, data={"title": title, "desp": desp}, timeout=20)
    response.raise_for_status()
    payload = response.json()
    if payload.get("code", 0) != 0:
        raise RuntimeError(f"Server酱返回失败: {payload}")
    return True


def send_pushplus(config: dict[str, Any], title: str, content: str) -> bool:
    """通过 PushPlus 发送 Markdown 消息，作为 Server酱的备选渠道。"""
    token = str(config.get("pushplus_token", "")).strip()
    if not token or token.startswith("${"):
        return False
    response = httpx.post(
        "https://www.pushplus.plus/send",
        json={
            "token": token,
            "title": title,
            "content": content,
            "template": "markdown",
        },
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    if str(payload.get("code")) != "200":
        raise RuntimeError(f"PushPlus 返回失败: {payload}")
    return True


def send_notification(config: dict[str, Any], title: str, message: str) -> bool:
    """优先 Server酱，失败时仅在两个渠道都配置时切换到 PushPlus。"""
    serverchan_configured = bool(
        str(config.get("serverchan", {}).get("sendkey", "")).strip()
        and not str(config.get("serverchan", {}).get("sendkey", "")).strip().startswith("${")
    )
    pushplus_configured = bool(
        str(config.get("pushplus_token", "")).strip()
        and not str(config.get("pushplus_token", "")).strip().startswith("${")
    )

    if serverchan_configured:
        try:
            if send_serverchan(config, title, message):
                return True
            if not pushplus_configured:
                return False
            LOGGER.warning("Server酱未返回成功，切换到 PushPlus")
            return send_pushplus(config, title, message)
        except Exception as exc:
            if not pushplus_configured:
                raise
            LOGGER.warning("Server酱推送失败，切换到 PushPlus: %s", exc)
            return send_pushplus(config, title, message)
    if pushplus_configured:
        return send_pushplus(config, title, message)

    LOGGER.warning("未配置 SERVERCHAN_SENDKEY 或 PUSHPLUS_TOKEN，本次不发送推送")
    return False


def parse_run_date(value: str | None) -> date:
    return (
        datetime.strptime(value, "%Y-%m-%d").date()
        if value
        else datetime.now(ZoneInfo("Asia/Shanghai")).date()
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="基金定投辅助工具")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--as-of", help="测试用日期，格式 YYYY-MM-DD")
    parser.add_argument("--no-push", action="store_true", help="只计算和记录，不推送微信")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Windows 终端常见默认编码是 GBK，消息里的中文标记和 emoji 可能导致打印失败。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    root = Path(__file__).resolve().parent
    config_path = (root / args.config).resolve() if not Path(args.config).is_absolute() else Path(args.config)
    config = load_config(config_path)
    run_date = parse_run_date(args.as_of)

    # 工作流每月 1-5 号都会触发，但只有本月第一个 A 股交易日才真正运行。
    # 因此 1 号是周末或节假日时，后续工作日会自然接棒执行。
    try:
        trade_dates = load_trade_dates()
        should_run, first_trade_day = is_first_trading_day(run_date, trade_dates)
    except Exception as exc:
        LOGGER.error("交易日历获取失败，本次不执行定投提醒: %s", exc)
        return 1
    if not should_run:
        print(
            f"{format_chinese_date(run_date)}不是本月第一个A股交易日，"
            f"本月第一个交易日是{format_chinese_date(first_trade_day)}，本次跳过。"
        )
        return 0

    fetcher = ValuationFetcher(config)
    valuations = fetcher.fetch_all(run_date)
    hs300_percentile = valuations["hs300"].percentile if "hs300" in valuations else None
    amounts, purchase_warnings = calculate_amounts_with_warnings(config, hs300_percentile)

    storage = config["storage"]
    workbook_path = root / storage["workbook"]
    state_path = root / storage["state_file"]
    alert_history_path = root / storage["alert_history_file"]
    stats = update_workbook(workbook_path, run_date, config, valuations, amounts)
    message = build_investment_message(
        run_date,
        config,
        valuations,
        amounts,
        purchase_warnings,
    )
    extra_alerts, next_state, next_alert_history = build_extra_alerts(
        config,
        stats,
        workbook_path,
        state_path,
        alert_history_path,
        run_date,
    )
    if extra_alerts:
        message += "\n\n" + "\n\n".join(extra_alerts)
    message += "\n\n" + build_manual_value_reminder(config)

    print(message)
    if not args.no_push:
        # 只有消息成功到达任一渠道后，才记录季度和止盈提醒状态，避免漏提醒。
        if send_notification(config, f"今日定投提醒 {run_date:%Y-%m-%d}", message):
            _save_state(state_path, next_state)
            _save_alert_history(alert_history_path, next_alert_history)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

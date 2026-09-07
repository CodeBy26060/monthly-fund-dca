import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datetime import date

from main import (
    Valuation,
    _extract_html_percentile,
    _extract_overall_pe_ttm_history,
    _calculate_pe_ttm_percentile,
    _save_alert_history,
    build_extra_alerts,
    build_investment_message,
    build_manual_value_reminder,
    calculate_amounts,
    calculate_amounts_with_warnings,
    is_first_trading_day,
    send_notification,
    update_workbook,
)


CONFIG = {
    "basic_monthly_amount": 500,
    "funds": {
        "hs300": {"weight": 0.4},
        "chinext": {"weight": 0.3},
        "bond": {"weight": 0.3},
    },
    "valuation_rules": {
        "low_threshold": 30,
        "high_threshold": 70,
        "low_multiplier": 2,
        "high_multiplier": 0.5,
    },
    "min_purchase_amount": 10,
}


def test_normal_amounts():
    assert calculate_amounts(CONFIG, 57) == {"hs300": 200, "chinext": 150, "bond": 150}


def test_low_amounts_match_user_example():
    assert calculate_amounts(CONFIG, 25) == {"hs300": 400, "chinext": 300, "bond": 300}


def test_high_amounts_move_cash_to_bond():
    assert calculate_amounts(CONFIG, 75) == {"hs300": 100, "chinext": 75, "bond": 325}


def test_overall_pe_ttm_history_is_kept_and_negative_values_are_removed():
    records = [
        {"date": "2018-01-01", "addTtmPe": 10, "ttmPe": 99},
        {"date": "2019-01-01", "addTtmPe": -2, "ttmPe": 99},
        {"date": "2020-01-01", "lyrPe": 999},
        {"date": "2021-01-01", "addTtmPe": 20, "ttmPe": 99},
    ]
    assert _extract_overall_pe_ttm_history(records) == [
        ("2018-01-01", 10),
        ("2021-01-01", 20),
    ]


def test_pe_ttm_percentile_uses_recent_ten_years():
    history = [
        ("2014-01-01", 1),
        ("2017-01-01", 2),
        ("2020-01-01", 3),
        ("2024-01-01", 4),
    ]
    percentile, latest = _calculate_pe_ttm_percentile(history, date(2024, 1, 2))
    assert percentile == 100
    assert latest == "2024-01-01"


def test_html_percentile_parser():
    html = '<span>当前分位</span><span class="value">56.95%</span>'
    assert _extract_html_percentile(html) == 56.95


def test_low_message_explains_total_increases_to_1000():
    config = {
        **CONFIG,
        "monitor": {"csi500_cheap_threshold": 30, "csi500_watch_threshold": 50},
        "funds": {
            "hs300": {"name": "沪深300", "code": "007339", "weight": 0.4},
            "chinext": {"name": "创业板", "code": "004744", "weight": 0.3},
            "bond": {"name": "纯债", "code": "070009", "weight": 0.3},
        },
    }
    message = build_investment_message(
        date(2026, 10, 1),
        config,
        {"hs300": Valuation("hs300", "沪深300", 25, "test")},
        calculate_amounts(config, 25),
    )
    assert "本月大盘便宜，总投入增加到1000元，多投多赚~" in message


def test_high_message_matches_the_reduced_stock_investment_example():
    config = {
        **CONFIG,
        "monitor": {"csi500_cheap_threshold": 30, "csi500_watch_threshold": 50},
        "funds": {
            "hs300": {"name": "沪深300", "code": "007339", "weight": 0.4},
            "chinext": {"name": "创业板", "code": "004744", "weight": 0.3},
            "bond": {"name": "纯债", "code": "070009", "weight": 0.3},
        },
    }
    message = build_investment_message(
        date(2026, 10, 1),
        config,
        {"hs300": Valuation("hs300", "沪深300", 75, "test")},
        calculate_amounts(config, 75),
    )
    assert "当前沪深300 PE百分位：75%，估值偏高，少投点！" in message
    assert "操作建议：沪深300和创业板减半，省下的钱加到纯债" in message
    assert "大盘有点贵，少买股票多买债，稳住就行~" in message


def test_minimum_purchase_amount_warning_is_returned():
    config = {
        **CONFIG,
        "basic_monthly_amount": 20,
        "funds": {
            "hs300": {"name": "沪深300", "code": "007339", "weight": 0.4},
            "chinext": {"name": "创业板", "code": "004744", "weight": 0.3},
            "bond": {"name": "纯债", "code": "070009", "weight": 0.3},
        },
    }
    amounts, warnings = calculate_amounts_with_warnings(config, 80)
    # 股票类基金触发最低申购金额保护，余下预算继续留在纯债。
    assert amounts == {"hs300": 10, "chinext": 10, "bond": 13}
    assert any(
        "创业板因减半后金额过低，已自动调整为最低申购金额10元" in warning
        for warning in warnings
    )


def test_trade_day_gate():
    trade_dates = {date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 5)}
    assert is_first_trading_day(date(2026, 10, 1), trade_dates) == (True, date(2026, 10, 1))
    assert is_first_trading_day(date(2026, 10, 2), trade_dates) == (False, date(2026, 10, 1))


def test_zero_manual_value_pauses_profit_alert():
    config = {**CONFIG, "portfolio": {"manual_current_value": 0}}
    reminder = build_manual_value_reminder(config)
    assert "你还没填写当前市值，止盈提醒暂停" in reminder
    assert "请在配置文件中更新你的当前总市值（manual_current_value字段），否则止盈提醒无法生效" in reminder


def test_positive_manual_value_only_keeps_update_reminder():
    config = {**CONFIG, "portfolio": {"manual_current_value": 1580}}
    reminder = build_manual_value_reminder(config)
    assert "你还没填写当前市值，止盈提醒暂停" not in reminder
    assert "manual_current_value字段" in reminder


def test_manual_value_calculates_return_and_creates_workbook(tmp_path):
    config = {**CONFIG, "portfolio": {"manual_current_value": 600}}
    valuations = {"hs300": Valuation("hs300", "沪深300", 57, "test")}
    stats = update_workbook(
        tmp_path / "data" / "fund_records.xlsx",
        date(2026, 9, 6),
        config,
        valuations,
        calculate_amounts(config, 57),
    )
    assert stats["market_value"] == 600
    assert stats["cumulative"] == 500
    assert stats["return_rate"] == 20


def test_zero_manual_value_skips_return_calculation(tmp_path):
    config = {**CONFIG, "portfolio": {"manual_current_value": 0}}
    stats = update_workbook(
        tmp_path / "fund_records.xlsx",
        date(2026, 9, 6),
        config,
        {},
        calculate_amounts(config, None),
    )
    assert stats["market_value"] is None
    assert stats["return_rate"] is None


def test_take_profit_alert_is_rearmed_only_after_falling_below_reset_line(tmp_path):
    config = {
        **CONFIG,
        "monitor": {
            "take_profit_first_threshold": 20,
            "take_profit_second_threshold": 30,
            "take_profit_first_reset_threshold": 15,
            "quarter_report_every_months": 3,
        },
    }
    stats = {"return_rate": 25, "market_value": 1000, "cumulative": 800, "row_count": 1}
    state_path = tmp_path / "state.json"
    history_path = tmp_path / "alert_history.json"
    workbook_path = tmp_path / "fund_records.xlsx"

    alerts, _, history = build_extra_alerts(
        config, stats, workbook_path, state_path, history_path, date(2026, 10, 1)
    )
    assert len(alerts) == 1
    assert history["20_percent_alerted"] is True
    _save_alert_history(history_path, history)

    alerts, _, history = build_extra_alerts(
        config,
        {**stats, "return_rate": 18},
        workbook_path,
        state_path,
        history_path,
        date(2026, 10, 1),
    )
    assert alerts == []
    assert history["20_percent_alerted"] is True
    _save_alert_history(history_path, history)

    alerts, _, history = build_extra_alerts(
        config,
        {**stats, "return_rate": 14},
        workbook_path,
        state_path,
        history_path,
        date(2026, 10, 1),
    )
    assert alerts == []
    assert history["20_percent_alerted"] is False
    _save_alert_history(history_path, history)

    alerts, _, _ = build_extra_alerts(
        config,
        {**stats, "return_rate": 20},
        workbook_path,
        state_path,
        history_path,
        date(2026, 10, 1),
    )
    assert len(alerts) == 1


def test_quarter_report_is_only_added_in_calendar_quarter_months(tmp_path):
    config = {
        **CONFIG,
        "portfolio": {"manual_current_value": 550},
        "monitor": {
            "take_profit_first_threshold": 20,
            "take_profit_second_threshold": 30,
            "take_profit_first_reset_threshold": 15,
            "quarter_report_every_months": 3,
        },
    }
    workbook_path = tmp_path / "fund_records.xlsx"
    stats = update_workbook(
        workbook_path,
        date(2026, 3, 2),
        config,
        {},
        calculate_amounts(config, 57),
    )
    alerts, _, _ = build_extra_alerts(
        config,
        stats,
        workbook_path,
        tmp_path / "state.json",
        tmp_path / "alert_history.json",
        date(2026, 3, 2),
    )
    assert any("过去3个月" in alert for alert in alerts)
    assert any("收益率未考虑分红影响，仅供参考" in alert for alert in alerts)

    alerts, _, _ = build_extra_alerts(
        config,
        stats,
        workbook_path,
        tmp_path / "state.json",
        tmp_path / "alert_history.json",
        date(2026, 4, 1),
    )
    assert not any("过去3个月" in alert for alert in alerts)


def test_serverchan_failure_falls_back_to_pushplus(monkeypatch):
    import main

    calls = []

    def fail_serverchan(*args, **kwargs):
        raise RuntimeError("Server酱异常")

    def succeed_pushplus(*args, **kwargs):
        calls.append("pushplus")
        return True

    monkeypatch.setattr(main, "send_serverchan", fail_serverchan)
    monkeypatch.setattr(main, "send_pushplus", succeed_pushplus)
    config = {
        "serverchan": {"sendkey": "serverchan-key"},
        "pushplus_token": "pushplus-token",
    }
    assert send_notification(config, "标题", "内容") is True
    assert calls == ["pushplus"]

from datetime import date

from convertible_bond import down_reset_overrides as dro
from convertible_bond.cb_events import (
    CBEvent,
    CBEventStore,
    classify_announcement_title,
    events_for_down_reset,
    parse_event_from_announcement,
)
from convertible_bond.gui.constants import default_p_down_pct_for_state


def test_default_p_down_pct_for_state_uses_calibrated_buckets():
    assert default_p_down_pct_for_state(triggered=False) == (15.0, "未触发")
    assert default_p_down_pct_for_state(triggered=True) == (25.0, "已触发")
    assert default_p_down_pct_for_state(
        triggered=True, has_trigger_notice=True
    ) == (65.0, "触发提示")
    assert default_p_down_pct_for_state(
        triggered=True, has_scheduled_reset=True
    ) == (25.0, "公告态")
    assert default_p_down_pct_for_state(
        triggered=True, in_no_reset_block=True
    ) == (25.0, "冻结后")


def test_trigger_notice_is_not_classified_as_approved_down_reset():
    title = "关于惠云转债可能触发向下修正转股价格条件的提示性公告"

    assert classify_announcement_title(title) == "down_reset_trigger_notice"
    assert (
        classify_announcement_title(
            "关于触发转股价格向下修正条件暨董事会提议向下修正转股价格的公告"
        )
        == "down_reset_proposed"
    )
    event = parse_event_from_announcement("123456.SZ", title, date(2026, 1, 5))

    assert event is not None
    assert event.event_type == "down_reset_trigger_notice"
    assert event.parsed_status == "触发提示"


def test_events_for_down_reset_ignores_legacy_misclassified_trigger_notice(tmp_path):
    title = "关于惠云转债可能触发向下修正转股价格条件的提示性公告"
    store = CBEventStore(tmp_path / "events.json")
    store.add_many([
        CBEvent(
            bond_code="123456.SZ",
            event_date=date(2026, 1, 5),
            event_type="down_reset_approved",
            raw_title=title,
        )
    ])

    assert events_for_down_reset(
        "123456.SZ", store=store, through_date=date(2026, 1, 31)
    ) == []


def test_calibrated_down_reset_constants_are_pinned_to_their_values():
    """四个校准常数必须被**字面值**钉住, 不能只靠"拿常数算期望值"的断言.

    这四个数是 ``cb-calibrate-down-reset`` 从历史事件校准出来的模型参数, 直接决定
    regime ② 一次性下修节点的生效日与概率。但现存引用它们的用例全是
    ``assert result == pytest.approx(dro.PROPOSED_PASS_PROB)`` 这种形状 —— 期望值
    **从常数本身算出来**, 于是把 0.9 改成 0.1、把 17 天改成 170 天, 套件照样全绿。

    改这些值本身没问题, 但那是一次**模型行为变更**, 应该由这条用例红一次来确认,
    而不是悄悄生效。改校准请连同这里一起改, 并在提交信息里写明重跑了校准。
    """
    assert dro.PROPOSED_PASS_PROB == 0.95         # 有终态 96.4% / 含未决 93.0%
    assert dro.PROPOSED_EFFECTIVE_LAG_DAYS == 17  # 提议→通过 中位 17 / 均值 19 天
    assert dro.APPROVED_PASS_PROB == 1.0          # 已通过 → 必然落地
    assert dro.APPROVED_EFFECTIVE_LAG_DAYS == 7   # 通过→生效 (登记日次日) 的兜底
    assert dro.DEFAULT_COOLDOWN_MONTHS == 6       # 募集说明书常见冷静期

    # 概率是概率, 滞后是正整数天 —— 顺手钉住量纲, 免得单位被改成"月"还全绿
    assert 0.0 < dro.PROPOSED_PASS_PROB <= dro.APPROVED_PASS_PROB <= 1.0
    assert dro.PROPOSED_EFFECTIVE_LAG_DAYS > dro.APPROVED_EFFECTIVE_LAG_DAYS > 0


def test_approved_down_reset_builds_a_node_at_the_lag_fallback(tmp_path):
    """regime ②「已通过待生效」必须建得出节点 —— 此前恒不可达。

    ``parse_event_from_announcement`` 对 ``down_reset_approved`` **只解析 event_price,
    不解析生效日**, 于是 ``effective_start`` 恒等于公告日 (通用回落)、``effective_end``
    恒为 None (实测全库 113/113)。而 ``events_for_down_reset(through_date=valuation_date)``
    已保证 ``event_date <= valuation_date``, 所以 ``eff > cmp_date`` 恒为假 ——
    ``APPROVED_EFFECTIVE_LAG_DAYS`` 那条兜底一行都执行不到, 节点从来没建过。

    修法与 delisting / call_redemption 的 ``last_trading_date`` 同形: ``effective_start``
    只有**真解析到**(严格晚于公告日) 才算数, 否则走滞后兜底。
    """
    from datetime import date, timedelta

    from convertible_bond.data_providers import BondTerms
    from convertible_bond.down_reset_overrides import resolve_down_reset

    store = CBEventStore(tmp_path / "events.json")
    announced = date(2026, 8, 25)
    store.add_many([CBEvent(
        bond_code="123124.SZ", event_date=announced,
        event_type="down_reset_approved", parsed_status="已下修",
        raw_title="关于向下修正转股价格的公告",
        effective_start=announced,          # ← 通用回落, 不是真解析到的生效日
    )])
    terms = BondTerms(sec_name="晶瑞转2", conversion_price=12.0,
                      listing_date=date(2022, 1, 1), maturity_date=date(2030, 1, 1))

    resolved = resolve_down_reset("123124.SZ", terms, event_store=store,
                                  valuation_date=announced + timedelta(days=1))
    assert resolved.approved_effective_date == announced + timedelta(
        days=dro.APPROVED_EFFECTIVE_LAG_DAYS)

    # 真解析到生效日时照常用它
    store2 = CBEventStore(tmp_path / "events2.json")
    store2.add_many([CBEvent(
        bond_code="123124.SZ", event_date=announced,
        event_type="down_reset_approved", parsed_status="已下修",
        raw_title="关于向下修正转股价格的公告",
        effective_end=date(2026, 9, 10),
    )])
    resolved2 = resolve_down_reset("123124.SZ", terms, event_store=store2,
                                   valuation_date=announced + timedelta(days=1))
    assert resolved2.approved_effective_date == date(2026, 9, 10)


def test_both_block_until_implementations_agree_on_the_same_event(tmp_path):
    """不下修冻结期只许有一份口径 —— 用同一条事件跑两边, 不靠读代码。

    ``apply_events_to_terms`` (写进 cb_data) 与 ``resolve_down_reset`` (喂给 pricer)
    各自算过一次 block_until: 前者恒用默认 6 个月, 后者用公告自己写的承诺月数。实测
    113700.SH 海天转债那份写"三个月"的公告在两边差出整整一个季度 (2027-01-18 vs
    2026-10-18), 而 837 条不下修事件里 284 条的承诺月数不是 6。

    今天两边不一致也算不错价, 因为 ``resolve_down_reset`` 把 ``terms.down_reset_block_until``
    排在优先级最后、有事件时轮不到它 —— 所以这条用例断言的是**两边相等**, 而不是
    "定价结果对"。后者今天恒真, 拿它做守护等于没有守护。
    """
    from convertible_bond.cb_events import apply_events_to_terms
    from convertible_bond.data_providers import BondTerms
    from convertible_bond.down_reset_overrides import DownResetOverrides, resolve_down_reset

    empty_overrides = DownResetOverrides(tmp_path / "no-overrides.json")
    val_date = date(2026, 9, 4)

    # 0 单列: 真值判断会让它回落到 6, 而那正是两边分叉的形状。
    for months in (1, 3, 6, 12, 0, None):
        store = CBEventStore(tmp_path / f"events-{months}.json")
        event = CBEvent(
            bond_code="113700.SH",
            event_date=date(2026, 7, 18),
            event_type="down_reset_rejected",
            raw_title="关于不向下修正“海天转债”转股价格的公告",
            # 早于公告日 → plausible_commitment_end 判它不可信, 两边都要回落到月数
            effective_end=date(2026, 6, 26),
            commitment_months=months,
            parsed_status="不下修",
        )
        store.add_many([event])
        terms = BondTerms(sec_name="海天转债", conversion_price=10.0)

        applied = apply_events_to_terms(
            "113700.SH", terms, [event], valuation_date=val_date,
        ).down_reset_block_until
        resolved = resolve_down_reset(
            "113700.SH", terms, empty_overrides,
            valuation_date=val_date, event_store=store,
        ).block_until
        assert applied == resolved, f"承诺 {months} 个月时两边算出不同的冻结期"

    # 承诺月数确实被用上了 —— 免得两边一起退化成默认 6 个月也照样"相等"。
    three = apply_events_to_terms(
        "113700.SH",
        BondTerms(sec_name="海天转债"),
        [CBEvent(
            bond_code="113700.SH", event_date=date(2026, 7, 18),
            event_type="down_reset_rejected", raw_title="关于不向下修正的公告",
            effective_end=date(2026, 6, 26), commitment_months=3,
        )],
        valuation_date=val_date,
    ).down_reset_block_until
    assert three == date(2026, 10, 18), "公告写三个月, 却按默认 6 个月冻结"

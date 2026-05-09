"""WindPy 后端.

需要本机已安装 Wind 终端 + Python 插件。Wind 字段在不同终端/权限下可能存在差异,
``get_admission_status`` 通过 ``_wss_candidates`` 模式逐个候选字段尝试。
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from .base import (
    BondTerms,
    CashflowSchedule,
    DataProvider,
    infer_cb_trading_metadata,
    parse_coupon_string,
    to_date,
)
from ._helpers import (
    _announcement_row_from_wind,
    _date_or_none,
    _float_or_none,
    _latest_finite,
    _string_or_none,
    _wind_table_rows,
)


logger = logging.getLogger(__name__)


class WindDataProvider(DataProvider):
    """通过 WindPy 拉数据. 需要本机已安装 Wind 终端 + 插件."""
    name = "Wind"

    _BOND_FIELDS = (
        # 注: listdate 在某些 Wind 终端版本/账户上被拒 (CWSSService: invalid indicators),
        # 而 ipo_date 总是返回; listing_date 已统一兜底到 ipo_date, 因此不再请求 listdate.
        "sec_name", "underlyingcode", "ipo_date", "maturitydate",
        "latestpar",
        "clause_conversion2_swapshareprice",
        "clause_calloption_redemptionprice",
        "clause_calloption_triggerproportion",
        "clause_putoption_redeem_triggerproportion",
        "clause_putoption_putbackperiodobs",
        "couponrate",
        "close", "creditrating", "outstandingbalance",
    )

    def __init__(self):
        self._w = None
        # Wind 字段在不同终端版本/账户权限下偶尔会失效 (返回 "CWSSService: invalid indicators").
        # 首次批量调用失败时探测一次, 把坏字段缓存到此集合, 后续直接跳过, 避免每只债都重复探测.
        self._bad_bond_fields: set[str] = set()

    def _ensure(self):
        if self._w is not None:
            return self._w
        try:
            from WindPy import w  # type: ignore[import-not-found]
        except ImportError as e:
            raise ImportError(
                "未检测到 WindPy. 请在 Wind 终端 '插件管理' 中安装 Python 接口."
            ) from e
        if not w.isconnected():
            ret = w.start()
            if ret.ErrorCode != 0:
                raise RuntimeError(f"Wind 启动失败 (ErrorCode={ret.ErrorCode})")
        self._w = w
        return w

    def get_bond_terms(self, bond_code, valuation_date):
        w = self._ensure()
        val_str = valuation_date.strftime("%Y%m%d")
        fields = [f for f in self._BOND_FIELDS if f not in self._bad_bond_fields]
        if not fields:
            raise RuntimeError(
                f"Wind 取 {bond_code} 条款失败: 所有候选字段都被 Wind 拒绝, "
                f"已知失效字段 = {sorted(self._bad_bond_fields)}")
        res = w.wss(bond_code, ",".join(fields), f"tradeDate={val_str}")
        if res.ErrorCode != 0:
            # "invalid indicators" → 单字段探测找出失效字段, 缓存后用剩余字段重试一次
            err_str = str(getattr(res, "Data", "") or "").lower()
            if "invalid indicators" in err_str:
                newly_bad = self._probe_invalid_bond_fields(bond_code, fields, val_str)
                if newly_bad:
                    self._bad_bond_fields.update(newly_bad)
                    logger.warning(
                        "Wind 拒绝以下条款字段, 后续同步将自动跳过: %s", sorted(newly_bad))
                    fields = [f for f in fields if f not in newly_bad]
                    if not fields:
                        raise RuntimeError(
                            f"Wind 取 {bond_code} 条款失败: 探测后无可用字段, "
                            f"失效字段 = {sorted(newly_bad)}")
                    res = w.wss(bond_code, ",".join(fields), f"tradeDate={val_str}")
                    if res.ErrorCode != 0:
                        raise RuntimeError(
                            f"Wind 取 {bond_code} 条款失败 (重试后仍报错): {res.Data}")
                else:
                    raise RuntimeError(
                        f"Wind 取 {bond_code} 条款失败: {res.Data} "
                        f"(单字段探测未发现具体失效字段, 可能是 tradeDate / 权限问题)")
            else:
                raise RuntimeError(f"Wind 取 {bond_code} 条款失败: {res.Data}")
        d = {f.lower(): v[0] for f, v in zip(res.Fields, res.Data)}

        def _f(key):
            v = d.get(key)
            return float(v) if v is not None else None

        terms = BondTerms(
            sec_name=d.get("sec_name"),
            underlying_code=d.get("underlyingcode"),
            issue_date=to_date(d.get("ipo_date")),
            listing_date=to_date(d.get("listdate")) or to_date(d.get("ipo_date")),
            maturity_date=to_date(d.get("maturitydate")),
            face_value=_f("latestpar"),
            conversion_price=_f("clause_conversion2_swapshareprice"),
            redemption_price=_f("clause_calloption_redemptionprice"),
            call_trigger_pct=_f("clause_calloption_triggerproportion"),
            put_trigger_pct=_f("clause_putoption_redeem_triggerproportion"),
            put_obs_months=_f("clause_putoption_putbackperiodobs"),
            coupon_rates=parse_coupon_string(d.get("couponrate")),
            close=_f("close"),
            credit_rating=d.get("creditrating"),
            outstanding_balance=_f("outstandingbalance"),
        )
        return infer_cb_trading_metadata(bond_code, terms, valuation_date)

    def _probe_invalid_bond_fields(self, code, fields, val_str):
        """单字段试探, 返回 Wind 报 'invalid indicators' 的字段集合.

        仅在批量 wss 失败时被调用一次; 用于一次性识别失效字段并缓存到
        ``self._bad_bond_fields``, 避免后续每只债都重复批量失败.
        """
        bad: set[str] = set()
        for field in fields:
            try:
                res = self._w.wss(code, field, f"tradeDate={val_str}")
            except Exception:
                continue
            if getattr(res, "ErrorCode", -1) == 0:
                continue
            data_str = str(getattr(res, "Data", "") or "").lower()
            if "invalid indicators" in data_str:
                bad.add(field)
        return bad

    def get_admission_status(self, bond_code, valuation_date, base_terms=None):
        """增量刷新主池准入状态字段.

        Wind 字段在不同终端/权限下可能存在差异, 因此这里逐个候选字段尝试;
        拿不到的字段保持 None, 不影响已有 cb_data 内容。
        """
        bond_data = self._wss_candidates(
            bond_code,
            {
                "suspension_status": ("trade_status", "suspensionstatus", "suspendtype"),
                "call_status": (
                    "clause_calloption_status",
                    "calloption_status",
                    "redemption_status",
                    "earlyredemption_status",
                ),
                "call_announce_date": (
                    "clause_calloption_announcementdate",
                    "calloption_announcementdate",
                    "redemption_announcementdate",
                ),
                "call_redemption_date": (
                    "clause_calloption_redemptiondate",
                    "calloption_redemptiondate",
                    "redemptiondate",
                ),
                "last_trading_date": ("lasttrade_date", "lasttradingdate", "last_trade_date"),
                "delisting_date": ("delist_date", "delistingdate"),
                "credit_rating": ("creditrating",),
                "outstanding_balance": ("outstandingbalance",),
            },
            valuation_date,
        )
        bond_turnover_amount = self._wsd_latest_number(bond_code, "amt", valuation_date)

        underlying_code = None
        if base_terms is not None:
            underlying_code = base_terms.underlying_code
        if not underlying_code:
            underlying_code = self._wss_value(bond_code, "underlyingcode", valuation_date)

        stock_data = {}
        underlying_pct_change = None
        if underlying_code:
            stock_data = self._wss_candidates(
                str(underlying_code),
                {
                    "underlying_name": ("sec_name",),
                    # 结构性状态: ST/退市风险等, 一般通过专用字段返回, 不会随每日交易切换
                    "underlying_status": ("riskwarning", "st_status", "specialtreatment"),
                    # 日级交易状态: 停牌/暂停交易; trade_status 在 Wind 上对停牌正股返回 "停牌"
                    "underlying_trade_status": ("trade_status", "tradestatus"),
                },
                valuation_date,
            )
            underlying_pct_change = self._wsd_latest_number(
                str(underlying_code), "pct_chg", valuation_date,
            )

        terms = BondTerms(
            suspension_status=_string_or_none(bond_data.get("suspension_status")),
            call_status=_string_or_none(bond_data.get("call_status")),
            call_announce_date=_date_or_none(bond_data.get("call_announce_date")),
            call_redemption_date=_date_or_none(bond_data.get("call_redemption_date")),
            last_trading_date=_date_or_none(bond_data.get("last_trading_date")),
            delisting_date=_date_or_none(bond_data.get("delisting_date")),
            underlying_name=_string_or_none(stock_data.get("underlying_name")),
            underlying_status=_string_or_none(stock_data.get("underlying_status")),
            underlying_trade_status=_string_or_none(stock_data.get("underlying_trade_status")),
            underlying_pct_change=underlying_pct_change,
            bond_turnover_amount=bond_turnover_amount,
            credit_rating=_string_or_none(bond_data.get("credit_rating")),
            outstanding_balance=_float_or_none(bond_data.get("outstanding_balance")),
        )
        return terms

    def _wss_candidates(self, code, candidates, valuation_date):
        return {
            key: self._wss_first_available(code, fields, valuation_date)
            for key, fields in candidates.items()
        }

    def _wss_first_available(self, code, fields, valuation_date):
        for field_name in fields:
            value = self._wss_value(code, field_name, valuation_date)
            if value is not None:
                return value
        return None

    def _wss_value(self, code, field_name, valuation_date):
        w = self._ensure()
        val_str = valuation_date.strftime("%Y%m%d")
        try:
            res = w.wss(code, field_name, f"tradeDate={val_str}")
        except Exception:
            return None
        if getattr(res, "ErrorCode", -1) != 0 or not getattr(res, "Data", None):
            return None
        try:
            value = res.Data[0][0]
        except Exception:
            return None
        return value if value not in ("", "--") else None

    def _wsd_latest_number(self, code, field_name, valuation_date):
        w = self._ensure()
        d = valuation_date.isoformat()
        try:
            res = w.wsd(code, field_name, d, d, "")
        except Exception:
            return None
        if getattr(res, "ErrorCode", -1) != 0 or not getattr(res, "Data", None):
            return None
        try:
            return _float_or_none(res.Data[0][-1])
        except Exception:
            return None

    def list_bond_announcements(self, bond_code, start, end):
        """尝试从 Wind 公告接口拉公告标题.

        Wind 的公告 wset 字段在不同环境中可能有差异; 本实现只做容错尝试,
        失败时返回空列表, 不影响状态字段同步和人工事件表。
        """
        w = self._ensure()
        options = (
            f"windcode={bond_code};startdate={start.isoformat()};enddate={end.isoformat()}",
            f"windcode={bond_code};startDate={start.isoformat()};endDate={end.isoformat()}",
            f"secid={bond_code};startdate={start.isoformat()};enddate={end.isoformat()}",
        )
        datasets = ("announcement", "announcemnent")
        for dataset in datasets:
            for option in options:
                try:
                    res = w.wset(dataset, option)
                except Exception:
                    continue
                if getattr(res, "ErrorCode", -1) != 0 or not getattr(res, "Data", None):
                    continue
                rows = _wind_table_rows(res)
                parsed = [_announcement_row_from_wind(row) for row in rows]
                parsed = [row for row in parsed if row.get("title") and row.get("date")]
                if parsed:
                    return parsed
        return []

    def get_stock_close(self, stock_code, on_date):
        w = self._ensure()
        val_str = on_date.strftime("%Y%m%d")
        res = w.wss(stock_code, "close", f"tradeDate={val_str};priceAdj=U")
        if res.ErrorCode != 0:
            raise RuntimeError(f"Wind 取正股 {stock_code} 现价失败: {res.Data}")
        return float(res.Data[0][0])

    def get_stock_history(self, stock_code, start, end):
        w = self._ensure()
        res = w.wsd(stock_code, "close", start.isoformat(), end.isoformat(), "priceAdj=U")
        if res.ErrorCode != 0:
            raise RuntimeError(f"Wind 取正股 {stock_code} 历史价失败: {res.Data}")
        return [
            (to_date(t), (float(v) if v is not None else None))
            for t, v in zip(res.Times, res.Data[0])
        ]

    def get_bond_history(self, bond_code, start, end):
        w = self._ensure()
        res = w.wsd(bond_code, "close", start.isoformat(), end.isoformat())
        if res.ErrorCode != 0:
            raise RuntimeError(f"Wind 取 {bond_code} 历史价失败: {res.Data}")
        return [
            (to_date(t), (float(v) if v is not None else None))
            for t, v in zip(res.Times, res.Data[0])
        ]

    def get_cashflow(self, bond_code):
        w = self._ensure()
        res = w.wset("cashflow", f"windcode={bond_code}")
        if res.ErrorCode != 0 or not res.Data:
            return None
        fields = [f.lower() for f in res.Fields]
        try:
            i_date = fields.index("cash_flows_date")
            i_cf = fields.index("cash_flows_per_cny100_par")
            i_rate = fields.index("coupon_rate")
        except ValueError:
            return None
        rows = list(zip(*res.Data))
        if not rows:
            return None
        coupons = []
        for row in rows:
            rate = row[i_rate]
            if rate is None:
                continue
            coupons.append(float(rate) / 100.0)
        last = rows[-1]
        return CashflowSchedule(
            coupon_rates=tuple(coupons) if coupons else None,
            redemption_price=float(last[i_cf]) if last[i_cf] is not None else None,
            maturity_date=to_date(last[i_date]) if last[i_date] else None,
            cashflows=rows,
        )

    def get_risk_free_rate(self, on_date):
        w = self._ensure()
        # SHIBOR1Y.IR 用 w.wsd (证券日频接口) 而不是 w.edb: 后者需要 EDB (经济数据库)
        # 订阅, 普通账户会返回 ErrorCode=-40521007 "权限验证不通过"; wsd 仅需基础行情权限.
        rr = w.wsd("SHIBOR1Y.IR", "close",
                   (on_date - timedelta(days=10)).isoformat(),
                   on_date.isoformat())
        if rr.ErrorCode == 0:
            if not rr.Data or not rr.Data[0]:
                return None
            return _latest_finite(rr.Data[0])
        # Wind 也失败时退回 akshare 的公开 Shibor 数据 (央行授权全国银行间同业拆借中心
        # 发布, akshare 与 Wind 数值一致).
        diag = ""
        try:
            if rr.Data and rr.Data[0]:
                diag = f": {rr.Data[0]}"
        except Exception:
            pass
        wind_err = f"Wind SHIBOR1Y.IR 拉取失败 (ErrorCode={rr.ErrorCode}{diag})"
        try:
            from .akshare import AkshareDataProvider
            ak_rate = AkshareDataProvider().get_risk_free_rate(on_date)
        except Exception as ak_exc:
            raise RuntimeError(f"{wind_err}; akshare 兜底也失败: {ak_exc}") from ak_exc
        if ak_rate is None:
            raise RuntimeError(f"{wind_err}; akshare 兜底返回空")
        logger.info("%s, 已 fallback 到 akshare Shibor: %.4f%%", wind_err, ak_rate)
        return ak_rate

    def list_tradable_cbs(self, on_date=None):
        """通过 wset('sectorconstituent') 拉某日的"沪深可转债"成分.

        sectorid 'a101020600000000' = 沪深可转债 (含已退市标记的债不入此列).
        返回 ``[(wind_code, sec_name), ...]``; ``sec_name`` 留给上层过滤定向转债。
        """
        w = self._ensure()
        d = (on_date or date.today()).isoformat()
        res = w.wset(
            "sectorconstituent",
            f"date={d};sectorid=a101020600000000;field=wind_code,sec_name",
        )
        if res.ErrorCode != 0:
            raise RuntimeError(f"Wind wset 拉转债成分失败: {res.Data}")
        try:
            fields = [f.lower() for f in res.Fields]
            i_code = fields.index("wind_code")
        except (AttributeError, ValueError):
            return []
        i_name = fields.index("sec_name") if "sec_name" in fields else None
        rows = list(zip(*res.Data))
        out: list[tuple[str, str | None]] = []
        for r in rows:
            code = r[i_code]
            if not code:
                continue
            name = str(r[i_name]) if i_name is not None and r[i_name] else None
            out.append((str(code), name))
        return out

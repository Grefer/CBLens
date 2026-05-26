"""本地 CSV 后端: 适用于无网络/无 Wind/无 akshare 的离线环境.

目录布局 (root/):
  bonds/<bond_code>.csv      列: date,close
  stocks/<stock_code>.csv    列: date,close
  terms/<bond_code>.json     条款 JSON (字段名同 BondTerms)
"""
from __future__ import annotations

from datetime import date

from .base import (
    BondTerms,
    DataProvider,
    infer_cb_trading_metadata,
    to_date,
)


class CSVDataProvider(DataProvider):
    """从本地 CSV 文件读取数据, 适用于无网络/无 Wind/无 akshare 的环境.

    任何文件缺失会抛出 FileNotFoundError; 上层应捕获并提示用户手填.
    """
    name = "CSV"

    def __init__(self, root: str):
        from pathlib import Path
        self.root = Path(root)
        if not self.root.exists():
            raise FileNotFoundError(f"CSV 数据根目录不存在: {root}")

    def _read_price_csv(self, path) -> list[tuple[date, float | None]]:
        import csv
        out = []
        with open(path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                d_raw = row.get("date") or row.get("日期")
                c_raw = row.get("close") or row.get("收盘")
                if d_raw is None:
                    continue
                d = to_date(d_raw)
                try:
                    v = float(c_raw) if c_raw not in (None, "") else None
                except ValueError:
                    v = None
                out.append((d, v))
        out.sort(key=lambda x: x[0] or date.min)
        return out

    def _bond_csv(self, code):
        p = self.root / "bonds" / f"{code}.csv"
        if not p.exists():
            raise FileNotFoundError(f"未找到转债历史: {p}")
        return self._read_price_csv(p)

    def _stock_csv(self, code):
        p = self.root / "stocks" / f"{code}.csv"
        if not p.exists():
            raise FileNotFoundError(f"未找到正股历史: {p}")
        return self._read_price_csv(p)

    def get_bond_terms(self, bond_code, valuation_date):
        import json
        p = self.root / "terms" / f"{bond_code}.json"
        if not p.exists():
            return BondTerms()
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        coupons = d.get("coupon_rates")
        if isinstance(coupons, list):
            coupons = tuple(float(x) for x in coupons)
        terms = BondTerms(
            sec_name=d.get("sec_name"),
            underlying_code=d.get("underlying_code"),
            issue_date=to_date(d.get("issue_date")),
            listing_date=to_date(d.get("listing_date")),
            tradable_date=to_date(d.get("tradable_date")),
            is_tradable=d.get("is_tradable"),
            trading_status=d.get("trading_status"),
            maturity_date=to_date(d.get("maturity_date")),
            face_value=d.get("face_value"),
            conversion_price=d.get("conversion_price"),
            redemption_price=d.get("redemption_price"),
            down_reset_trigger_pct=d.get("down_reset_trigger_pct"),
            call_trigger_pct=d.get("call_trigger_pct"),
            put_trigger_pct=d.get("put_trigger_pct"),
            put_obs_months=d.get("put_obs_months"),
            putback_start_date=to_date(d.get("putback_start_date")),
            putback_end_date=to_date(d.get("putback_end_date")),
            putback_price=d.get("putback_price"),
            conversion_suspension_start_date=to_date(d.get("conversion_suspension_start_date")),
            conversion_suspension_end_date=to_date(d.get("conversion_suspension_end_date")),
            conversion_suspension_status=d.get("conversion_suspension_status"),
            down_reset_block_until=to_date(d.get("down_reset_block_until")),
            down_reset_p_scale=d.get("down_reset_p_scale"),
            down_reset_note=d.get("down_reset_note"),
            down_reset_cooldown_months=d.get("down_reset_cooldown_months"),
            coupon_rates=coupons,
            close=d.get("close"),
            credit_rating=d.get("credit_rating"),
            credit_rating_outlook=d.get("credit_rating_outlook"),
            credit_watch_status=d.get("credit_watch_status"),
            outstanding_balance=d.get("outstanding_balance"),
            suspension_status=d.get("suspension_status"),
            call_status=d.get("call_status"),
            call_announce_date=to_date(d.get("call_announce_date")),
            call_redemption_date=to_date(d.get("call_redemption_date")),
            call_redemption_price=d.get("call_redemption_price"),
            call_no_redemption_until=to_date(d.get("call_no_redemption_until")),
            last_trading_date=to_date(d.get("last_trading_date")),
            delisting_date=to_date(d.get("delisting_date")),
            underlying_name=d.get("underlying_name"),
            underlying_status=d.get("underlying_status"),
            underlying_trade_status=d.get("underlying_trade_status"),
            underlying_pct_change=d.get("underlying_pct_change"),
            bond_turnover_amount=d.get("bond_turnover_amount"),
        )
        return infer_cb_trading_metadata(bond_code, terms, valuation_date)

    def get_stock_close(self, stock_code, on_date):
        history = self._stock_csv(stock_code)
        for d, v in reversed(history):
            if d is not None and v is not None and d <= on_date:
                return float(v)
        raise RuntimeError(f"CSV 中无 {stock_code} 在 {on_date} 之前的有效收盘价")

    def get_stock_history(self, stock_code, start, end):
        return [(d, v) for d, v in self._stock_csv(stock_code) if d is not None and start <= d <= end]

    def get_bond_history(self, bond_code, start, end):
        return [(d, v) for d, v in self._bond_csv(bond_code) if d is not None and start <= d <= end]

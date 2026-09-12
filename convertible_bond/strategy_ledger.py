"""策略持仓账本：报价用于估值，成交与已确认支付才改变可用资金。

转债历史 ``close`` 按未复权全价使用；合同票息记为应收，明确的实际付款才转入现金。
旧 Wind ``cashflows`` 元组不带列名，不能可靠识别到账日期或金额，因而不作现金凭证。
强赎登记日、摘牌日和名义到期日也不是到账凭证。所有现金流按持有区间 (start, end]
入账；到期含息赎回金额只结算一次，不再额外叠加最后一期票息。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from .data_providers.base import finite_float, to_date


ACCOUNTING_BASIS = "cash_ledger_v1"
_EPS = 1e-12


@dataclass
class LedgerHolding:
    """数量持续跨期，最后报价日期独立于真实退出日期。"""

    quantity: float
    entry_date: date
    entry_price: float
    mark_date: date
    mark_price: float
    metadata: dict[str, Any] = field(default_factory=dict)
    pending_exit: dict[str, Any] | None = None


@dataclass
class CouponReceivable:
    """已持有券的合同应收；付款未核实前不得用于建仓。"""

    bond_code: str
    coupon_date: date
    quantity: float
    amount_per_bond: float
    carrying_amount_per_bond: float | None = None

    @property
    def amount(self) -> float:
        carrying = self.amount_per_bond if self.carrying_amount_per_bond is None else self.carrying_amount_per_bond
        return self.quantity * carrying

    @property
    def nominal_amount(self) -> float:
        return self.quantity * self.amount_per_bond


def _safe_date(value) -> date | None:
    try:
        return to_date(value)
    except (ValueError, TypeError):
        return None


def _anniversary(start: date, year: int) -> date:
    try:
        return start.replace(year=start.year + year)
    except ValueError:
        return start.replace(year=start.year + year, day=28)


def _cashflows(provider, code: str, on_date: date):
    """只解析有明确字段名的付款；无标签元组和登记日一律不猜。"""
    warnings: list[str] = []
    try:
        terms = provider.get_bond_terms(code, on_date)
    except Exception:
        terms = None
    try:
        schedule = provider.get_cashflow(code)
    except Exception as exc:
        schedule = None
        warnings.append(f"{code}: 现金流取数失败 ({type(exc).__name__})")
    confirmed: list[dict[str, Any]] = []
    for row in getattr(schedule, "cashflows", ()) or ():
        if not isinstance(row, dict):
            warnings.append(f"{code}: 原始现金流无字段名，未据此确认付款")
            continue
        payment_date = _safe_date(row.get("payment_date"))
        amount = finite_float(row.get("amount_per_bond", row.get("amount")))
        kind = row.get("kind", row.get("type"))
        # confirmed 必须是明确布尔值；truthy 字符串不能充当付款凭证。
        if (row.get("confirmed") is not True or payment_date is None
                or amount is None or amount < 0 or kind not in {"coupon", "redemption"}):
            warnings.append(f"{code}: 现金流缺少确认状态、到账日、金额或类型，未计入现金")
            continue
        confirmed.append({
            "date": payment_date, "amount": amount, "kind": kind,
            "coupon_date": _safe_date(row.get("coupon_date")) or payment_date,
            "coupon_date_explicit": _safe_date(row.get("coupon_date")) is not None,
            "record_date": _safe_date(row.get("record_date")),
        })
    # 相同凭证可能在多份公告重复出现；按经济事实去重。
    confirmed = list({(r["date"], r["kind"], r["amount"], r["coupon_date"]): r
                      for r in confirmed}.values())
    rates = getattr(schedule, "coupon_rates", None) or getattr(terms, "coupon_rates", None)
    issue = getattr(terms, "issue_date", None)
    maturity = getattr(schedule, "maturity_date", None) or getattr(terms, "maturity_date", None)
    face = finite_float(getattr(terms, "face_value", None)) or 100.0
    coupons: dict[date, float] = {}
    if isinstance(issue, date) and rates:
        for year, rate in enumerate(rates, start=1):
            value = finite_float(rate)
            coupon_date = _anniversary(issue, year)
            # 最后一期票息已包含在到期赎回价里；没有实际兑付凭证时保留原持仓。
            if value is not None and value >= 0 and (maturity is None or coupon_date < maturity):
                coupons[coupon_date] = face * value
    if terms is not None and (getattr(terms, "call_redemption_date", None)
                              or (isinstance(maturity, date) and maturity <= on_date)):
        if not any(row["kind"] == "redemption" for row in confirmed):
            warnings.append(f"{code}: 兑付到账未核实，持仓继续估值，不释放本金")
    return coupons, confirmed, list(dict.fromkeys(warnings))


class StrategyLedger:
    """无杠杆、分数张数的研究账本；组合与各条基准使用同一实现。"""

    def __init__(self):
        self.cash = 1.0
        self.holdings: dict[str, LedgerHolding] = {}
        self.receivables: list[CouponReceivable] = []
        self.processed_cashflows: set[tuple] = set()
        self.payment_entitlements: dict[tuple, float] = {}
        self.last_date: date | None = None

    @property
    def equity(self) -> float:
        return self.cash + sum(h.quantity * h.mark_price for h in self.holdings.values()) + sum(
            r.amount for r in self.receivables)

    def run_period(self, provider, selected: list[dict[str, Any]], start: date, end: date,
                   *, intended_count: int, exposure: float = 1.0,
                   funding_mode: str = "reserve_cash", execution_timing: str = "signal_close",
                   execution_lookahead_days: int = 10, transaction_cost: float = 0.0,
                   cash_yield_rate: float = 0.0, event_store=None, cancel_cb=None) -> dict[str, Any]:
        """按真实日期处理付款→报价→卖出→买入，期末只估值，不虚构清仓。

        换手按 max(买入额, 卖出额) 度量，单边费率对买卖两侧的真实成交分别计费。
        费用从现金支付，买入额预留费用；未确认应收及无法卖出的持仓不能出资。
        next_close 的旧仓在信号日后继续持有，直到该券下一根真实收盘才调仓。
        """
        start_equity = self.equity
        opening_cash = self.cash
        rows = {str(r.get("bond_code")): dict(r) for r in selected}
        codes = (set(rows) | set(self.holdings) | {r.bond_code for r in self.receivables}
                 | {key[0] for key in self.payment_entitlements})
        histories: dict[str, dict[date, float]] = {}
        schedules = {}
        warnings: list[str] = []
        event_signals: dict[date, list[tuple[str, Any]]] = {}
        for code in sorted(codes):
            if cancel_cb is not None:
                cancel_cb()
            try:
                history = provider.get_bond_history(code, start, end)
            except Exception as exc:
                history = []
                warnings.append(f"{code}: 行情取数失败 ({type(exc).__name__})，原持仓保留")
            histories[code] = {
                d: px for d, raw in history or []
                if isinstance(d, date) and start <= d <= end
                and (px := finite_float(raw)) is not None and px > 0
            }
            coupons, payments, notes = _cashflows(provider, code, end)
            schedules[code] = (coupons, payments)
            warnings.extend(notes)
            if event_store is not None:
                try:
                    events = event_store.list_events(bond_code=code, through_date=end)
                except Exception:
                    events = []
                for event in events or []:
                    d = getattr(event, "event_date", None)
                    if (isinstance(d, date) and start < d <= end and getattr(event, "event_type", None)
                            in {"down_reset_proposed", "down_reset_approved", "down_reset_rejected"}):
                        event_signals.setdefault(d, []).append((code, event))

        deadline = min(end, start + timedelta(days=max(1, execution_lookahead_days)))
        # full_invest 在当前时点有可成交价的券间分配，不借用未来是否有报价的信息。
        desired = {code: exposure / intended_count for code in rows} if intended_count > 0 else {}
        pending = set(rows) | set(self.holdings)
        executable_candidates = set(rows) & set(self.holdings)
        for code, holding in self.holdings.items():
            if code not in rows:
                holding.pending_exit = holding.pending_exit or {
                    "signal_date": start, "reason": "rebalance"}
            elif (holding.pending_exit or {}).get("reason") == "rebalance":
                holding.pending_exit = None
        period_rows: dict[str, dict[str, Any]] = {}
        opening_values: dict[str, float] = {}
        buys: dict[str, float] = {}
        sells: dict[str, float] = {}
        incomes: dict[str, float] = {}
        coupon_cash = redemption_cash = coupon_income = cash_interest = fees = 0.0
        rebalance_turnover = event_turnover = 0.0
        ledger_entries: list[dict[str, Any]] = []
        curve: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        event_exit_count = 0
        actual_denominator = intended_count
        cash_weight_days = 0.0
        first_cash_weight: float | None = None

        def ensure_row(code, holding=None):
            if code not in period_rows:
                metadata = rows.get(code) or (holding.metadata if holding else {})
                period_rows[code] = dict(metadata)
                period_rows[code].update({
                    "bond_code": code, "entry_date": holding.entry_date if holding else None,
                    "start_price": holding.mark_price if holding else None,
                    "exit_date": None, "exit_reason": "held", "exit_signal_date": None,
                    "exit_event_type": None, "exit_event_title": None,
                    "post_exit_cash_return": 0.0, "event_exit_cash_days": 0,
                })
            return period_rows[code]

        for code, holding in self.holdings.items():
            ensure_row(code, holding)
            opening_values[code] = holding.quantity * holding.mark_price
        previous_date = start
        current = start
        while current <= end:
            if cancel_cb is not None:
                cancel_cb()
            days = (current - previous_date).days
            nav_before = self.equity
            if days:
                cash_weight_days += (self.cash / nav_before if nav_before > 0 else 0.0) * days
                # 每个自然日一次，避免曲线采样稀疏程度改变利息；ACT/365 日复利。
                interest = self.cash * max(0.0, cash_yield_rate) * days / 365.0
                self.cash += interest
                cash_interest += interest
                if interest:
                    ledger_entries.append({"date": current, "kind": "cash_interest",
                                           "amount": interest, "cash_change": interest})
            previous_date = current
            # 期初属于上期，避免连续两期重复领息。首期买入日也不领取当日票息。
            if current > start:
                for code in sorted(codes):
                    coupons, payments = schedules[code]
                    holding = self.holdings.get(code)
                    today_payments = [p for p in payments if p["date"] == current]
                    terminal = any(p["kind"] == "redemption" for p in today_payments)
                    amount = coupons.get(current)
                    direct_coupon = any(p["kind"] == "coupon" and p["coupon_date"] == current
                                        for p in today_payments)
                    if holding and amount is not None and not terminal and not direct_coupon:
                        key = (code, current, "receivable")
                        if key not in self.processed_cashflows:
                            self.processed_cashflows.add(key)
                            book_amount = (amount if current in histories[code]
                                           else min(amount, holding.mark_price))
                            receivable = CouponReceivable(code, current, holding.quantity, amount, book_amount)
                            self.receivables.append(receivable)
                            coupon_income += receivable.amount
                            incomes[code] = incomes.get(code, 0.0) + receivable.amount
                            # 停牌期间旧全价尚未除息，不能再凭空加一份应收；扣减后继续估值。
                            if current not in histories[code]:
                                holding.mark_price = max(0.0, holding.mark_price - book_amount)
                            ledger_entries.append({"date": current, "bond_code": code,
                                                   "kind": "coupon_receivable", "amount": receivable.amount,
                                                   "nominal_amount": receivable.nominal_amount,
                                                   "cash_change": 0.0, "confirmed": False})
                    for payment in today_payments:
                        key = (code, current, payment["kind"], payment["coupon_date"], payment["amount"])
                        if key in self.processed_cashflows:
                            continue
                        self.processed_cashflows.add(key)
                        holding = self.holdings.get(code)
                        if payment["kind"] == "coupon":
                            # 已确认的含息终值全额结算，最后一期付息行不能再叠一次。
                            if terminal and payment["coupon_date"] == current:
                                continue
                            accrued = [r for r in self.receivables if r.bond_code == code
                                       and (r.coupon_date == payment["coupon_date"]
                                            if payment["coupon_date_explicit"] else r.coupon_date <= current)]
                            if not payment["coupon_date_explicit"] and len({r.coupon_date for r in accrued}) > 1:
                                warnings.append(f"{code}: 已确认票息未标明所属息期且有多笔应收，暂不重复确认收入")
                                continue
                            if accrued:
                                quantity = sum(r.quantity for r in accrued)
                                prior = sum(r.amount for r in accrued)
                                self.receivables = [r for r in self.receivables if r not in accrued]
                            elif (code, payment["record_date"], payment["coupon_date"],
                                  payment["amount"]) in self.payment_entitlements:
                                quantity = self.payment_entitlements.pop((
                                    code, payment["record_date"], payment["coupon_date"], payment["amount"]))
                                prior = 0.0
                            elif holding and (
                                holding.entry_date <= payment["record_date"] if payment["record_date"]
                                else holding.entry_date < payment["coupon_date"]
                            ):
                                quantity, prior = holding.quantity, 0.0
                            else:
                                continue
                            paid = quantity * payment["amount"]
                            coupon_cash += paid
                            coupon_income += paid - prior
                            incomes[code] = incomes.get(code, 0.0) + paid - prior
                            ensure_row(code, holding)
                            self.cash += paid
                            if not prior and holding and current not in histories[code]:
                                holding.mark_price = max(0.0, holding.mark_price - payment["amount"])
                        elif holding:
                            paid = holding.quantity * payment["amount"]
                            self.cash += paid
                            redemption_cash += paid
                            sells[code] = sells.get(code, 0.0) + paid
                            row = ensure_row(code, holding)
                            row.update({"exit_date": current, "exit_reason": "redemption",
                                        "end_price": payment["amount"], "position_status": "redeemed"})
                            del self.holdings[code]
                            pending.discard(code)
                            desired.pop(code, None)
                        else:
                            continue
                        ledger_entries.append({"date": current, "bond_code": code, "kind": payment["kind"],
                                               "amount": paid, "cash_change": paid, "confirmed": True})

            for code, holding in self.holdings.items():
                px = histories.get(code, {}).get(current)
                if px is not None:
                    holding.mark_price, holding.mark_date = px, current
            # 公告日收盘之后才获知信号，最早下一自然日的真实报价成交。
            for code, event in event_signals.get(current, []):
                holding = self.holdings.get(code)
                if holding:
                    holding.pending_exit = {"signal_date": current, "reason": "down_reset_event",
                                            "event_type": event.event_type,
                                            "event_title": getattr(event, "raw_title", None)}
                    desired.pop(code, None)
                    pending.add(code)
            can_rebalance = (current == start if execution_timing == "signal_close" else current > start)
            tradeable = {
                code for code in pending if current in histories.get(code, {})
                and (can_rebalance or (self.holdings.get(code) is not None
                     and self.holdings[code].pending_exit is not None))
            }
            tradeable = {code for code in tradeable
                         if not (self.holdings.get(code) and self.holdings[code].pending_exit
                                 and self.holdings[code].pending_exit["signal_date"] >= current
                                 and (execution_timing != "signal_close"
                                      or self.holdings[code].pending_exit["reason"] == "down_reset_event"))}
            if funding_mode == "full_invest" and can_rebalance and current <= deadline:
                executable_candidates.update(code for code in rows if code in tradeable)
                available = {code for code in executable_candidates
                             if not (code in self.holdings and self.holdings[code].pending_exit)}
                if available:
                    new_desired = {code: exposure / len(available) for code in available}
                    if new_desired != desired:
                        # 较晚才可成交的新成员改变等权目标，已有仓位也要参加实际再平衡。
                        # 只把今天有真实报价的券加入成交批，不能按陈旧估值卖出凑钱。
                        pending.update(available)
                        tradeable.update(code for code in available if current in histories.get(code, {}))
                    desired = new_desired
                    actual_denominator = len(available)
            nav = self.equity
            # 无法调动的旧仓已占用风险预算；停牌仓位超标时不能再主动买入添风险。
            immovable_value = sum(h.quantity * h.mark_price for code, h in self.holdings.items()
                                  if code not in tradeable)
            remaining_risk_budget = max(0.0, exposure * nav - immovable_value)
            desired_tradeable_value = sum(desired.get(code, 0.0) * nav for code in tradeable)
            target_scale = (min(1.0, remaining_risk_budget / desired_tradeable_value)
                            if desired_tradeable_value > 0 else 1.0)
            sale_amount = buy_amount = event_sales = 0.0
            requests: dict[str, float] = {}
            for code in sorted(tradeable):
                holding = self.holdings.get(code)
                target = desired.get(code, 0.0) * nav * target_scale
                if holding and holding.pending_exit:
                    target = 0.0
                value = holding.quantity * holding.mark_price if holding else 0.0
                delta = target - value
                if delta < -_EPS and holding:
                    amount = min(value, -delta)
                    quantity = amount / histories[code][current]
                    self.cash += amount
                    holding.quantity -= quantity
                    sale_amount += amount
                    sells[code] = sells.get(code, 0.0) + amount
                    reason = (holding.pending_exit or {}).get("reason", "rebalance")
                    row = ensure_row(code, holding)
                    if reason == "down_reset_event":
                        event_sales += amount
                        event_exit_count += 1
                    if holding.quantity <= _EPS:
                        row.update({"exit_date": current, "end_price": histories[code][current],
                                    "exit_reason": reason, "position_status": "closed",
                                    "exit_signal_date": (holding.pending_exit or {}).get("signal_date", start),
                                    "exit_event_type": (holding.pending_exit or {}).get("event_type"),
                                    "exit_event_title": (holding.pending_exit or {}).get("event_title")})
                        del self.holdings[code]
                    ledger_entries.append({"date": current, "bond_code": code, "kind": "sell",
                                           "quantity": quantity, "price": histories[code][current],
                                           "amount": amount, "cash_change": amount, "reason": reason})
                    pending.discard(code)
                elif delta > _EPS and current <= deadline and can_rebalance:
                    requests[code] = delta
                elif abs(delta) <= _EPS:
                    # 被冻结资金/风险预算暂时挡住的新买单，执行窗口内仍可等旧仓卖出。
                    if holding is not None or desired.get(code, 0.0) <= 0:
                        pending.discard(code)
            requested = sum(requests.values())
            # 买卖双方分别付费；现金约束一并解，不能用负现金暗中融资。
            affordable = max(0.0, (self.cash - transaction_cost * sale_amount) / (1.0 + transaction_cost))
            ratio = min(1.0, affordable / requested) if requested > 0 else 0.0
            for code, requested_amount in requests.items():
                amount = requested_amount * ratio
                if amount <= _EPS:
                    continue
                px = histories[code][current]
                quantity = amount / px
                holding = self.holdings.get(code)
                if holding is None:
                    holding = LedgerHolding(quantity, current, px, current, px, rows.get(code, {}))
                    self.holdings[code] = holding
                else:
                    holding.quantity += quantity
                row = ensure_row(code, holding)
                row["entry_date"] = row.get("entry_date") or current
                row["start_price"] = row.get("start_price") or px
                self.cash -= amount
                buy_amount += amount
                buys[code] = buys.get(code, 0.0) + amount
                ledger_entries.append({"date": current, "bond_code": code, "kind": "buy",
                                       "quantity": quantity, "price": px, "amount": amount,
                                       "cash_change": -amount, "reason": "rebalance"})
                # 本次调仓按可用资金实际成交；费用引起的尾差不在后续每日追单。
                pending.discard(code)
            daily_turnover_amount = max(sale_amount, buy_amount)
            fee = transaction_cost * (sale_amount + buy_amount)
            self.cash -= fee
            fees += fee
            if abs(self.cash) <= _EPS:
                self.cash = 0.0
            if fee:
                ledger_entries.append({"date": current, "kind": "transaction_cost",
                                       "amount": fee, "cash_change": -fee})
            if start_equity > 0:
                event_turnover += event_sales / start_equity
                rebalance_turnover += max(0.0, daily_turnover_amount - event_sales) / start_equity
            if first_cash_weight is None and (can_rebalance or current == end):
                first_cash_weight = self.cash / self.equity if self.equity > 0 else 0.0
            # 登记日看收盘交易后的持有人；付款前卖出也不能丢掉已经确定的收息权。
            for code, (_, payments) in schedules.items():
                for payment in payments:
                    if (payment["kind"] == "coupon" and payment["record_date"] == current
                            and payment["date"] > current):
                        holding = self.holdings.get(code)
                        self.payment_entitlements[(code, current, payment["coupon_date"], payment["amount"])] = (
                            holding.quantity if holding else 0.0)
            # 含信号日收盘的成本点不覆盖初始 1.0；初始交易成本会保留在首个后续点中。
            if current > start and (current.weekday() < 5 or current == end):
                curve.append({"date": current, "equity": self.equity, "cash": self.cash,
                              "receivables": sum(r.amount for r in self.receivables)})
            current += timedelta(days=1)

        for code, holding in self.holdings.items():
            if code in pending and code not in desired:
                holding.pending_exit = holding.pending_exit or {"signal_date": start, "reason": "rebalance"}
        for rank, row in enumerate(selected, start=1):
            code = str(row.get("bond_code"))
            if code not in period_rows:
                skipped.append({"rank": rank, "bond_code": code, "bond_name": row.get("bond_name"),
                                "reason": "执行窗口无真实报价或可用现金不足，未成交"})
        ending_holdings = []
        blocked_weight = 0.0
        for code, row in period_rows.items():
            holding = self.holdings.get(code)
            ending = holding.quantity * holding.mark_price if holding else 0.0
            if holding:
                blocked = holding.pending_exit is not None
                status = "blocked" if blocked else "open"
                row.update({"end_price": holding.mark_price, "mark_date": holding.mark_date,
                            "quantity": holding.quantity, "position_status": status,
                            "valuation_stale": holding.mark_date < end,
                            "exit_reason": "no_exit_price" if blocked else "held"})
                if blocked:
                    blocked_weight += ending / self.equity if self.equity > 0 else 0.0
                ending_holdings.append({"bond_code": code, "quantity": holding.quantity,
                                        "market_value": ending, "mark_price": holding.mark_price,
                                        "mark_date": holding.mark_date, "position_status": status,
                                        "pending_exit": holding.pending_exit,
                                        "weight": ending / self.equity if self.equity > 0 else 0.0})
            else:
                row["quantity"] = 0.0
            invested = opening_values.get(code, 0.0) + buys.get(code, 0.0)
            pnl = ending + sells.get(code, 0.0) - invested
            income = incomes.get(code, 0.0)
            row.update({"weight": invested / start_equity if start_equity else 0.0,
                        "price_return": pnl / invested if invested else 0.0,
                        "period_return": (pnl + income) / invested if invested else 0.0,
                        "return_contribution": (pnl + income) / start_equity if start_equity else 0.0,
                        "coupon_income": income, "buy_amount": buys.get(code, 0.0),
                        "sale_amount": sells.get(code, 0.0), "ending_value": ending})
        outstanding = sum(r.amount for r in self.receivables)
        if outstanding:
            warnings.append("合同票息已记税前应收，实际支付与税款未核实；应收不能用于买入")
        if any(h.mark_date < end for h in self.holdings.values()):
            warnings.append("部分期末持仓沿用最近报价估值；缺价不代表可按该价卖出")
        total_return = self.equity / start_equity - 1.0 if start_equity else 0.0
        period_days = max(1, (end - start).days)
        self.last_date = end
        return {
            "accounting_basis": ACCOUNTING_BASIS,
            "weight_denominator": actual_denominator,
            "opening_equity": start_equity, "opening_cash": opening_cash,
            "equity": self.equity, "curve": curve,
            "positions": list(period_rows.values()), "skipped_positions": skipped,
            "period_return": total_return,
            "gross_return": total_return - (cash_interest - fees) / start_equity,
            "cash_yield_return": cash_interest / start_equity, "cost": fees / start_equity,
            "cash_weight": first_cash_weight if first_cash_weight is not None else opening_cash / start_equity,
            "average_cash_weight": cash_weight_days / period_days,
            "end_cash_weight": self.cash / self.equity if self.equity else 0.0,
            "turnover": rebalance_turnover + event_turnover,
            "rebalance_turnover": rebalance_turnover, "event_exit_turnover": event_turnover,
            "event_exit_count": event_exit_count, "event_exit_cash_yield_return": 0.0,
            "available_cash": self.cash, "coupon_income": coupon_income / start_equity,
            "coupon_cash": coupon_cash / start_equity, "redemption_cash": redemption_cash / start_equity,
            "receivable_value": outstanding, "receivable_weight": outstanding / self.equity if self.equity else 0.0,
            "receivable_nominal_value": sum(r.nominal_amount for r in self.receivables),
            "ending_holdings": ending_holdings,
            "blocked_count": sum(h.pending_exit is not None for h in self.holdings.values()),
            "blocked_weight": blocked_weight, "ledger_entries": ledger_entries,
            "cashflow_warnings": list(dict.fromkeys(warnings)),
        }

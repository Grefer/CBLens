"""数据同步: 拉条款 / 正股 / 历史 σ / Shibor / 评级利差."""
from __future__ import annotations

import logging
import subprocess
import sys
import threading
import tkinter as tk
from datetime import date, timedelta
from tkinter import filedialog, messagebox

import customtkinter as ctk

from ...cache import CachedBondDataProvider
from ...data_providers import (
    AkshareDataProvider,
    CSVDataProvider,
    DataProvider,
    WindDataProvider,
)
from ..constants import (
    BOND_CODE_RE,
    DEFAULT_CREDIT_SPREAD_PCT,
    DEFAULT_DISTRESS_K_PCT,
    DEFAULT_P_DOWN_PCT,
)
from ..theme import (
    BG_CARD, BG_INPUT, BTN_HOVER,
    CREDIT_SPREAD_TABLE,
    FONT_FAMILY, FONT_MONO, TEXT, TEXT_DIM,
    VOL_WINDOW_DEFAULT,
    VOL_WINDOW_MAP,
)


logger = logging.getLogger(__name__)


# CLI 入口表: (菜单标签, python -m 模块名, 额外 CLI 参数, 提示文案)
_POOL_SYNC_TARGETS = (
    ("🔄 增量更新基础信息 (推荐)", "convertible_bond.cli.sync_tradable", ["--incremental"],
     "只拉本地条款库中超过 7 天未刷新或新发行的债, 显著比全量快\n通常 1-3 分钟."),
    ("🌐 全量同步基础信息", "convertible_bond.cli.sync_tradable", [],
     "拉取全部可交易转债的发行条款 / 票息 / 转股价 / 评级 / 余额到本地条款库\n慢, 通常 5-15 分钟; 期间会占用 Wind 接口."),
    ("🚦 刷新准入状态", "convertible_bond.cli.sync_admission_status", [],
     "刷新停牌 / 强赎公告 / ST / 临近摘牌 / 成交额等准入字段\n通常 1-5 分钟."),
    ("📰 同步公告事件", "convertible_bond.cli.sync_events", [],
     "下载并解析公告标题, 写入本地事件表\n通常 1-3 分钟."),
)


class WindSyncMixin:
    """与 GUI 行情/条款数据同步相关的方法."""

    # ── 动态行情源管理 ──────────────────────────────────
    def _get_provider(self, name=None) -> DataProvider:
        """惰性构造动态行情 provider, 并叠加 cb_data 静态信息层."""
        name = name or self.v_data_source.get()
        if name in self._provider_cache:
            return self._provider_cache[name]
        try:
            if name == "Wind":
                inner: DataProvider = WindDataProvider()
            elif name == "akshare":
                inner = AkshareDataProvider()
            elif name == "CSV":
                if not self._csv_root:
                    raise RuntimeError("请先选择 CSV 数据根目录")
                inner = CSVDataProvider(self._csv_root)
            else:
                raise RuntimeError(f"未知行情源: {name}")
        except ImportError as e:
            raise RuntimeError(str(e)) from e
        # 转债基础信息固定从 cb_data 读取; 正股价格/历史 σ/Shibor 透传到 inner
        static_source = inner if isinstance(inner, WindDataProvider) else None
        provider = CachedBondDataProvider(
            inner,
            self.terms_cache,
            static_source=static_source,
            max_age_days=365,
        )
        self._provider_cache[name] = provider
        return provider

    def _on_data_source_change(self, choice):
        """切换动态行情源. CSV 兼容旧入口, 新界面默认不展示."""
        if choice == "CSV":
            path = filedialog.askdirectory(title="选择 CSV 数据根目录 (含 bonds/ stocks/ terms/ 子目录)")
            if not path:
                # 用户取消, 还原下拉选择
                prev = next((k for k in self._provider_cache if k != "CSV"), "Wind")
                self.v_data_source.set(prev)
                return
            self._csv_root = path
            # CSV 路径变更, 失效缓存
            self._provider_cache.pop("CSV", None)
        self.v_status.set(f"行情源已切换至 {choice}")
        code = self._normalize_bond_code(self.v_bond_code.get())
        if BOND_CODE_RE.match(code):
            if self.terms_cache.has(code):
                self._fill_from_cache(code)
            self._fetch_wind(auto=True)

    # ── 转债代码联想 ─────────────────────────────────────────
    def _search_bond_index(self, query: str, limit: int = 30):
        """在本地 cb_data.json 索引上做模糊匹配, 同时支持代码与中文简称.

        返回 [(code, "code  sec_name"), ...]; query 为空返回 []."""
        q = (query or "").strip().lower()
        if not q:
            return []
        prefix, contains = [], []
        for code, d in self.terms_cache._data.items():
            if code.startswith("_") or not isinstance(d, dict):
                continue
            name = (d.get("sec_name") or "")
            cl, nl = code.lower(), name.lower()
            if q in cl or q in nl:
                label = f"{code}  {name}" if name else code
                bucket = prefix if (cl.startswith(q) or nl.startswith(q)) else contains
                bucket.append((code, label))
        prefix.sort(key=lambda t: t[0])
        contains.sort(key=lambda t: t[0])
        return (prefix + contains)[:limit]

    def _fill_from_cache(self, code: str):
        """从本地 cb_data.json 直接填表, 不走网络.
        正股价 / σ / r 会在代码输入后自动异步同步."""
        code = self._normalize_bond_code(code)
        terms = self.terms_cache.get(code)
        if terms is None:
            return
        self._populate_down_reset_from_resolver(code, terms)
        iss_dt = terms.issue_date
        mat_dt = terms.maturity_date
        conv_dt = iss_dt + timedelta(days=180) if iss_dt else None
        put_years = None
        if terms.put_obs_months is not None and iss_dt and mat_dt:
            total_months = (mat_dt - iss_dt).days / 30.4375
            put_years = int(round(max(0, (total_months - float(terms.put_obs_months)) / 12)))
        self._fill_wind_data({
            "bond_code": code,
            "S0": None,
            "K": terms.conversion_price,
            "face": terms.face_value or 100.0,
            "mat_date": mat_dt,
            "iss_date": iss_dt,
            "conv_date": conv_dt,
            "redemp": float(terms.redemption_price) if terms.redemption_price is not None else 107.0,
            "call_ratio": terms.call_trigger_pct,
            "put_ratio": terms.put_trigger_pct,
            "put_years": put_years,
            "coupons_tuple": terms.coupon_rates,
            "coupon_src": "terms",
            "sigma": None,
            "shibor": None,
            "stock_code": terms.underlying_code,
            "sec_name": terms.sec_name,
            "close": terms.close,
            "credit": terms.credit_rating,
            "outstanding": terms.outstanding_balance,
            "provider_name": "本地",
            "market_source": self.v_data_source.get(),
            "terms_source": self._cache_meta_source(code),
            "terms_origin": "缓存",
            "cache_age": self.terms_cache.fetched_at(code),
        })

    # ── 数据同步 (拉条款 + 正股 + 历史 σ) ───────────────────
    def _fetch_wind(self, auto=False):
        code = self._normalize_bond_code(self.v_bond_code.get())
        if not code:
            messagebox.showwarning("提示", "请先输入转债代码, 例如 128009.SZ")
            return
        self._set_bond_code_safely(code)
        source_name = self.v_data_source.get()
        if self._fetch_in_flight_code == code and self._fetch_in_flight_source == source_name:
            return
        self._fetch_in_flight_code = code
        self._fetch_in_flight_source = source_name
        self.btn_wind.configure(state="disabled")
        if self._force_refresh_terms:
            msg = f"从 {source_name} 强制刷新 {code}"
        elif auto:
            msg = f"自动同步 {code} ({source_name})"
        else:
            msg = f"同步 {code} (基础信息优先读本地条款库)"
        vol_window_label = self.v_vol_window.get()
        self._start_progress(msg)
        threading.Thread(
            target=self._fetch_wind_worker,
            args=(code, auto, source_name, vol_window_label),
            daemon=True,
        ).start()

    def _refresh_terms(self):
        """强制用 Wind 刷新当前债的 cb_data."""
        code = self.v_bond_code.get().strip()
        if not code:
            messagebox.showwarning("提示", "请先输入转债代码")
            return
        self._force_refresh_terms = True
        self._fetch_wind()

    # ── 全市场池同步 (替代命令行 cb-sync-* 调用) ─────────────────
    def _open_pool_sync_menu(self):
        """弹出菜单选择: 同步基础 / 准入状态 / 公告事件."""
        menu = tk.Menu(self, tearoff=0, font=(FONT_FAMILY, 12))
        for label, module, extra_args, _desc in _POOL_SYNC_TARGETS:
            menu.add_command(
                label=label,
                command=lambda m=module, l=label, a=tuple(extra_args): self._run_pool_sync(m, l, a),
            )
        try:
            x = self.btn_sync_pool.winfo_rootx()
            y = self.btn_sync_pool.winfo_rooty() + self.btn_sync_pool.winfo_height()
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def _run_pool_sync(self, module: str, label: str, extra_args: tuple = ()):
        """运行 python -m <module> [extra_args...], 在弹窗里实时显示输出."""
        # 同一 module 可能对应多个菜单项 (全量 / 增量), 用 label 精确匹配 desc
        desc = next((d for lbl, mod, _, d in _POOL_SYNC_TARGETS
                     if mod == module and lbl == label), "")
        confirm = messagebox.askokcancel(
            label,
            f"{desc}\n\n继续执行?",
        )
        if not confirm:
            return

        win = ctk.CTkToplevel(self)
        win.title(f"{label} — 运行中")
        win.geometry("760x460")
        win.transient(self)

        ctk.CTkLabel(
            win, text=f"{label}", text_color=TEXT,
            font=(FONT_FAMILY, 14, "bold")).pack(anchor="w", padx=14, pady=(12, 4))
        status_var = ctk.StringVar(value="启动中…")
        ctk.CTkLabel(
            win, textvariable=status_var, text_color=TEXT_DIM,
            font=(FONT_FAMILY, 12)).pack(anchor="w", padx=14)

        text_box = ctk.CTkTextbox(
            win, fg_color=BG_INPUT, text_color=TEXT,
            font=(FONT_MONO, 11), wrap="word")
        text_box.pack(fill="both", expand=True, padx=14, pady=10)

        btn_row = ctk.CTkFrame(win, fg_color="transparent")
        btn_row.pack(fill="x", padx=14, pady=(0, 12))
        close_btn = ctk.CTkButton(
            btn_row, text="关闭", state="disabled", width=80,
            fg_color=BG_CARD, hover_color=BTN_HOVER, text_color=TEXT,
            command=win.destroy)
        close_btn.pack(side="right")

        proc_holder: dict = {}

        def _kill():
            proc = proc_holder.get("proc")
            if proc is None or proc.poll() is not None:
                return
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    # 卡死 / 忽略 SIGTERM 的子进程, 升级到 SIGKILL
                    proc.kill()
            except Exception:
                logger.exception("终止池同步子进程失败 (PID=%s)",
                                 getattr(proc, "pid", "?"))

        cancel_btn = ctk.CTkButton(
            btn_row, text="终止", width=80,
            fg_color=BG_CARD, hover_color=BTN_HOVER, text_color=TEXT,
            command=_kill)
        cancel_btn.pack(side="right", padx=(0, 8))

        win.protocol("WM_DELETE_WINDOW", lambda: (_kill(), win.destroy()))

        def append(line: str):
            text_box.insert("end", line)
            text_box.see("end")

        def worker():
            try:
                proc = subprocess.Popen(
                    [sys.executable, "-u", "-m", module, *extra_args],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                proc_holder["proc"] = proc
                self.after(0, lambda: status_var.set(f"PID {proc.pid} · 运行中…"))
                assert proc.stdout is not None
                for line in proc.stdout:
                    self.after(0, append, line)
                rc = proc.wait()
                if rc == 0:
                    self.after(0, lambda: status_var.set("✅ 完成"))
                else:
                    self.after(0, lambda: status_var.set(f"❌ 退出码 {rc}"))
            except Exception as exc:
                self.after(0, lambda: status_var.set(f"❌ 启动失败: {exc}"))
            finally:
                self.after(0, lambda: cancel_btn.configure(state="disabled"))
                self.after(0, lambda: close_btn.configure(state="normal"))
                if hasattr(self, "_update_data_freshness"):
                    self.after(0, self._update_data_freshness)

        threading.Thread(target=worker, daemon=True).start()

    def _fetch_wind_worker(self, code, auto=False, source_name=None, vol_window_label=None):
        force = self._force_refresh_terms
        self._force_refresh_terms = False
        source_name = source_name or self.v_data_source.get()
        vol_window_label = vol_window_label or VOL_WINDOW_DEFAULT
        try:
            provider = self._get_provider(source_name)
            market_source = self._provider_market_name(provider)
            val_date = date.today()

            had_cached = self.terms_cache.has(code)
            if force and isinstance(provider, CachedBondDataProvider):
                terms = provider.force_refresh(code, val_date)
                terms_origin = "Wind强制刷新"
            else:
                terms = provider.get_bond_terms(code, val_date)
                terms_origin = "cb_data" if had_cached and not force else "Wind刷新"

            stock_code = terms.underlying_code
            if not stock_code:
                raise ValueError("本地条款库未包含标的正股代码 — 请先用 Wind 刷新基础信息")

            try:
                S0 = provider.get_stock_close(stock_code, val_date)
            except Exception as exc:
                logger.warning("正股现价获取失败: %s", exc)
                S0 = float("nan")

            vol_win_days = VOL_WINDOW_MAP.get(vol_window_label, 126)
            try:
                sigma = provider.hist_vol(stock_code, val_date, vol_win_days)
            except Exception:
                sigma = None

            shibor_rate = None
            try:
                shibor_rate = provider.get_risk_free_rate(val_date)
            except Exception:
                shibor_rate = None

            iss_dt = terms.issue_date
            conv_dt = iss_dt + timedelta(days=180) if iss_dt else None

            cf = provider.get_cashflow(code)
            if cf and cf.coupon_rates:
                coupons_tuple = cf.coupon_rates
                coupon_src = "cashflow"
            else:
                coupons_tuple = terms.coupon_rates
                coupon_src = "terms"

            mat_dt = (cf.maturity_date if cf and cf.maturity_date else terms.maturity_date)

            if cf and cf.redemption_price is not None:
                redemp = float(cf.redemption_price)
            elif terms.redemption_price is not None:
                redemp = float(terms.redemption_price)
            else:
                redemp = 107.0

            put_years = None
            if terms.put_obs_months is not None and iss_dt and mat_dt:
                total_months = (mat_dt - iss_dt).days / 30.4375
                put_years = int(round(max(0, (total_months - float(terms.put_obs_months)) / 12)))

            self.after(0, self._fill_wind_data, {
                "bond_code": code,
                "S0": float(S0) if S0 == S0 else None,  # NaN check
                "K": terms.conversion_price,
                "face": terms.face_value or 100.0,
                "mat_date": mat_dt,
                "iss_date": iss_dt,
                "conv_date": conv_dt,
                "redemp": float(redemp),
                "call_ratio": terms.call_trigger_pct,
                "put_ratio": terms.put_trigger_pct,
                "put_years": put_years,
                "coupons_tuple": coupons_tuple,
                "coupon_src": coupon_src,
                "sigma": sigma,
                "shibor": shibor_rate,
                "stock_code": stock_code,
                "sec_name": terms.sec_name,
                "close": terms.close,
                "credit": terms.credit_rating,
                "outstanding": terms.outstanding_balance,
                "_terms": terms,
                "provider_name": provider.name,
                "market_source": market_source,
                "terms_source": self._cache_meta_source(code),
                "terms_origin": terms_origin,
                "cache_age": self.terms_cache.fetched_at(code),
                "vol_window": vol_window_label,
            })
        except Exception as exc:
            err_msg = f"{source_name} 获取失败: {exc}"
            self.after(0, self._on_error, err_msg, not auto)
        finally:
            if self._fetch_in_flight_code == code and self._fetch_in_flight_source == source_name:
                self._fetch_in_flight_code = None
                self._fetch_in_flight_source = None
            self.after(0, self._stop_progress)
            self.after(0, lambda: self.btn_wind.configure(state="normal"))

    def _fill_wind_data(self, d):
        data_code = d.get("bond_code")
        if data_code and self._normalize_bond_code(self.v_bond_code.get()) != data_code:
            return
        terms_for_dr = d.get("_terms") or self.terms_cache.get(data_code or "")
        if data_code and terms_for_dr is not None:
            self._populate_down_reset_from_resolver(data_code, terms_for_dr)
        if data_code:
            self._maybe_sync_events_background(data_code)

        origin_tag = d.get("terms_origin", "?")
        terms_label = self._terms_source_label(origin_tag)
        market_label = d.get("market_source") or self.v_data_source.get()
        coupon_src = d.get("coupon_src", "terms")
        coupon_label = "现金流" if coupon_src == "cashflow" else terms_label

        if d.get("S0") is not None:
            self._set_field(self.v_S0, f"{d['S0']:.4f}", self.v_src_S0, "行情")
        elif "S0" in d:
            self._set_field(self.v_S0, "", self.v_src_S0, "待行情")
        if d.get("K") is not None:
            self._set_field(self.v_K, f"{d['K']:.2f}", self.v_src_K, terms_label)
        if d.get("face") is not None:
            self._set_field(self.v_face, f"{d['face']:.0f}", self.v_src_face, terms_label)
        self._set_field(self.v_cur_date, date.today().isoformat(), self.v_src_cur_date, "系统")
        self._set_field(
            self.v_mat_date,
            d["mat_date"].isoformat() if d.get("mat_date") else "",
            self.v_src_mat_date,
            terms_label,
        )
        self._set_field(
            self.v_iss_date,
            d["iss_date"].isoformat() if d.get("iss_date") else "",
            self.v_src_iss_date,
            terms_label,
        )
        self._set_field(
            self.v_conv_date,
            d["conv_date"].isoformat() if d.get("conv_date") else "",
            self.v_src_conv_date,
            terms_label,
        )
        if d.get("redemp") is not None:
            self._set_field(self.v_redemp, f"{d['redemp']:.1f}", self.v_src_redemp, coupon_label)
        if d.get("call_ratio") is not None:
            self._set_field(self.v_call_ratio, f"{float(d['call_ratio']):.0f}", self.v_src_call_ratio, terms_label)
        if d.get("put_ratio") is not None:
            self._set_field(self.v_put_ratio, f"{float(d['put_ratio']):.0f}", self.v_src_put_ratio, terms_label)
        if d.get("put_years") is not None:
            self._set_field(self.v_put_years, f"{int(d['put_years'])}", self.v_src_put_years, terms_label)
        if d.get("sigma") is not None:
            self._set_field(self.v_sigma, f"{d['sigma'] * 100:.2f}", self.v_src_sigma, "历史")
        elif "sigma" in d:
            self._set_field(self.v_sigma, "", self.v_src_sigma, "待历史")
        if d.get("shibor") is not None:
            self._set_field(self.v_r, f"{d['shibor']:.2f}", self.v_src_r, "利率")

        parsed = d.get("coupons_tuple")
        if parsed:
            self._set_field(
                self.v_coupons,
                ",".join(f"{c*100:.2f}" for c in parsed),
                self.v_src_coupons,
                coupon_label,
            )

        self._last_stock_code = d.get("stock_code")
        self._last_credit = d.get("credit")

        if d.get("credit") and d["credit"] in CREDIT_SPREAD_TABLE:
            self._set_field(
                self.v_spread,
                f"{CREDIT_SPREAD_TABLE[d['credit']]:.1f}",
                self.v_src_spread,
                "评级",
            )
        elif "credit" in d:
            self._set_field(
                self.v_spread,
                f"{DEFAULT_CREDIT_SPREAD_PCT:.1f}",
                self.v_src_spread,
                "默认",
            )

        self._set_field(
            self.v_p_down,
            self._fmt_pct(d.get("p_down_pct", DEFAULT_P_DOWN_PCT)),
            self.v_src_p_down,
            "模型",
        )
        self._set_field(
            self.v_dk,
            self._fmt_pct(d.get("distress_k_pct", DEFAULT_DISTRESS_K_PCT)),
            self.v_src_dk,
            "模型",
        )

        if d.get("close") is not None:
            self._set_field(self.v_market_price, f"{float(d['close']):.2f}")

        ref_parts = []
        if d.get("sec_name"):
            ref_parts.append(str(d["sec_name"]))
        if d.get("close") is not None:
            ref_parts.append(f"市价 {float(d['close']):.2f}")
        if d.get("credit"):
            ref_parts.append(f"评级 {d['credit']}")
        if d.get("outstanding") is not None:
            ref_parts.append(f"剩余规模 {float(d['outstanding']):.2f} 亿")
        if d.get("cache_age") and origin_tag == "缓存":
            ref_parts.append(f"缓存日期 {d['cache_age'].strftime('%Y-%m-%d')}")
        self.v_ref_info.set("  ·  ".join(ref_parts) if ref_parts else "已加载")

        detail_parts = [f"条款来源：{terms_label}", f"行情来源：{market_label}"]
        source_parts = []
        if d.get("S0") is not None:
            source_parts.append(f"正股价：{market_label}")
        if d.get("sigma") is not None:
            source_parts.append(
                f"波动率：{market_label}历史 {d.get('vol_window') or self.v_vol_window.get()}")
        if d.get("shibor") is not None:
            source_parts.append(f"无风险利率：{market_label}")
        elif self.v_src_r.get() == "手工":
            source_parts.append("无风险利率：手工")
        source_parts.append(f"利差：{self.v_src_spread.get()}")
        source_parts.append("下修与信用扩张：模型")
        detail_parts.append("参数来源：" + "；".join(source_parts))
        self.v_ref_detail.set("\n".join(detail_parts))

        src_tag = "付息计划" if coupon_src == "cashflow" else "条款字段"
        s0_text = f"S₀={d['S0']:.3f}" if d.get("S0") is not None else "S₀=N/A"
        self.v_status.set(
            f"已加载 {self.v_bond_code.get()} (正股 {d.get('stock_code', '?')}, {s0_text}, 票息: {src_tag})"
        )
        # 标记最近一次行情拉取时间, 状态栏 "行情 Nmin前" 由此驱动
        from datetime import datetime as _dt
        self._last_quote_fetch_ts = _dt.now()
        if hasattr(self, "_update_data_freshness"):
            self._update_data_freshness()

    # ── 波动率窗口切换 ────────────────────────────────────
    def _on_vol_window_change(self, choice):
        if not self._last_stock_code:
            return
        days = VOL_WINDOW_MAP.get(choice, 126)
        self._start_progress(f"重算波动率 ({choice})")
        self.vol_window_menu.configure(state="disabled")
        threading.Thread(
            target=self._recompute_vol_worker,
            args=(self._last_stock_code, days),
            daemon=True,
        ).start()

    def _recompute_vol_worker(self, stock_code, days):
        try:
            provider = self._get_provider()
            sigma = provider.hist_vol(stock_code, date.today(), days)
            self.after(0, lambda: self._set_field(
                self.v_sigma, f"{sigma * 100:.2f}", self.v_src_sigma, "历史"))
            self.after(0, lambda: self.v_status.set(
                f"已按 {self.v_vol_window.get()} 窗口重算 σ = {sigma*100:.2f}%"
            ))
        except Exception as exc:
            self.after(0, self._on_error, f"重算 σ 失败: {exc}")
        finally:
            self.after(0, self._stop_progress)
            self.after(0, lambda: self.vol_window_menu.configure(state="normal"))

    # ── Shibor 1Y ────────────────────────────────────────
    def _fetch_shibor(self):
        self.btn_shibor.configure(state="disabled")
        self._start_progress("拉取无风险利率")
        threading.Thread(target=self._fetch_shibor_worker, daemon=True).start()

    def _fetch_shibor_worker(self):
        try:
            provider = self._get_provider()
            latest = provider.get_risk_free_rate(date.today())
            if latest is None:
                raise RuntimeError(f"{provider.name} 未返回有效无风险利率")
            self.after(0, lambda: self._set_field(
                self.v_r, f"{latest:.2f}", self.v_src_r, "利率"))
            self.after(0, lambda: self.v_status.set(
                f"无风险利率 ({provider.name}) = {latest:.4f}%"))
        except Exception as exc:
            self.after(0, self._on_error, f"无风险利率拉取失败: {exc}")
        finally:
            self.after(0, self._stop_progress)
            self.after(0, lambda: self.btn_shibor.configure(state="normal"))

    # ── 按评级填入信用利差 ────────────────────────────────
    def _fill_spread_from_rating(self):
        if not self._last_credit:
            messagebox.showinfo("提示", "请先点击 📥 同步获取条款, 取得评级后再按此按钮")
            return
        if self._last_credit not in CREDIT_SPREAD_TABLE:
            messagebox.showwarning(
                "提示",
                f"评级 '{self._last_credit}' 不在经验表中\n"
                f"已知评级: {', '.join(CREDIT_SPREAD_TABLE.keys())}"
            )
            return
        val = CREDIT_SPREAD_TABLE[self._last_credit]
        self._set_field(self.v_spread, f"{val:.1f}", self.v_src_spread, "评级")
        self.v_status.set(f"按评级 {self._last_credit} 填入信用利差 {val:.1f}%")

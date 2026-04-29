#!/usr/bin/env python3
"""
可转债理论定价 GUI — 主应用模块.

从原始 gui.py 拆分而来. UI 构建辅助件见 gui.theme / gui.widgets;
各 tab 的纯 UI 布局见 gui.tabs.* (业务逻辑方法仍留在本类中).
"""

import customtkinter as ctk
from tkinter import messagebox, filedialog
from datetime import date, datetime, timedelta
import csv
import json
import logging
import re
import threading
import numpy as np
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import matplotlib.pyplot as plt

# 解决 Matplotlib 中文显示和负号问题
matplotlib.rcParams['font.sans-serif'] = ['PingFang SC', 'Heiti TC', 'Arial Unicode MS', 'SimHei', 'Microsoft YaHei', 'sans-serif']
matplotlib.rcParams['axes.unicode_minus'] = False

from ..pricer import UniversalCBPricer, DEFAULT_COUPON_RATES
from ..data_providers import (
    to_date, parse_coupon_string as parse_coupon,
    DataProvider, WindDataProvider, AkshareDataProvider, CSVDataProvider,
    BondTerms,
)
from ..cache import CachedBondDataProvider, TermsBundle, project_bundle_path
from ..backtest import backtest_theoretical_price
from ..down_reset_overrides import (
    DEFAULT_COOLDOWN_MONTHS, default_overrides,
    reload_default_overrides, resolve_down_reset,
)
from ..data_providers import _add_months
from ..cb_events import (
    CBEventStore,
    apply_events_to_terms, project_events_path, reload_default_event_store,
)
from ..cb_event_sync import sync_cb_events

from .tabs import batch as batch_tab

from .theme import (
    BG_APP, BG_CARD, BG_INPUT, BORDER, TEXT, TEXT_DIM,
    ACCENT, ACCENT_HOVER, GREEN, RED, ORANGE,
    BTN_CTRL, BTN_HOVER,
    FONT_FAMILY, FONT_MONO,
    VOL_WINDOW_MAP, VOL_WINDOW_DEFAULT, CREDIT_SPREAD_TABLE,
    get_color,
)
from .widgets import (
    _form_row, create_card, CollapsibleSection, Tooltip,
    AutocompleteEntry, _latest_finite_number,
)

ctk.set_default_color_theme("blue")

BOND_CODE_RE = re.compile(r"^\d{6}\.[A-Z]{2}$")
DEFAULT_P_DOWN_PCT = 15.0
DEFAULT_DISTRESS_K_PCT = 5.0
DEFAULT_CREDIT_SPREAD_PCT = 3.0
EVENT_SYNC_STALE_HOURS = 24


class CBPricerApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("Dark") # 默认深色
        
        self.title("CBPricer")
        self.geometry("1280x900")
        self.minsize(1100, 800)
        self.configure(fg_color=BG_APP)
        
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._build_vars()
        self._animating = False
        self._build_ui()

    # ── 变量 ──────────────────────────────────────────────
    def _build_vars(self):
        self.v_bond_code = ctk.StringVar()
        self.v_S0        = ctk.StringVar(value="55.0")
        self.v_K         = ctk.StringVar(value="52.77")
        self.v_face      = ctk.StringVar(value="100")
        self.v_redemp    = ctk.StringVar(value="107")
        self.v_cur_date  = ctk.StringVar(value=date.today().isoformat())
        self.v_mat_date  = ctk.StringVar(value="2026-07-30")
        self.v_iss_date  = ctk.StringVar(value="2020-07-30")
        self.v_conv_date = ctk.StringVar(value="2021-02-06")
        self.v_coupons   = ctk.StringVar(value="0.3,0.4,0.8,1.5,1.8,2.0")
        self.v_sigma     = ctk.StringVar(value="28")
        self.v_r         = ctk.StringVar(value="2.2")
        self.v_spread    = ctk.StringVar(value="3.0")
        self.v_p_down    = ctk.StringVar(value=f"{DEFAULT_P_DOWN_PCT:g}")
        self.v_dk        = ctk.StringVar(value=f"{DEFAULT_DISTRESS_K_PCT:g}")
        # 下修事件覆盖 (per-bond) — 默认由 cb_events 自动解析; 面板仅作维护/确认
        self.v_dr_announce_date = ctk.StringVar(value="")
        self.v_dr_cooldown      = ctk.StringVar(value="")
        self.v_dr_p_scale       = ctk.StringVar(value="")
        self.v_dr_block_until   = ctk.StringVar(value="—")
        self.v_dr_note          = ctk.StringVar(value="")
        self.v_dr_status        = ctk.StringVar(value="无事件")
        self.v_call_ratio  = ctk.StringVar(value="130")
        self.v_put_ratio   = ctk.StringVar(value="70")
        self.v_put_years   = ctk.StringVar(value="2")
        self.v_call_notice = ctk.StringVar(value="30")
        self.v_M           = ctk.StringVar(value="500")
        self.v_N           = ctk.StringVar(value="2000")
        self.v_result      = ctk.StringVar(value="—")
        self.v_status      = ctk.StringVar(value="就绪")
        self.v_ref_info    = ctk.StringVar(value="尚未拉取数据")
        self.v_ref_detail  = ctk.StringVar(value="")
        self.v_vol_window  = ctk.StringVar(value=VOL_WINDOW_DEFAULT)
        self.v_theme       = ctk.StringVar(value="Dark")

        today = date.today()
        self.v_bt_start  = ctk.StringVar(value=(today - timedelta(days=180)).isoformat())
        self.v_bt_end    = ctk.StringVar(value=today.isoformat())
        self.v_bt_freq   = ctk.StringVar(value="周")
        self.v_bt_status = ctk.StringVar(value="输入转债代码 → 拉取参数 → 运行回测")
        self.v_bt_solve_iv = ctk.BooleanVar(value=True)
        self.v_bt_show_decomp = ctk.BooleanVar(value=True)

        # 价值分解 & 希腊值
        self.v_bond_floor   = ctk.StringVar(value="—")
        self.v_parity       = ctk.StringVar(value="—")
        self.v_option_prem  = ctk.StringVar(value="—")
        self.v_delta        = ctk.StringVar(value="—")
        self.v_gamma        = ctk.StringVar(value="—")
        self.v_vega         = ctk.StringVar(value="—")
        self.v_theta        = ctk.StringVar(value="—")

        # 隐含波动率反解 — 市价输入 & 反解结果
        self.v_market_price = ctk.StringVar(value="")
        self.v_iv           = ctk.StringVar(value="—")

        self.v_sens_status  = ctk.StringVar(value="设置参数范围后点击运行")

        self._sens_figure     = None
        self._sens_canvas     = None
        self._bt_figure       = None
        self._bt_canvas       = None
        self._last_bt_result  = None

        self._last_stock_code = None
        self._last_credit = None

        # 定价参数来源标签
        self.v_src_S0          = ctk.StringVar(value="手工")
        self.v_src_K           = ctk.StringVar(value="手工")
        self.v_src_face        = ctk.StringVar(value="手工")
        self.v_src_redemp      = ctk.StringVar(value="手工")
        self.v_src_cur_date    = ctk.StringVar(value="系统")
        self.v_src_mat_date    = ctk.StringVar(value="手工")
        self.v_src_iss_date    = ctk.StringVar(value="手工")
        self.v_src_conv_date   = ctk.StringVar(value="手工")
        self.v_src_coupons     = ctk.StringVar(value="手工")
        self.v_src_sigma       = ctk.StringVar(value="手工")
        self.v_src_r           = ctk.StringVar(value="手工")
        self.v_src_spread      = ctk.StringVar(value="手工")
        self.v_src_p_down      = ctk.StringVar(value="模型")
        self.v_src_dk          = ctk.StringVar(value="模型")
        self.v_src_call_ratio  = ctk.StringVar(value="手工")
        self.v_src_put_ratio   = ctk.StringVar(value="手工")
        self.v_src_put_years   = ctk.StringVar(value="手工")
        self.v_src_call_notice = ctk.StringVar(value="手工")

        self._programmatic_update = False
        self._suppress_bond_autoload = False
        self._auto_fetch_after = None
        self._last_auto_loaded_code = None
        self._fetch_in_flight_code = None
        self._fetch_in_flight_source = None
        self._source_trace_handles = []

        # 动态行情源
        self.v_data_source = ctk.StringVar(value="Wind")
        self._provider_cache: dict = {}      # name -> 已实例化的 provider (惰性)
        self._csv_root: str = ""             # CSV provider 的根目录

        # 转债静态信息缓存: 项目级单文件 bundle (data/cb_data.json)
        # 通过 `python -m convertible_bond.cli.sync_tradable` 批量更新
        self.terms_cache = TermsBundle(project_bundle_path())
        self._force_refresh_terms = False    # 下次 _fetch_wind 是否强制走网络

        # 事件表 (data/cb_events.json)
        self.event_store = CBEventStore(project_events_path())
        self.v_event_summary = ctk.StringVar(value="加载转债后显示事件")
        self._event_widgets: list = []       # 动态构建的事件行 widget 列表
        self._event_sync_in_flight: set[str] = set()

        self._attach_manual_source_tracking()
        self._bond_code_trace = self.v_bond_code.trace_add("write", self._on_bond_code_write)

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=0)
        self.grid_rowconfigure(1, weight=1)
        self.grid_rowconfigure(2, weight=0)
        self._build_header()
        self._build_tabview()
        self._build_statusbar()
        self._bind_shortcuts()

    def _build_header(self):
        self._tab_names = ["⚡ 定价", "📈 回测", "🔥 敏感性", "📦 批量"]
        
        header = ctk.CTkFrame(self, fg_color=BG_CARD, corner_radius=0, height=60)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(0, weight=1)
        header.grid_columnconfigure(1, weight=0)
        header.grid_columnconfigure(2, weight=1)
        header.grid_propagate(False)

        left_frame = ctk.CTkFrame(header, fg_color="transparent")
        left_frame.grid(row=0, column=0, sticky="w", padx=20, pady=15)
        ctk.CTkLabel(left_frame, text="CBPricer", font=(FONT_FAMILY, 20, "bold"), text_color=TEXT).pack(side="left")
        ctk.CTkLabel(left_frame, text="PRO", font=(FONT_FAMILY, 10, "bold"), text_color=ACCENT, 
                     fg_color=BG_INPUT, corner_radius=4, padx=6, pady=2).pack(side="left", padx=(8, 16), pady=(0, 2))
                     
        self.theme_switch = ctk.CTkSwitch(left_frame, text="深色模式", command=self._toggle_theme, width=40, progress_color=ACCENT, font=(FONT_FAMILY, 12), text_color=TEXT_DIM)
        self.theme_switch.pack(side="left")
        self.theme_switch.select()

        self.tab_seg = ctk.CTkSegmentedButton(
            header, values=self._tab_names, command=self._switch_tab,
            font=(FONT_FAMILY, 13, "bold"), height=30,
            selected_color=ACCENT, selected_hover_color=ACCENT_HOVER,
            unselected_color=BG_INPUT, unselected_hover_color=BTN_HOVER,
            text_color=TEXT, text_color_disabled=TEXT_DIM,
            corner_radius=8)
        self.tab_seg.set("⚡ 定价")
        self.tab_seg.grid(row=0, column=1, pady=15)

        right_frame = ctk.CTkFrame(header, fg_color="transparent")
        right_frame.grid(row=0, column=2, sticky="e", padx=20, pady=15)
        
        ctk.CTkLabel(right_frame, text="行情源", text_color=TEXT_DIM,
                     font=(FONT_FAMILY, 12, "bold")).pack(side="left", padx=(0, 4))
        self.data_source_menu = ctk.CTkOptionMenu(
            right_frame, variable=self.v_data_source,
            values=["Wind", "akshare"],
            command=self._on_data_source_change,
            width=88, height=30, font=(FONT_FAMILY, 12),
            fg_color=BG_INPUT, button_color=BTN_HOVER, text_color=TEXT,
            dropdown_fg_color=BG_INPUT, dropdown_text_color=TEXT, corner_radius=6)
        self.data_source_menu.pack(side="left", padx=(0, 10))
        Tooltip(self.data_source_menu,
                "选择动态行情/利率来源；转债基础信息固定读取 cb_data, 并由 Wind 刷新")

        AutocompleteEntry(
            right_frame, textvariable=self.v_bond_code,
            get_suggestions=self._search_bond_index,
            on_select=self._on_bond_code_selected,
            width=140, font=(FONT_MONO, 13), placeholder_text="如 128009.SZ",
            border_width=0, corner_radius=6, fg_color=BG_INPUT, height=30,
        ).pack(side="left")
        self.btn_wind = ctk.CTkButton(
            right_frame, text="📥 同步", command=self._fetch_wind,
            fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=("#ffffff", "#11111b"),
            font=(FONT_FAMILY, 12, "bold"), width=80, height=30, corner_radius=6)
        self.btn_wind.pack(side="left", padx=(8, 0))
        Tooltip(self.btn_wind, "读取 cb_data 静态信息 + 拉取正股 + 历史 σ")

        self.btn_refresh_terms = ctk.CTkButton(
            right_frame, text="🔄", command=self._refresh_terms,
            fg_color=BG_INPUT, hover_color=BTN_HOVER, text_color=TEXT,
            font=(FONT_FAMILY, 14), width=32, height=30, corner_radius=6)
        self.btn_refresh_terms.pack(side="left", padx=(4, 0))
        Tooltip(self.btn_refresh_terms,
                "强制用 Wind 刷新当前债的 cb_data\n(下修 / 评级变更后用)")
        
        self.btn_save = ctk.CTkButton(right_frame, text="💾", command=self._save_preset, width=30, height=30, fg_color=BG_INPUT, hover_color=BTN_HOVER, text_color=TEXT, font=(FONT_FAMILY, 14), corner_radius=6)
        self.btn_save.pack(side="left", padx=(12, 0))
        Tooltip(self.btn_save, "保存当前参数预设  (Ctrl+S)")
        self.btn_load = ctk.CTkButton(right_frame, text="📂", command=self._load_preset, width=30, height=30, fg_color=BG_INPUT, hover_color=BTN_HOVER, text_color=TEXT, font=(FONT_FAMILY, 14), corner_radius=6)
        self.btn_load.pack(side="left", padx=(6, 0))
        Tooltip(self.btn_load, "加载参数预设  (Ctrl+O)")

    def _build_statusbar(self):
        sb = ctk.CTkFrame(self, fg_color=BG_CARD, corner_radius=0, height=28)
        sb.grid(row=2, column=0, sticky="ew")
        sb.grid_columnconfigure(1, weight=1)
        sb.grid_propagate(False)
        
        ctk.CTkLabel(sb, text="信息", text_color=TEXT_DIM, font=(FONT_FAMILY, 11)).grid(row=0, column=0, sticky="w", padx=15, pady=4)
        self.lbl_ref = ctk.CTkLabel(sb, textvariable=self.v_ref_info, text_color=TEXT_DIM, font=(FONT_FAMILY, 11))
        self.lbl_ref.grid(row=0, column=1, sticky="w", padx=15, pady=4)
        Tooltip(self.lbl_ref, self.v_ref_detail)
        self.lbl_status = ctk.CTkLabel(sb, textvariable=self.v_status, text_color=TEXT, font=(FONT_FAMILY, 11, "bold"))
        self.lbl_status.grid(row=0, column=2, sticky="e", padx=15, pady=4)

    def _build_tabview(self):
        self._tab_frames = {}
        self._tab_container = ctk.CTkFrame(self, fg_color="transparent")
        self._tab_container.grid(row=1, column=0, sticky="nsew", padx=10, pady=10)
        self._tab_container.grid_columnconfigure(0, weight=1)
        self._tab_container.grid_rowconfigure(0, weight=1)

        for name in self._tab_names:
            f = ctk.CTkFrame(self._tab_container, fg_color="transparent")
            f.grid_columnconfigure(0, weight=1)
            f.grid_rowconfigure(0, weight=1)
            self._tab_frames[name] = f

        self._tab_frames["⚡ 定价"].grid(row=0, column=0, sticky="nsew")
        self._build_pricing_tab()
        self._build_backtest_tab()
        self._build_sensitivity_tab()
        batch_tab.build(self, self._tab_frames["📦 批量"])

    def _switch_tab(self, selected):
        for name, f in self._tab_frames.items():
            if name == selected:
                f.grid(row=0, column=0, sticky="nsew")
            else:
                f.grid_remove()

    # ── 字段来源与自动加载 ─────────────────────────────────
    def _attach_manual_source_tracking(self):
        pairs = (
            (self.v_S0, self.v_src_S0),
            (self.v_K, self.v_src_K),
            (self.v_face, self.v_src_face),
            (self.v_redemp, self.v_src_redemp),
            (self.v_cur_date, self.v_src_cur_date),
            (self.v_mat_date, self.v_src_mat_date),
            (self.v_iss_date, self.v_src_iss_date),
            (self.v_conv_date, self.v_src_conv_date),
            (self.v_coupons, self.v_src_coupons),
            (self.v_sigma, self.v_src_sigma),
            (self.v_r, self.v_src_r),
            (self.v_spread, self.v_src_spread),
            (self.v_p_down, self.v_src_p_down),
            (self.v_dk, self.v_src_dk),
            (self.v_call_ratio, self.v_src_call_ratio),
            (self.v_put_ratio, self.v_src_put_ratio),
            (self.v_put_years, self.v_src_put_years),
            (self.v_call_notice, self.v_src_call_notice),
        )
        for value_var, source_var in pairs:
            handle = value_var.trace_add(
                "write",
                lambda *_args, src=source_var: self._mark_manual_source(src),
            )
            self._source_trace_handles.append((value_var, handle))

    def _mark_manual_source(self, source_var):
        if self._programmatic_update:
            return
        source_var.set("手工")

    def _set_field(self, value_var, value, source_var=None, source=None):
        self._programmatic_update = True
        try:
            value_var.set(value)
        finally:
            self._programmatic_update = False
        if source_var is not None and source is not None:
            source_var.set(source)

    @staticmethod
    def _fmt_pct(value):
        return f"{float(value):.2f}".rstrip("0").rstrip(".")

    def _normalize_bond_code(self, raw: str) -> str:
        code = (raw or "").strip().upper()
        if not code:
            return ""
        if BOND_CODE_RE.match(code):
            return code
        if re.fullmatch(r"\d{6}", code):
            matches = [
                cached for cached in self.terms_cache.list_bonds()
                if cached.upper().startswith(f"{code}.")
            ]
            if len(matches) == 1:
                return matches[0].upper()
            if code.startswith(("110", "111", "113", "118")):
                return f"{code}.SH"
            if code.startswith(("123", "127", "128")):
                return f"{code}.SZ"
        return code

    def _set_bond_code_safely(self, code: str):
        if self.v_bond_code.get().strip() == code:
            return
        self._suppress_bond_autoload = True
        try:
            self.v_bond_code.set(code)
        finally:
            self._suppress_bond_autoload = False

    def _on_bond_code_write(self, *_):
        if self._suppress_bond_autoload:
            return
        code = self._normalize_bond_code(self.v_bond_code.get())
        if not BOND_CODE_RE.match(code):
            self._last_auto_loaded_code = None
            if self._auto_fetch_after is not None:
                self.after_cancel(self._auto_fetch_after)
                self._auto_fetch_after = None
            return
        if self._auto_fetch_after is not None:
            self.after_cancel(self._auto_fetch_after)
        self._auto_fetch_after = self.after(650, lambda c=code: self._auto_load_bond_code(c))

    def _on_bond_code_selected(self, code: str):
        norm = self._normalize_bond_code(code)
        if not BOND_CODE_RE.match(norm):
            return
        if self._auto_fetch_after is not None:
            self.after_cancel(self._auto_fetch_after)
            self._auto_fetch_after = None
        self._auto_load_bond_code(norm)

    def _auto_load_bond_code(self, code: str):
        self._auto_fetch_after = None
        code = self._normalize_bond_code(code)
        if not BOND_CODE_RE.match(code):
            return
        if self._normalize_bond_code(self.v_bond_code.get()) != code:
            return
        self._set_bond_code_safely(code)
        if (self._last_auto_loaded_code == code
                and self._fetch_in_flight_code == code
                and self._fetch_in_flight_source == self.v_data_source.get()):
            return
        self._last_auto_loaded_code = code
        if self.terms_cache.has(code):
            self._fill_from_cache(code)
        self._maybe_sync_events_background(code)
        self._fetch_wind(auto=True)

    def _cache_meta_source(self, code: str) -> str:
        try:
            raw = self.terms_cache._data.get(code, {})
            return str(raw.get("_meta", {}).get("source") or "cb_data")
        except Exception:
            return "cb_data"

    @staticmethod
    def _provider_market_name(provider) -> str:
        market = getattr(provider, "market", None)
        return getattr(market, "name", None) or getattr(provider, "name", "?")

    @staticmethod
    def _terms_source_label(origin: str) -> str:
        if not origin:
            return "条款"
        if "Wind" in origin:
            return "Wind"
        return "cb_data"

    def _build_down_reset_panel(self, parent):
        """下修事件覆盖面板.

        条款字段 (cooldown_months) 写回 cb_data.json;
        事件字段 (announce_date / p_scale / note) 写到 down_reset_overrides.json.
        触发 announce_date + cooldown → block_until 自动推算并显示.
        """
        card = create_card(parent, "下修事件参数", 0, 0, icon="🛡")
        _form_row(card, "不修正公告日", self.v_dr_announce_date, 0, width=130,
                  tooltip="手工覆盖入口。日常定价优先读取 cb_events, 通常无需填写。")
        _form_row(card, "再观察期", self.v_dr_cooldown, 1, width=80,
                  tooltip="单位: 月。没有公告正文承诺期时, 用公告日 + 再观察期推算冻结截止日。")
        _form_row(card, "p_scale", self.v_dr_p_scale, 2, width=80,
                  tooltip="冻结期结束后对下修强度 p_down 的乘子。留空表示不调整。")
        _form_row(card, "备注", self.v_dr_note, 3, width=240,
                  tooltip="手工记录覆盖依据。")
        _form_row(card, "屏蔽至", self.v_dr_block_until, 4, width=130,
                  tooltip="下修价值在该日期前被屏蔽。事件表有 effective_end 时会自动填入。")

        status_row = ctk.CTkFrame(card, fg_color="transparent")
        status_row.grid(row=5, column=0, sticky="ew", padx=16, pady=(2, 4))
        ctk.CTkLabel(status_row, textvariable=self.v_dr_status,
                     text_color=TEXT_DIM, font=(FONT_FAMILY, 11)).pack(side="left")

        btns = ctk.CTkFrame(card, fg_color="transparent")
        btns.grid(row=6, column=0, sticky="ew", padx=16, pady=(2, 8))
        ctk.CTkButton(btns, text="保存事件", command=self._save_down_reset_override,
                      fg_color=BTN_CTRL, hover_color=BTN_HOVER, text_color=ORANGE,
                      font=(FONT_FAMILY, 12, "bold"), width=85, height=28,
                      corner_radius=6).pack(side="left", padx=(0, 6))
        ctk.CTkButton(btns, text="清除事件", command=self._clear_down_reset_override,
                      fg_color=BTN_CTRL, hover_color=BTN_HOVER, text_color=TEXT_DIM,
                      font=(FONT_FAMILY, 12), width=85, height=28,
                      corner_radius=6).pack(side="left", padx=(0, 6))
        ctk.CTkButton(btns, text="cooldown→cb_data",
                      command=self._save_down_reset_cooldown_to_cb_data,
                      fg_color=BTN_CTRL, hover_color=BTN_HOVER, text_color=TEXT_DIM,
                      font=(FONT_FAMILY, 11), width=140, height=28,
                      corner_radius=6).pack(side="left")

    def _build_pricing_tab(self):
        tab = self._tab_frames["⚡ 定价"]
        tab.grid_columnconfigure(0, weight=0)
        tab.grid_columnconfigure(1, weight=1)
        tab.grid_rowconfigure(0, weight=1)

        # ── 左列: 参数面板 ──
        lp = ctk.CTkScrollableFrame(tab, fg_color="transparent", width=380,
                                    scrollbar_button_color=BORDER)
        lp.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        lp.grid_columnconfigure(0, weight=1)

        sec1 = create_card(lp, "定价核心", 0, 0, icon="⚡")
        def make_vol(p):
            self.vol_window_menu = ctk.CTkOptionMenu(
                p, variable=self.v_vol_window, values=list(VOL_WINDOW_MAP.keys()),
                width=75, font=(FONT_FAMILY, 12), fg_color=BORDER, button_color=BTN_HOVER,
                text_color=TEXT, dropdown_fg_color=BG_INPUT, dropdown_text_color=TEXT,
                command=self._on_vol_window_change)
            return self.vol_window_menu
        def make_shi(p):
            self.btn_shibor = ctk.CTkButton(
                p, text="Shibor", command=self._fetch_shibor, fg_color=BTN_CTRL,
                hover_color=BTN_HOVER, text_color=ORANGE,
                font=(FONT_FAMILY, 12, "bold"), width=75, height=28, corner_radius=6)
            return self.btn_shibor
        def make_spr(p):
            self.btn_spread = ctk.CTkButton(
                p, text="按评级", command=self._fill_spread_from_rating, fg_color=BTN_CTRL,
                hover_color=BTN_HOVER, text_color=ORANGE,
                font=(FONT_FAMILY, 12, "bold"), width=75, height=28, corner_radius=6)
            return self.btn_spread
        _form_row(sec1, "正股价 S", self.v_S0, 0, wind=True, source_var=self.v_src_S0,
                  tooltip="估值日附近正股收盘/最新价, 是转股价值和下修触发判断的核心输入。")
        _form_row(sec1, "转股价 K", self.v_K, 1, wind=True, source_var=self.v_src_K,
                  tooltip="当前转股价。转股价值 = S / K * 100。")
        _form_row(sec1, "波动率 σ (%)", self.v_sigma, 2, wind=True, width=80,
                  source_var=self.v_src_sigma,
                  tooltip="年化历史波动率。可在高级模型参数中切换估算窗口。")
        _form_row(sec1, "信用利差 (%)", self.v_spread, 3, width=80,
                  extra_widget=make_spr, source_var=self.v_src_spread,
                  tooltip="用于纯债折现和信用风险调整。可按评级经验表自动填入。")

        event_row = ctk.CTkFrame(sec1, fg_color="transparent")
        event_row.grid(row=4, column=0, sticky="ew", padx=16, pady=(6, 2))
        event_row.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(event_row, text="  事件状态", text_color=TEXT_DIM,
                     font=(FONT_FAMILY, 13)).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(event_row, textvariable=self.v_dr_status, text_color=TEXT,
                     font=(FONT_FAMILY, 12), width=180, anchor="e").grid(
            row=0, column=1, sticky="e")

        adv_terms = CollapsibleSection(lp, "条款明细", expanded=False)
        adv_terms.grid(row=1, column=0, sticky="ew", padx=6, pady=5)
        sec_terms = create_card(adv_terms.content, "条款与日期", 0, 0, icon="📄")
        _form_row(sec_terms, "面值", self.v_face, 0, wind=True, source_var=self.v_src_face,
                  tooltip="通常为 100。除特殊测试外无需修改。")
        _form_row(sec_terms, "到期赎回价", self.v_redemp, 1, wind=True, source_var=self.v_src_redemp,
                  tooltip="到期偿付价格, 含最后一期利息和赎回溢价。")
        _form_row(sec_terms, "估值日期", self.v_cur_date, 2, source_var=self.v_src_cur_date,
                  tooltip="模型当前日期。历史定价或复盘时可手动调整。")
        _form_row(sec_terms, "到期日期", self.v_mat_date, 3, wind=True, source_var=self.v_src_mat_date)
        _form_row(sec_terms, "发行日期", self.v_iss_date, 4, wind=True, source_var=self.v_src_iss_date)
        _form_row(sec_terms, "转股起始日", self.v_conv_date, 5, wind=True, source_var=self.v_src_conv_date)
        _form_row(sec_terms, "各年票息 (%)", self.v_coupons, 6, wind=True, width=180, source_var=self.v_src_coupons,
                  tooltip="逐年票息百分比, 逗号分隔。")
        _form_row(sec_terms, "强赎触发 (%K)", self.v_call_ratio, 7, wind=True, source_var=self.v_src_call_ratio,
                  tooltip="正股价格达到转股价的该比例附近时触发强赎条款。")
        _form_row(sec_terms, "回售触发 (%K)", self.v_put_ratio, 8, wind=True, source_var=self.v_src_put_ratio,
                  tooltip="正股价格低于转股价的该比例附近时触发回售条款。")
        _form_row(sec_terms, "回售生效年数", self.v_put_years, 9, wind=True, source_var=self.v_src_put_years)
        _form_row(sec_terms, "强赎宽限天数", self.v_call_notice, 10, source_var=self.v_src_call_notice,
                  tooltip="公告强赎后的缓冲窗口。用于近似宽限期内的股票选择权。")

        adv_model = CollapsibleSection(lp, "高级模型参数", expanded=False)
        adv_model.grid(row=2, column=0, sticky="ew", padx=6, pady=5)
        sec2 = create_card(adv_model.content, "利率与风险参数", 0, 0, icon="⚙️")

        vol_row = ctk.CTkFrame(sec2, fg_color="transparent")
        vol_row.grid(row=0, column=0, sticky="ew", padx=16, pady=4)
        vol_row.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(vol_row, text="  波动率窗口", text_color=TEXT_DIM,
                     font=(FONT_FAMILY, 13)).grid(row=0, column=0, sticky="w")
        vol_box = ctk.CTkFrame(vol_row, fg_color="transparent")
        vol_box.grid(row=0, column=1, sticky="e")
        make_vol(vol_box).pack(side="left")
        Tooltip(vol_row, "用于重新估算 σ 的历史窗口。修改后会重算当前正股的年化波动率。")

        _form_row(sec2, "无风险利率 r (%)", self.v_r, 1, width=80,
                  extra_widget=make_shi, source_var=self.v_src_r,
                  tooltip="无风险利率, 默认可用 Shibor 1Y 近似。")
        _form_row(sec2, "下修强度 p (%/年)", self.v_p_down, 2, source_var=self.v_src_p_down,
                  tooltip="年化下修事件强度。公告不下修冻结期内会被事件表自动屏蔽。")
        _form_row(sec2, "信用扩张系数 (%)", self.v_dk, 3, source_var=self.v_src_dk,
                  tooltip="正股越低时信用利差扩张的幅度参数。")
        sec4 = create_card(adv_model.content, "数值网格", 1, 0, icon="🧮")
        _form_row(sec4, "空间节点 M", self.v_M, 0,
                  tooltip="PDE 空间网格。越大越精细, 也越慢。")
        _form_row(sec4, "时间步数 N", self.v_N, 1,
                  tooltip="PDE 时间网格。越大越精细, 也越慢。")

        dr_sec = CollapsibleSection(lp, "维护: 下修覆盖", expanded=False)
        dr_sec.grid(row=3, column=0, sticky="ew", padx=6, pady=5)
        self._build_down_reset_panel(dr_sec.content)

        # ── 事件面板 ──
        ev_sec = CollapsibleSection(lp, "公告事件", expanded=False)
        ev_sec.grid(row=4, column=0, sticky="ew", padx=6, pady=5)
        self._build_events_panel(ev_sec.content)

        # ── 右列: 结果面板 ──
        rp = ctk.CTkFrame(tab, fg_color="transparent")
        rp.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        rp.grid_columnconfigure(0, weight=1)
        rp.grid_rowconfigure(1, weight=1)

        # 英雄结果卡
        rc = ctk.CTkFrame(rp, fg_color=BG_CARD, corner_radius=16)
        rc.grid(row=0, column=0, sticky="ew", pady=(6, 12))
        rc.grid_columnconfigure(0, weight=1)
        rc.grid_columnconfigure(1, weight=1)

        left_hero = ctk.CTkFrame(rc, fg_color="transparent")
        left_hero.grid(row=0, column=0, sticky="nw", padx=30, pady=25)
        
        self.btn_calc = ctk.CTkButton(
            left_hero, text="✨ 开始计算 (Ctrl+Enter)", command=self._run_pricing,
            font=(FONT_FAMILY, 15, "bold"), width=200, height=50, corner_radius=10,
            fg_color=("#1e66f5", "#0052cc"), hover_color=("#7287fd", "#0066ff"),
            text_color=("#ffffff", "#ffffff"))
        self.btn_calc.pack(anchor="w", pady=(0, 15))
        
        self.progress_bar = ctk.CTkProgressBar(
            left_hero, orientation="horizontal", mode="indeterminate",
            width=200, height=4, corner_radius=2, progress_color=ACCENT, fg_color=BG_INPUT)
        self.progress_bar.pack(anchor="w", pady=(0, 10))
        self.progress_bar.set(0)

        right_hero = ctk.CTkFrame(rc, fg_color="transparent")
        right_hero.grid(row=0, column=1, sticky="ne", padx=30, pady=25)
        
        ctk.CTkLabel(right_hero, text="理论价格 (¥)", font=(FONT_FAMILY, 13),
                     text_color=TEXT_DIM).pack(anchor="e")
        self.lbl_result = ctk.CTkLabel(right_hero, textvariable=self.v_result,
                                       font=(FONT_FAMILY, 56, "bold"), text_color=TEXT)
        self.lbl_result.pack(anchor="e")

        # IV 工具栏
        tb = ctk.CTkFrame(rc, fg_color="transparent")
        tb.grid(row=1, column=0, columnspan=2, sticky="ew", padx=30, pady=(0, 25))
        
        ctk.CTkLabel(tb, text="🎯 隐含波动率反解", text_color=TEXT_DIM,
                     font=(FONT_FAMILY, 13, "bold")).pack(side="left", padx=(0, 15))
        ctk.CTkEntry(tb, textvariable=self.v_market_price, width=80,
                     font=(FONT_MONO, 13), fg_color=BG_INPUT, border_width=0, corner_radius=6,
                     placeholder_text="市价 ¥").pack(side="left", padx=(0, 8))
        self.btn_iv = ctk.CTkButton(
            tb, text="解 IV", command=self._solve_iv,
            fg_color=BTN_CTRL, hover_color=BTN_HOVER, text_color=ORANGE,
            font=(FONT_FAMILY, 12, "bold"), width=70, height=28, corner_radius=6)
        self.btn_iv.pack(side="left", padx=(0, 15))
        
        ctk.CTkLabel(tb, text="IV =", text_color=TEXT_DIM,
                     font=(FONT_FAMILY, 12)).pack(side="left", padx=(0, 4))
        ctk.CTkLabel(tb, textvariable=self.v_iv, text_color=ORANGE,
                     font=(FONT_MONO, 14, "bold"), width=70, anchor="w").pack(side="left", padx=(0, 20))
                     
        self.btn_conv = ctk.CTkButton(
            tb, text="🩺 收敛诊断", command=self._convergence_check,
            fg_color=BTN_CTRL, hover_color=BTN_HOVER, text_color=TEXT_DIM,
            font=(FONT_FAMILY, 12, "bold"), width=90, height=28, corner_radius=6)
        self.btn_conv.pack(side="right")
        self.btn_cashflow = ctk.CTkButton(
            tb, text="💰 现金流", command=self._show_cashflow,
            fg_color=BTN_CTRL, hover_color=BTN_HOVER, text_color=TEXT_DIM,
            font=(FONT_FAMILY, 12, "bold"), width=90, height=28, corner_radius=6)
        self.btn_cashflow.pack(side="right", padx=(0, 8))

        # 指标仪表盘
        dc = ctk.CTkFrame(rp, fg_color="transparent")
        dc.grid(row=1, column=0, sticky="nsew", pady=(0, 6))
        dc.grid_columnconfigure((0, 1, 2, 3), weight=1, uniform="dec")
        dc.grid_rowconfigure((0, 1), weight=1, uniform="r")

        def _tile(parent, row, col, label, var, hl=False):
            t = ctk.CTkFrame(parent, fg_color=BG_CARD, corner_radius=16)
            t.grid(row=row, column=col, sticky="nsew", padx=5, pady=5)
            t.grid_columnconfigure(0, weight=1)
            t.grid_rowconfigure(1, weight=1)
            ctk.CTkLabel(t, text=label, text_color=TEXT_DIM,
                         font=(FONT_FAMILY, 12, "bold")).grid(row=0, column=0, sticky="w", padx=16, pady=(16, 0))
            val_color = ACCENT if hl else TEXT
            ctk.CTkLabel(t, textvariable=var, text_color=val_color,
                         font=(FONT_MONO, 20, "bold")).grid(row=1, column=0, sticky="w", padx=16, pady=(0, 16))

        _tile(dc, 0, 0, "🏷️ 纯债价值", self.v_bond_floor)
        _tile(dc, 0, 1, "🔄 转股价值", self.v_parity)
        _tile(dc, 0, 2, "✨ 期权溢价", self.v_option_prem, hl=True)
        _tile(dc, 0, 3, "Δ Delta", self.v_delta)
        _tile(dc, 1, 0, "Γ Gamma", self.v_gamma)
        _tile(dc, 1, 1, "ν Vega", self.v_vega)
        _tile(dc, 1, 2, "Θ Theta", self.v_theta)
        _tile(dc, 1, 3, "🎯 隐含波动率", self.v_iv)
    def _build_backtest_tab(self):
        tab = self._tab_frames["📈 回测"]
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(2, weight=1)

        # 控制栏
        ctrl = ctk.CTkFrame(tab, fg_color=BG_CARD, corner_radius=16)
        ctrl.grid(row=0, column=0, sticky="ew", pady=(6, 12), padx=6)

        ch = ctk.CTkFrame(ctrl, fg_color="transparent")
        ch.grid(row=0, column=0, sticky="ew", padx=20, pady=(15, 8))
        ctk.CTkLabel(ch, text="📈 历史回测对比",
                     font=(FONT_FAMILY, 16, "bold"), text_color=TEXT).pack(side="left")
        ctk.CTkLabel(ch, text="理论价 vs 实际收盘 (条款/模型参数 = 当前界面值)",
                     font=(FONT_FAMILY, 12), text_color=TEXT_DIM).pack(side="left", padx=(12, 0))

        cc = ctk.CTkFrame(ctrl, fg_color="transparent")
        cc.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 15))
        ctk.CTkLabel(cc, text="开始", text_color=TEXT_DIM, font=(FONT_FAMILY, 13)).pack(side="left", padx=(0, 4))
        ctk.CTkEntry(cc, textvariable=self.v_bt_start, width=110, font=(FONT_MONO, 13),
                     fg_color=BG_INPUT, border_width=0, corner_radius=6).pack(side="left", padx=(0, 12))
        ctk.CTkLabel(cc, text="结束", text_color=TEXT_DIM, font=(FONT_FAMILY, 13)).pack(side="left", padx=(0, 4))
        ctk.CTkEntry(cc, textvariable=self.v_bt_end, width=110, font=(FONT_MONO, 13),
                     fg_color=BG_INPUT, border_width=0, corner_radius=6).pack(side="left", padx=(0, 12))
        ctk.CTkLabel(cc, text="频率", text_color=TEXT_DIM, font=(FONT_FAMILY, 13)).pack(side="left", padx=(0, 4))
        ctk.CTkOptionMenu(cc, variable=self.v_bt_freq, values=["日", "周", "月"],
                          width=70, font=(FONT_FAMILY, 12), fg_color=BG_INPUT, button_color=BTN_HOVER,
                          text_color=TEXT, dropdown_fg_color=BG_INPUT, dropdown_text_color=TEXT).pack(side="left", padx=(0, 15))
        self.btn_backtest = ctk.CTkButton(
            cc, text="📊 运行回测", command=self._run_backtest,
            fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=("#ffffff", "#11111b"),
            font=(FONT_FAMILY, 13, "bold"), width=110, height=32, corner_radius=6)
        self.btn_backtest.pack(side="left")
        ctk.CTkCheckBox(
            cc, text="价值分解", variable=self.v_bt_show_decomp,
            command=self._refresh_backtest_chart,
            font=(FONT_FAMILY, 12), text_color=TEXT_DIM, fg_color=ACCENT,
            checkbox_width=16, checkbox_height=16,
            border_width=1, corner_radius=3).pack(side="left", padx=(15, 0))
        ctk.CTkCheckBox(
            cc, text="反解 IV", variable=self.v_bt_solve_iv,
            font=(FONT_FAMILY, 12), text_color=TEXT_DIM, fg_color=ACCENT,
            checkbox_width=16, checkbox_height=16,
            border_width=1, corner_radius=3).pack(side="left", padx=(10, 0))
        self.btn_bt_png = ctk.CTkButton(
            cc, text="📸 PNG", command=self._export_bt_png,
            fg_color=BG_INPUT, hover_color=BTN_HOVER, text_color=TEXT,
            font=(FONT_FAMILY, 12), width=75, height=32, corner_radius=6, state="disabled")
        self.btn_bt_png.pack(side="left", padx=(10, 0))
        self.btn_bt_csv = ctk.CTkButton(
            cc, text="📝 CSV", command=self._export_bt_csv,
            fg_color=BG_INPUT, hover_color=BTN_HOVER, text_color=TEXT,
            font=(FONT_FAMILY, 12), width=75, height=32, corner_radius=6, state="disabled")
        self.btn_bt_csv.pack(side="left", padx=(6, 0))

        self.lbl_bt_status = ctk.CTkLabel(
            tab, textvariable=self.v_bt_status,
            font=(FONT_FAMILY, 12), text_color=TEXT_DIM)
        self.lbl_bt_status.grid(row=1, column=0, sticky="w", padx=16, pady=(0, 6))

        self.bt_chart_frame = ctk.CTkFrame(tab, fg_color=BG_CARD, corner_radius=16)
        self.bt_chart_frame.grid(row=2, column=0, sticky="nsew", padx=6, pady=(0, 6))
        self.bt_chart_frame.grid_columnconfigure(0, weight=1)
        self.bt_chart_frame.grid_rowconfigure(0, weight=1)

    def _bind_shortcuts(self):
        self.bind_all("<Control-Return>", lambda e: self._run_pricing())
        self.bind_all("<Control-s>", lambda e: self._save_preset())
        self.bind_all("<Control-o>", lambda e: self._load_preset())
    def _build_sensitivity_tab(self):
        tab = self._tab_frames["🔥 敏感性"]
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(2, weight=1)

        ctrl = ctk.CTkFrame(tab, fg_color=BG_CARD, corner_radius=16)
        ctrl.grid(row=0, column=0, sticky="ew", pady=(6, 12), padx=6)

        ch = ctk.CTkFrame(ctrl, fg_color="transparent")
        ch.grid(row=0, column=0, sticky="ew", padx=20, pady=(15, 8))
        ctk.CTkLabel(ch, text="🔥 敏感性分析 (σ-S Heatmap)",
                     font=(FONT_FAMILY, 16, "bold"), text_color=TEXT).pack(side="left")
        ctk.CTkLabel(ch, text="固定其他参数，遍历 (波动率, 正股价) 网格",
                     font=(FONT_FAMILY, 12), text_color=TEXT_DIM).pack(side="left", padx=(12, 0))

        cc = ctk.CTkFrame(ctrl, fg_color="transparent")
        cc.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 15))

        self.v_sens_s_min = ctk.StringVar(value="70")
        self.v_sens_s_max = ctk.StringVar(value="130")
        self.v_sens_sig_min = ctk.StringVar(value="10")
        self.v_sens_sig_max = ctk.StringVar(value="60")
        self.v_sens_steps = ctk.StringVar(value="12")

        ctk.CTkLabel(cc, text="S (%K)", text_color=TEXT_DIM, font=(FONT_FAMILY, 13)).pack(side="left", padx=(0, 4))
        ctk.CTkEntry(cc, textvariable=self.v_sens_s_min, width=50, font=(FONT_MONO, 13),
                     fg_color=BG_INPUT, border_width=0, corner_radius=6).pack(side="left", padx=(0, 2))
        ctk.CTkLabel(cc, text="~", text_color=TEXT_DIM, font=(FONT_FAMILY, 13)).pack(side="left", padx=2)
        ctk.CTkEntry(cc, textvariable=self.v_sens_s_max, width=50, font=(FONT_MONO, 13),
                     fg_color=BG_INPUT, border_width=0, corner_radius=6).pack(side="left", padx=(0, 15))

        ctk.CTkLabel(cc, text="σ (%)", text_color=TEXT_DIM, font=(FONT_FAMILY, 13)).pack(side="left", padx=(0, 4))
        ctk.CTkEntry(cc, textvariable=self.v_sens_sig_min, width=50, font=(FONT_MONO, 13),
                     fg_color=BG_INPUT, border_width=0, corner_radius=6).pack(side="left", padx=(0, 2))
        ctk.CTkLabel(cc, text="~", text_color=TEXT_DIM, font=(FONT_FAMILY, 13)).pack(side="left", padx=2)
        ctk.CTkEntry(cc, textvariable=self.v_sens_sig_max, width=50, font=(FONT_MONO, 13),
                     fg_color=BG_INPUT, border_width=0, corner_radius=6).pack(side="left", padx=(0, 15))

        ctk.CTkLabel(cc, text="网格", text_color=TEXT_DIM, font=(FONT_FAMILY, 13)).pack(side="left", padx=(0, 4))
        ctk.CTkEntry(cc, textvariable=self.v_sens_steps, width=40, font=(FONT_MONO, 13),
                     fg_color=BG_INPUT, border_width=0, corner_radius=6).pack(side="left", padx=(0, 15))

        self.btn_sensitivity = ctk.CTkButton(
            cc, text="🔥 运行分析", command=self._run_sensitivity,
            fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=("#ffffff", "#11111b"),
            font=(FONT_FAMILY, 13, "bold"), width=110, height=32, corner_radius=6)
        self.btn_sensitivity.pack(side="left")

        self.lbl_sens_status = ctk.CTkLabel(
            tab, textvariable=self.v_sens_status, font=(FONT_FAMILY, 12), text_color=TEXT_DIM)
        self.lbl_sens_status.grid(row=1, column=0, sticky="sw", padx=16, pady=(0, 6))

        self.sens_chart_frame = ctk.CTkFrame(tab, fg_color=BG_CARD, corner_radius=16)
        self.sens_chart_frame.grid(row=2, column=0, sticky="nsew", padx=6, pady=(0, 6))
        self.sens_chart_frame.grid_columnconfigure(0, weight=1)
        self.sens_chart_frame.grid_rowconfigure(0, weight=1)

    def _run_sensitivity(self):
        try:
            params = self._collect_params()
        except Exception as exc:
            messagebox.showerror("参数错误", str(exc))
            return
        try:
            s_min = float(self.v_sens_s_min.get()) / 100.0
            s_max = float(self.v_sens_s_max.get()) / 100.0
            sig_min = float(self.v_sens_sig_min.get()) / 100.0
            sig_max = float(self.v_sens_sig_max.get()) / 100.0
            steps = int(self.v_sens_steps.get())
        except ValueError:
            messagebox.showerror("错误", "请输入有效的范围参数")
            return
        if steps < 3 or steps > 30:
            messagebox.showwarning("提示", "网格步数建议 3~30")
            return
        self.btn_sensitivity.configure(state="disabled")
        self._start_progress("正在计算敏感性网格")
        threading.Thread(
            target=self._sensitivity_worker,
            args=(params, s_min, s_max, sig_min, sig_max, steps),
            daemon=True).start()

    def _sensitivity_worker(self, params, s_min, s_max, sig_min, sig_max, steps):
        try:
            K = params["pricer"]["K"]
            S_vals = np.linspace(K * s_min, K * s_max, steps)
            sig_vals = np.linspace(sig_min, sig_max, steps)
            m = params["model"]
            m_fast = dict(m, M=max(100, m["M"] // 4), N=max(500, m["N"] // 4))
            grid = np.zeros((steps, steps))
            total = steps * steps
            done = [0]

            def compute_one(i, j):
                p = dict(params["pricer"], S0=float(S_vals[j]))
                pricer = UniversalCBPricer(**p)
                return i, j, float(pricer.price(
                    sigma=float(sig_vals[i]), r=m_fast["r"],
                    base_spread=m_fast["base_spread"], p_down=m_fast["p_down"],
                    distress_k=m_fast["distress_k"], M=m_fast["M"], N=m_fast["N"]))

            with ThreadPoolExecutor(max_workers=4) as pool:
                futs = [pool.submit(compute_one, i, j)
                        for i in range(steps) for j in range(steps)]
                for fut in as_completed(futs):
                    i, j, v = fut.result()
                    grid[i, j] = v
                    done[0] += 1
                    if done[0] % max(1, total // 20) == 0:
                        self.after(0, lambda d=done[0]: self.v_sens_status.set(
                            f"进度 {d}/{total} ..."))

            self.after(0, self._render_sensitivity_chart, S_vals, sig_vals, grid, K)
        except Exception as exc:
            self.after(0, lambda: self.v_sens_status.set(f"❌ 敏感性分析失败: {exc}"))
            self.after(0, lambda: messagebox.showerror("敏感性分析失败", str(exc)))
        finally:
            self.after(0, self._stop_progress)
            self.after(0, lambda: self.btn_sensitivity.configure(state="normal"))

    def _render_sensitivity_chart(self, S_vals, sig_vals, grid, K):
        if self._sens_figure is not None:
            self._sens_figure.clf()
            plt.close(self._sens_figure)
            self._sens_figure = None
            self._sens_canvas = None
        for child in self.sens_chart_frame.winfo_children():
            child.destroy()

        bg = get_color(BG_CARD)
        bg_in = get_color(BG_INPUT)
        txt = get_color(TEXT)
        txt_dim = get_color(TEXT_DIM)
        brd = get_color(BORDER)

        fig = Figure(figsize=(11, 5), dpi=100, facecolor=bg)
        ax = fig.add_subplot(111, facecolor=bg_in)

        vmin, vmax = float(np.min(grid)), float(np.max(grid))
        center = 100.0
        if vmin < center < vmax:
            from matplotlib.colors import TwoSlopeNorm
            norm = TwoSlopeNorm(vmin=vmin, vcenter=center, vmax=vmax)
        else:
            norm = None

        im = ax.pcolormesh(S_vals, sig_vals * 100, grid, cmap="RdYlGn",
                           norm=norm, shading="auto")
        cbar = fig.colorbar(im, ax=ax, pad=0.02)
        cbar.set_label("理论价 (¥)", color=txt_dim, fontsize=10)
        cbar.ax.tick_params(colors=txt_dim, labelsize=9)

        # Mark current point
        try:
            cur_s = float(self.v_S0.get())
            cur_sig = float(self.v_sigma.get())
            ax.plot(cur_s, cur_sig, marker="*", markersize=18,
                    color=get_color(ACCENT), markeredgecolor="white",
                    markeredgewidth=1.5, zorder=5)
        except ValueError:
            pass

        ax.set_xlabel("正股价 S (¥)", color=txt_dim, fontsize=11)
        ax.set_ylabel("波动率 σ (%)", color=txt_dim, fontsize=11)
        ax.set_title("理论价 σ-S 敏感性热力图", color=txt, fontsize=13, fontweight="bold")
        ax.tick_params(colors=txt_dim, labelsize=9)
        for spine in ax.spines.values():
            spine.set_color(brd)

        fig.tight_layout()
        canvas = FigureCanvasTkAgg(fig, master=self.sens_chart_frame)
        canvas.draw()
        canvas.get_tk_widget().grid(row=0, column=0, sticky="nsew")
        self._sens_figure = fig
        self._sens_canvas = canvas
        self.v_sens_status.set(
            f"✅ {len(S_vals)}×{len(sig_vals)} = {len(S_vals)*len(sig_vals)} 点  |  "
            f"价格范围 {float(np.min(grid)):.2f} ~ {float(np.max(grid)):.2f}")

    def _toggle_theme(self):
        """切换深浅色模式"""
        if self.theme_switch.get() == 1:
            ctk.set_appearance_mode("Dark")
            self.theme_switch.configure(text="深色模式")
        else:
            ctk.set_appearance_mode("Light")
            self.theme_switch.configure(text="浅色模式")
            
        # 刷新 matplotlib 图表色彩
        if self._last_bt_result is not None:
            self._render_backtest_chart(self._last_bt_result)

    # ── 进度动画 ────────────────────────────────────────
    def _start_progress(self, base_msg):
        self._animating = True
        self._anim_base = base_msg
        self._anim_step = 0
        if hasattr(self, 'progress_bar'):
            self.progress_bar.start()
        self._tick_progress()

    def _tick_progress(self):
        if not self._animating:
            return
        dots = "." * (self._anim_step % 4)
        self.v_status.set(f"{self._anim_base}{dots}")
        self._anim_step += 1
        self.after(400, self._tick_progress)

    def _stop_progress(self):
        self._animating = False
        if hasattr(self, 'progress_bar'):
            self.progress_bar.stop()
            self.progress_bar.set(0)

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
            msg = f"同步 {code} (基础信息优先读 cb_data)"
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
                raise ValueError("cb_data 未包含标的正股代码 — 请先用 Wind 刷新基础信息")

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

        detail_parts = [f"条款: {origin_tag}", f"行情: {market_label}"]
        source_parts = []
        if d.get("S0") is not None:
            source_parts.append(f"S={market_label}")
        if d.get("sigma") is not None:
            source_parts.append(f"σ={market_label}历史{d.get('vol_window') or self.v_vol_window.get()}")
        if d.get("shibor") is not None:
            source_parts.append(f"r={market_label}")
        elif self.v_src_r.get() == "手工":
            source_parts.append("r=手工")
        source_parts.append(f"利差={self.v_src_spread.get()}")
        source_parts.append("p/dk=模型")
        detail_parts.append("参数来源: " + " / ".join(source_parts))
        self.v_ref_detail.set("\n".join(detail_parts))

        src_tag = "付息计划" if coupon_src == "cashflow" else "条款字段"
        s0_text = f"S₀={d['S0']:.3f}" if d.get("S0") is not None else "S₀=N/A"
        self.v_status.set(
            f"已加载 {self.v_bond_code.get()} (正股 {d.get('stock_code', '?')}, {s0_text}, 票息: {src_tag})"
        )

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

    # ── 定价计算 ──────────────────────────────────────────
    def _run_pricing(self):
        code = self._normalize_bond_code(self.v_bond_code.get())
        if code:
            self._maybe_sync_events_background(code)
        self.v_result.set("…")
        self.lbl_result.configure(text_color=get_color(TEXT_DIM))
        self.btn_calc.configure(state="disabled")
        self._start_progress("正在计算理论价格")
        threading.Thread(target=self._pricing_worker, daemon=True).start()

    def _pricing_worker(self):
        try:
            params = self._collect_params()
            pricer = UniversalCBPricer(**params["pricer"])
            result = pricer.price(**params["model"], return_greeks=True)
            sigma_used = params["model"]["sigma"]
            self.after(0, lambda: self._show_result(result, pricer, sigma_used))
        except Exception as exc:
            err_msg = f"计算失败: {exc}"
            self.after(0, self._on_error, err_msg)
        finally:
            self.after(0, self._stop_progress)
            self.after(0, lambda: self.btn_calc.configure(state="normal"))

    def _collect_params(self):
        def pf(v):
            val = v.get().strip()
            try:
                return float(val)
            except ValueError:
                raise ValueError(f"请输入有效数字，当前值: '{val}'")
        def pd(v):
            val = v.get().strip()
            try:
                return date.fromisoformat(val)
            except ValueError:
                raise ValueError(f"日期格式应为 YYYY-MM-DD，当前值: '{val}'")

        coupon_str = self.v_coupons.get().strip()
        coupon_rates = tuple(float(x.strip()) / 100.0
                             for x in coupon_str.split(",") if x.strip())

        current_date = pd(self.v_cur_date)
        pricer = dict(
            S0=pf(self.v_S0),
            K=pf(self.v_K),
            face_value=pf(self.v_face),
            redemption_price=pf(self.v_redemp),
            current_date=current_date,
            maturity_date=pd(self.v_mat_date),
            issue_date=pd(self.v_iss_date),
            conversion_start_date=pd(self.v_conv_date),
            coupon_rates=coupon_rates,
            call_trigger_ratio=pf(self.v_call_ratio) / 100.0,
            put_trigger_ratio=pf(self.v_put_ratio) / 100.0,
            put_active_years=int(pf(self.v_put_years)),
            call_notice_days=int(pf(self.v_call_notice)),
        )

        block_until, p_scale = self._resolve_down_reset_for_pricing(current_date)
        if block_until is not None:
            pricer["down_reset_block_until"] = block_until

        p_down = pf(self.v_p_down) / 100.0
        if p_scale is not None:
            p_down *= max(0.0, p_scale)

        model = dict(
            sigma=pf(self.v_sigma) / 100.0,
            r=pf(self.v_r) / 100.0,
            base_spread=pf(self.v_spread) / 100.0,
            p_down=p_down,
            distress_k=pf(self.v_dk) / 100.0,
            M=int(pf(self.v_M)),
            N=int(pf(self.v_N)),
        )
        return {"pricer": pricer, "model": model}

    # ── 下修事件覆盖 ───────────────────────────────────────
    def _resolve_down_reset_for_pricing(self, valuation_date: date):
        """定价前直接从事件表/覆盖层解析下修冻结, UI 字段只作兜底维护入口."""
        code = self._normalize_bond_code(self.v_bond_code.get())
        terms = self.terms_cache.get(code) if code else None
        ui_block, ui_p_scale = self._compute_down_reset_from_ui(update_display=False)
        if terms is None:
            return ui_block, ui_p_scale

        resolved = resolve_down_reset(code, terms, valuation_date=valuation_date)
        block_until = resolved.block_until or ui_block
        p_scale = ui_p_scale if ui_p_scale is not None else resolved.p_scale
        return block_until, p_scale

    def _compute_down_reset_from_ui(self, *, update_display: bool = True):
        """读取下修事件 GUI 字段 → (block_until, p_scale).

        仅作为手工维护兜底. 常规定价优先走 cb_events / overrides 解析.
        有公告日时用 announce_date + cooldown 推算 block_until; 没有公告日时,
        允许直接使用 "推算屏蔽至" 中的硬 override 日期.
        """
        ann_str = self.v_dr_announce_date.get().strip()
        cd_str = self.v_dr_cooldown.get().strip()
        ps_str = self.v_dr_p_scale.get().strip()
        block_str = self.v_dr_block_until.get().strip()

        block_until = None
        if ann_str:
            try:
                ann = date.fromisoformat(ann_str)
            except ValueError:
                raise ValueError(f"公告不修正日期格式应为 YYYY-MM-DD: '{ann_str}'")
            try:
                cd = float(cd_str) if cd_str else float(DEFAULT_COOLDOWN_MONTHS)
            except ValueError:
                raise ValueError(f"再观察期(月)应为数字或留空: '{cd_str}'")
            block_until = _add_months(ann, int(round(cd)))
        elif block_str and block_str not in {"—", "-", "N/A"}:
            try:
                block_until = date.fromisoformat(block_str)
            except ValueError:
                raise ValueError(f"推算屏蔽至日期格式应为 YYYY-MM-DD: '{block_str}'")

        p_scale = None
        if ps_str:
            try:
                p_scale = float(ps_str)
            except ValueError:
                raise ValueError(f"p_scale 应为数字或留空: '{ps_str}'")

        if update_display:
            self.v_dr_block_until.set(block_until.isoformat() if block_until else "—")
        return block_until, p_scale

    def _populate_down_reset_from_resolver(self, code: str, terms: BondTerms) -> None:
        """根据 cb_events + cb_data.cooldown + overrides.json 填充 GUI 字段."""
        ov = default_overrides().get(code) or {}
        ann = ov.get("announce_date") or ""
        ps = ov.get("p_scale_after_cooldown")
        resolved = resolve_down_reset(code, terms, valuation_date=date.today())
        note_parts = []
        if ov.get("note"):
            note_parts.append(str(ov["note"]))
        if terms.down_reset_note:
            note_parts.append(terms.down_reset_note)
        note_text = " | ".join(note_parts) if note_parts else (resolved.note or "")

        cooldown = terms.down_reset_cooldown_months
        if cooldown is None:
            cooldown = resolved.cooldown_months
        cd_str = "" if cooldown is None else f"{float(cooldown):g}"

        self.v_dr_announce_date.set(str(ann or resolved.announce_date or ""))
        self.v_dr_cooldown.set(cd_str)
        self.v_dr_p_scale.set("" if ps is None else f"{float(ps):g}")
        self.v_dr_note.set(note_text)

        # 同步 block_until 显示
        self.v_dr_block_until.set(
            resolved.block_until.isoformat() if resolved.block_until else "—"
        )

        if ann:
            tag = f"事件: {ann}"
            if cooldown is None:
                tag += " (cooldown 用默认值)"
            self.v_dr_status.set(tag)
        elif resolved.announce_date is not None:
            self.v_dr_status.set(f"事件表: {resolved.announce_date}")
        elif terms.down_reset_block_until is not None:
            self.v_dr_status.set(f"硬 override: {terms.down_reset_block_until}")
        else:
            self.v_dr_status.set("无事件")

    def _save_down_reset_override(self):
        code = self._normalize_bond_code(self.v_bond_code.get())
        if not code:
            messagebox.showwarning("提示", "请先输入转债代码")
            return
        ann_str = self.v_dr_announce_date.get().strip()
        ps_str = self.v_dr_p_scale.get().strip()
        ann = None
        if ann_str:
            try:
                ann = date.fromisoformat(ann_str)
            except ValueError:
                messagebox.showwarning("提示", f"公告日格式应为 YYYY-MM-DD: {ann_str}")
                return
        ps = None
        if ps_str:
            try:
                ps = float(ps_str)
            except ValueError:
                messagebox.showwarning("提示", f"p_scale 应为数字: {ps_str}")
                return
        try:
            default_overrides().set(
                code, announce_date=ann, p_scale_after_cooldown=ps,
                note=self.v_dr_note.get().strip() or None,
            )
            reload_default_overrides()
            self.v_dr_status.set(f"已保存到 overrides.json ({code})")
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))

    def _clear_down_reset_override(self):
        code = self._normalize_bond_code(self.v_bond_code.get())
        if not code:
            return
        if default_overrides().delete(code):
            reload_default_overrides()
        self.v_dr_announce_date.set("")
        self.v_dr_p_scale.set("")
        self.v_dr_note.set("")
        self.v_dr_block_until.set("—")
        self.v_dr_status.set("已清除")

    def _save_down_reset_cooldown_to_cb_data(self):
        """把 cooldown 写回 cb_data.json 的 down_reset_cooldown_months 字段."""
        code = self._normalize_bond_code(self.v_bond_code.get())
        if not code:
            messagebox.showwarning("提示", "请先输入转债代码")
            return
        terms = self.terms_cache.get(code)
        if terms is None:
            messagebox.showwarning("提示", f"{code} 不在 cb_data, 先 '同步' 拉取")
            return
        cd_str = self.v_dr_cooldown.get().strip()
        try:
            cd_val = float(cd_str) if cd_str else None
        except ValueError:
            messagebox.showwarning("提示", f"cooldown 应为数字或留空: {cd_str}")
            return
        terms.down_reset_cooldown_months = cd_val
        self.terms_cache.set(code, terms, source="manual_gui")
        self.v_dr_status.set(f"已写回 cb_data.json (cooldown={cd_val})")

    # ── 公告事件面板 ─────────────────────────────────────────
    def _build_events_panel(self, parent):
        """构建公告事件面板: 同步按钮 + 事件列表 + 应用按钮."""
        card = create_card(parent, "事件时间线", 0, 0, icon="📋")

        # 操作栏
        toolbar = ctk.CTkFrame(card, fg_color="transparent")
        toolbar.grid(row=0, column=0, sticky="ew", padx=16, pady=(8, 4))

        self.btn_sync_events = ctk.CTkButton(
            toolbar, text="🔄 同步公告", command=self._sync_events_from_cninfo,
            fg_color=BTN_CTRL, hover_color=BTN_HOVER, text_color=ORANGE,
            font=(FONT_FAMILY, 12, "bold"), width=100, height=28, corner_radius=6)
        self.btn_sync_events.pack(side="left", padx=(0, 6))
        Tooltip(self.btn_sync_events, "从巨潮资讯网抓取当前债的公告, 解析事件")

        self.btn_apply_events = ctk.CTkButton(
            toolbar, text="写回 cb_data", command=self._apply_events_to_current,
            fg_color=BTN_CTRL, hover_color=BTN_HOVER, text_color=TEXT_DIM,
            font=(FONT_FAMILY, 12), width=100, height=28, corner_radius=6)
        self.btn_apply_events.pack(side="left", padx=(0, 6))
        Tooltip(self.btn_apply_events,
                "维护动作: 将事件表固化写回 cb_data\n"
                "日常定价会直接读取 cb_events, 不需要点这里")

        ctk.CTkLabel(toolbar, textvariable=self.v_event_summary,
                     text_color=TEXT_DIM, font=(FONT_FAMILY, 11)).pack(side="left", padx=(8, 0))

        # 事件列表容器 (可滚动)
        self._events_list_frame = ctk.CTkScrollableFrame(
            card, fg_color="transparent", height=150,
            scrollbar_button_color=BORDER)
        self._events_list_frame.grid(row=1, column=0, sticky="nsew", padx=12, pady=(4, 8))
        self._events_list_frame.grid_columnconfigure(0, weight=1)

    def _refresh_events_panel(self, bond_code: str):
        """刷新事件面板: 从 event_store 加载当前债的事件并显示."""
        # 清空旧 widget
        for widget in self._event_widgets:
            try:
                widget.destroy()
            except Exception:
                pass
        self._event_widgets.clear()

        if not bond_code:
            self.v_event_summary.set("请输入转债代码")
            return

        events = self.event_store.list_events(bond_code=bond_code)
        if not events:
            self.v_event_summary.set("无事件记录")
            lbl = ctk.CTkLabel(
                self._events_list_frame, text="暂无事件 — 点击「同步公告」从巨潮抓取",
                text_color=TEXT_DIM, font=(FONT_FAMILY, 11))
            lbl.grid(row=0, column=0, sticky="w", padx=4, pady=4)
            self._event_widgets.append(lbl)
            return

        self.v_event_summary.set(f"{len(events)} 条事件")
        # 按日期倒序显示 (最新在上)
        for i, ev in enumerate(reversed(events)):
            row_frame = ctk.CTkFrame(
                self._events_list_frame, fg_color=BG_INPUT, corner_radius=8)
            row_frame.grid(row=i, column=0, sticky="ew", padx=2, pady=2)
            row_frame.grid_columnconfigure(1, weight=1)
            self._event_widgets.append(row_frame)

            # 事件类型 badge
            type_color = self._event_type_color(ev.event_type)
            type_label = self._event_type_short(ev.event_type)
            badge = ctk.CTkLabel(
                row_frame, text=type_label, text_color="#ffffff",
                fg_color=type_color, corner_radius=4,
                font=(FONT_FAMILY, 10, "bold"), width=52, height=18)
            badge.grid(row=0, column=0, padx=(6, 4), pady=4, sticky="w")

            # 日期 + 标题
            date_str = ev.event_date.isoformat()
            title_short = ev.raw_title[:40] + ("…" if len(ev.raw_title) > 40 else "")
            info_text = f"{date_str}  {title_short}"
            if ev.commitment_months:
                info_text += f"  [承诺{ev.commitment_months}个月]"

            info_lbl = ctk.CTkLabel(
                row_frame, text=info_text, text_color=TEXT,
                font=(FONT_FAMILY, 11), anchor="w")
            info_lbl.grid(row=0, column=1, padx=(2, 6), pady=4, sticky="w")

            # 来源标签
            src_lbl = ctk.CTkLabel(
                row_frame, text=ev.source, text_color=TEXT_DIM,
                font=(FONT_FAMILY, 10))
            src_lbl.grid(row=0, column=2, padx=(2, 8), pady=4, sticky="e")

    @staticmethod
    def _event_type_color(event_type: str) -> str:
        return {
            "down_reset_proposed": "#e6a700",   # 黄
            "down_reset_approved": "#40a02b",   # 绿
            "down_reset_rejected": "#d20f39",   # 红
            "call_redemption":     "#d20f39",
            "call_no_redemption":  "#40a02b",
            "putback":             "#7287fd",
            "rating_change":       "#df8e1d",
            "delisting":           "#8839ef",
            "suspension":          "#fe640b",
        }.get(event_type, "#6c6f85")

    @staticmethod
    def _event_type_short(event_type: str) -> str:
        return {
            "down_reset_proposed": "提议下修",
            "down_reset_approved": "已下修",
            "down_reset_rejected": "不下修",
            "call_redemption":     "强赎",
            "call_no_redemption":  "不强赎",
            "putback":             "回售",
            "rating_change":       "评级",
            "delisting":           "摘牌",
            "suspension":          "停牌",
        }.get(event_type, event_type[:4])

    def _event_last_synced_at(self, code: str) -> datetime | None:
        meta = getattr(self.event_store, "_meta", {}) or {}
        by_code = meta.get("synced_at_by_code") or {}
        raw = by_code.get(code) if by_code else (meta.get("updated_at") or meta.get("last_sync_at"))
        if not raw:
            return None
        try:
            return datetime.fromisoformat(str(raw))
        except ValueError:
            return None

    def _events_are_stale(self, code: str) -> bool:
        synced_at = self._event_last_synced_at(code)
        if synced_at is None:
            return True
        return datetime.now() - synced_at > timedelta(hours=EVENT_SYNC_STALE_HOURS)

    def _maybe_sync_events_background(self, code: str) -> bool:
        """后台刷新公告事件. 本地事件先参与定价, 网络结果回来后再刷新界面."""
        code = self._normalize_bond_code(code)
        if not BOND_CODE_RE.match(code):
            return False
        if code in self._event_sync_in_flight or not self._events_are_stale(code):
            return False

        self._event_sync_in_flight.add(code)
        if self._normalize_bond_code(self.v_bond_code.get()) == code:
            self.v_event_summary.set("公告缓存后台刷新中...")
        threading.Thread(
            target=self._auto_sync_events_worker, args=(code,), daemon=True,
        ).start()
        return True

    def _auto_sync_events_worker(self, code: str):
        try:
            from ..cninfo_provider import CninfoAnnouncementProvider
            provider = CninfoAnnouncementProvider()
            store = CBEventStore(project_events_path())
            result = sync_cb_events(
                provider, [code], store,
                end=date.today(), lookback_days=365,
                download_pdf=True,
            )
            self.after(0, lambda: self._on_auto_sync_events_done(code, result, None))
        except Exception as exc:
            self.after(0, lambda: self._on_auto_sync_events_done(code, None, exc))

    def _reload_events_for_current_code(self, code: str) -> None:
        self.event_store = CBEventStore(project_events_path())
        reload_default_event_store()
        if self._normalize_bond_code(self.v_bond_code.get()) != code:
            return
        self._refresh_events_panel(code)
        terms = self.terms_cache.get(code)
        if terms is not None:
            self._populate_down_reset_from_resolver(code, terms)

    def _on_auto_sync_events_done(self, code: str, result: dict | None, exc: Exception | None):
        self._event_sync_in_flight.discard(code)
        self._reload_events_for_current_code(code)
        if self._normalize_bond_code(self.v_bond_code.get()) != code:
            return
        if exc is not None:
            self.v_event_summary.set(f"公告后台同步失败: {exc}")
            return

        scanned = result.get("scanned_announcements", 0) if result else 0
        added = result.get("added", 0) if result else 0
        pdf_ok = result.get("pdf_downloaded", 0) if result else 0
        pdf_fail = result.get("pdf_failed", 0) if result else 0
        msg = f"公告已自动刷新: 扫描 {scanned} 条, 新增 {added} 条"
        if pdf_ok or pdf_fail:
            msg += f" (PDF ✓{pdf_ok} ✗{pdf_fail})"
        self.v_event_summary.set(msg)
        self._maybe_reprice_after_event_refresh(code)

    def _maybe_reprice_after_event_refresh(self, code: str) -> None:
        if self._normalize_bond_code(self.v_bond_code.get()) != code:
            return
        result_text = self.v_result.get().strip()
        if result_text in {"", "—", "…"} or result_text.startswith("ERR"):
            return
        try:
            if self.btn_calc.cget("state") == "disabled":
                return
        except Exception:
            return
        self.v_status.set("公告事件已刷新, 自动重算理论价")
        self._run_pricing()

    def _sync_events_from_cninfo(self):
        """从巨潮抓取当前债的公告并解析为事件."""
        code = self._normalize_bond_code(self.v_bond_code.get())
        if not code:
            messagebox.showwarning("提示", "请先输入转债代码")
            return
        self.btn_sync_events.configure(state="disabled")
        self.v_event_summary.set(f"正在从巨潮同步 {code}...")
        threading.Thread(
            target=self._sync_events_worker, args=(code,), daemon=True,
        ).start()

    def _sync_events_worker(self, code: str):
        try:
            from ..cninfo_provider import CninfoAnnouncementProvider
            provider = CninfoAnnouncementProvider()
            result = sync_cb_events(
                provider, [code], self.event_store,
                end=date.today(), lookback_days=365,
                download_pdf=True,
            )
            scanned = result["scanned_announcements"]
            added = result["added"]
            pdf_ok = result.get("pdf_downloaded", 0)
            pdf_fail = result.get("pdf_failed", 0)
            msg = f"扫描 {scanned} 条, 新增 {added} 条"
            if pdf_ok or pdf_fail:
                msg += f" (PDF ✓{pdf_ok} ✗{pdf_fail})"
            self.after(0, lambda: self._on_sync_events_done(code, msg))
        except Exception as exc:
            logger.warning("事件同步失败 (%s): %s", code, exc)
            self.after(0, lambda: self._on_sync_events_done(
                code, f"同步失败: {exc}"))

    def _on_sync_events_done(self, code: str, msg: str):
        self.btn_sync_events.configure(state="normal")
        self.v_event_summary.set(msg)
        self._reload_events_for_current_code(code)
        self._maybe_reprice_after_event_refresh(code)

    def _apply_events_to_current(self):
        """维护动作: 将事件表中的事件固化写回 cb_data."""
        code = self._normalize_bond_code(self.v_bond_code.get())
        if not code:
            messagebox.showwarning("提示", "请先输入转债代码")
            return
        terms = self.terms_cache.get(code)
        if terms is None:
            messagebox.showwarning("提示", f"{code} 不在 cb_data, 先同步")
            return

        events = self.event_store.list_events(
            bond_code=code, through_date=date.today())
        if not events:
            self.v_event_summary.set("无可应用的事件")
            return

        patched = apply_events_to_terms(code, terms, events)

        # 更新 GUI 字段
        changes = []
        if patched.down_reset_block_until != terms.down_reset_block_until:
            block_str = (patched.down_reset_block_until.isoformat()
                         if patched.down_reset_block_until else "—")
            self.v_dr_block_until.set(block_str)
            changes.append("block_until")
        if patched.down_reset_note != terms.down_reset_note and patched.down_reset_note:
            self.v_dr_note.set(patched.down_reset_note)
            changes.append("note")
        if patched.call_status != terms.call_status and patched.call_status:
            changes.append(f"call={patched.call_status}")
        if patched.call_no_redemption_until != terms.call_no_redemption_until:
            changes.append(f"不强赎至={patched.call_no_redemption_until}")

        # 把更新写回 cb_data
        self.terms_cache.set(code, patched, source="cb_events")

        if changes:
            self.v_event_summary.set(f"已写回 cb_data: {', '.join(changes)}")
            self.v_dr_status.set(f"事件已写回 ({len(events)} 条)")
            # 重新填充下修面板
            self._populate_down_reset_from_resolver(code, patched)
        else:
            self.v_event_summary.set("事件无新变更")


    @staticmethod
    def _fmt_greek(val, fmt):
        if val is None:
            return "—"
        try:
            f = float(val)
        except (TypeError, ValueError):
            return "—"
        if f != f:  # NaN
            return "—"
        return format(f, fmt)

    def _show_result(self, result, pricer, sigma_used):
        theo = result["price"] if isinstance(result, dict) else result
        self.v_result.set(f"{theo:.3f}")
        info = (
            f"S₀={pricer.S0:.3f}  K={pricer.K:.2f}  "
            f"T={pricer.T:.4f}年  "
            f"σ={sigma_used*100:.1f}%  "
            f"转股比例={pricer.ratio:.4f}"
        )
        self.v_status.set(info)

        if isinstance(result, dict):
            self.v_bond_floor.set(self._fmt_greek(result.get("bond_floor"), ".3f"))
            self.v_parity.set(self._fmt_greek(result.get("parity"), ".3f"))
            self.v_option_prem.set(self._fmt_greek(result.get("option_premium"), ".3f"))
            self.v_delta.set(self._fmt_greek(result.get("delta"), ".4f"))
            self.v_gamma.set(self._fmt_greek(result.get("gamma"), ".6f"))
            self.v_vega.set(self._fmt_greek(result.get("vega"), ".4f"))
            self.v_theta.set(self._fmt_greek(result.get("theta"), ".4f"))

            # 深度实值 + 已过强赎线: 期权价值数学上为 0, 提示是模型预期而非 bug
            opt = result.get("option_premium") or 0.0
            if abs(opt) < 0.01 and pricer.S0 / pricer.K >= pricer.call_trigger_ratio:
                self.v_status.set(info + "  ·  深度实值 + 已过强赎线, 期权价值锁定为 0 (理论锚 = 转股价值)")

        if theo > 100:
            self.lbl_result.configure(text_color=GREEN)
        elif theo < 100:
            self.lbl_result.configure(text_color=RED)
        else:
            self.lbl_result.configure(text_color=TEXT)


    def _on_error(self, msg, show_dialog=True):
        self._stop_progress()
        self.v_status.set(f"❌ {msg}")
        if show_dialog:
            self.v_ref_info.set(f"❌ 获取失败")
            self.v_result.set("—")
            self.lbl_result.configure(text_color=RED)
            messagebox.showerror("错误", str(msg))

    # ── 历史回测 ────────────────────────────────────────────
    def _run_backtest(self):
        code = self.v_bond_code.get().strip()
        if not code:
            messagebox.showwarning("提示", "请先输入转债代码")
            return
        try:
            start = date.fromisoformat(self.v_bt_start.get().strip())
            end = date.fromisoformat(self.v_bt_end.get().strip())
        except ValueError:
            messagebox.showerror("错误", "日期格式应为 YYYY-MM-DD")
            return
        if start >= end:
            messagebox.showerror("错误", "开始日期应早于结束日期")
            return

        freq_map = {"日": "D", "周": "W", "月": "M"}
        freq = freq_map.get(self.v_bt_freq.get(), "W")

        try:
            params = dict(
                r=float(self.v_r.get()) / 100.0,
                base_spread=float(self.v_spread.get()) / 100.0,
                p_down=float(self.v_p_down.get()) / 100.0,
                distress_k=float(self.v_dk.get()) / 100.0,
                M=int(float(self.v_M.get())),
                N=int(float(self.v_N.get())),
                vol_window_days=VOL_WINDOW_MAP.get(self.v_vol_window.get(), 21),
                solve_iv=bool(self.v_bt_solve_iv.get()),
                call_notice_days=int(float(self.v_call_notice.get())),
            )
        except ValueError as e:
            messagebox.showerror("错误", f"参数解析失败: {e}")
            return

        self.btn_backtest.configure(state="disabled")
        self.v_bt_status.set(f"正在回测 {code} {start} → {end} ({self.v_bt_freq.get()}频) ...")
        threading.Thread(
            target=self._backtest_worker,
            args=(code, start, end, freq, params),
            daemon=True,
        ).start()

    def _backtest_worker(self, code, start, end, freq, params):
        try:
            provider = self._get_provider()

            def progress(i, total):
                self.after(0, lambda: self.v_bt_status.set(
                    f"进度 {i}/{total} ..."
                ))

            result = backtest_theoretical_price(
                code, start_date=start, end_date=end, freq=freq,
                provider=provider, progress_cb=progress, **params,
            )
            self._last_bt_result = result
            self.after(0, self._render_backtest_chart, result)
        except Exception as exc:
            self.after(0, lambda: self.v_bt_status.set(f"❌ 回测失败: {exc}"))
            self.after(0, lambda: messagebox.showerror("回测失败", str(exc)))
        finally:
            self.after(0, lambda: self.btn_backtest.configure(state="normal"))

    def _refresh_backtest_chart(self):
        """切换"价值分解"复选框时无需重新拉数据, 用缓存重绘."""
        if self._last_bt_result is not None:
            self._render_backtest_chart(self._last_bt_result)

    def _render_backtest_chart(self, result):
        dates = result["dates"]
        theo = result["theo_prices"]
        mkt = result["market_prices"]
        bond_floors = result.get("bond_floors", [])
        parities = result.get("parities", [])
        sigmas = result.get("sigmas", [])
        ivs = result.get("ivs", [])

        if not dates:
            self.v_bt_status.set("❌ 无有效采样点")
            return

        # 释放旧图表资源，防止内存泄漏
        if self._bt_figure is not None:
            self._bt_figure.clf()
            plt.close(self._bt_figure)
            self._bt_figure = None
            self._bt_canvas = None

        for child in self.bt_chart_frame.winfo_children():
            child.destroy()

        # 根据当前深浅色模式获取真实 HEX
        bg_card_color = get_color(BG_CARD)
        bg_input_color = get_color(BG_INPUT)
        text_dim_color = get_color(TEXT_DIM)
        text_color = get_color(TEXT)
        border_color = get_color(BORDER)
        accent_color = get_color(ACCENT)
        orange_color = get_color(ORANGE)
        red_color = get_color(RED)
        green_color = get_color(GREEN)

        iv_arr = np.array([v if v is not None else np.nan for v in ivs], dtype=float) \
                 if len(ivs) else np.array([])
        has_iv = iv_arr.size > 0 and bool(np.any(np.isfinite(iv_arr)))
        show_decomp = bool(self.v_bt_show_decomp.get()) and bond_floors and parities

        if has_iv:
            fig = Figure(figsize=(11, 6), dpi=100, facecolor=bg_card_color)
            ax = fig.add_subplot(2, 1, 1, facecolor=bg_input_color)
            ax_iv = fig.add_subplot(2, 1, 2, facecolor=bg_input_color, sharex=ax)
        else:
            fig = Figure(figsize=(11, 5), dpi=100, facecolor=bg_card_color)
            ax = fig.add_subplot(111, facecolor=bg_input_color)
            ax_iv = None

        ax.plot(dates, theo, color=accent_color, linewidth=2.0, marker="o", markersize=4,
                label="理论价", zorder=3)
        ax.plot(dates, mkt, color=orange_color, linewidth=2.0, marker="s", markersize=4,
                label="市价(收盘)", zorder=2)

        if show_decomp:
            ax.plot(dates, bond_floors, color=text_dim_color, linewidth=1.2,
                    linestyle="--", alpha=0.7, label="纯债价值", zorder=1)
            ax.plot(dates, parities, color=green_color, linewidth=1.2,
                    linestyle=":", alpha=0.7, label="转股价值", zorder=1)

        theo_arr = np.array(theo)
        mkt_arr = np.array(mkt)
        ax.fill_between(dates, theo_arr, mkt_arr,
                        where=(mkt_arr >= theo_arr).tolist(), color=red_color, alpha=0.12, label="市价溢价")
        ax.fill_between(dates, theo_arr, mkt_arr,
                        where=(mkt_arr < theo_arr).tolist(), color=green_color, alpha=0.12, label="市价折价")

        ax.set_ylabel("价格", color=text_dim_color, fontsize=10)
        ax.tick_params(colors=text_dim_color, labelsize=9)
        for spine in ax.spines.values():
            spine.set_color(border_color)
        ax.grid(True, color=border_color, linestyle="--", alpha=0.4)

        legend = ax.legend(loc="best", framealpha=0.9, facecolor=bg_card_color,
                           edgecolor=border_color, fontsize=9, labelcolor=text_color)
        legend.get_frame().set_linewidth(0.5)

        if ax_iv is not None:
            hv_pct = np.array(sigmas) * 100
            iv_pct = iv_arr * 100
            ax_iv.plot(dates, hv_pct, color=text_dim_color, linewidth=1.5,
                       marker="o", markersize=3, label="历史波动率 HV", zorder=2)
            ax_iv.plot(dates, iv_pct, color=accent_color, linewidth=2.0,
                       marker="s", markersize=4, label="隐含波动率 IV", zorder=3)
            valid = np.isfinite(iv_pct) & np.isfinite(hv_pct)
            if np.any(valid):
                d_v = np.array(dates)[valid]
                hv_v = hv_pct[valid]
                iv_v = iv_pct[valid]
                where_high = [bool(x) for x in (iv_v >= hv_v)]
                where_low = [bool(x) for x in (iv_v < hv_v)]
                ax_iv.fill_between(d_v, hv_v, iv_v, where=where_high,
                                   color=red_color, alpha=0.12)
                ax_iv.fill_between(d_v, hv_v, iv_v, where=where_low,
                                   color=green_color, alpha=0.12)
            ax_iv.set_xlabel("日期", color=text_dim_color, fontsize=10)
            ax_iv.set_ylabel("σ (%)", color=text_dim_color, fontsize=10)
            ax_iv.tick_params(colors=text_dim_color, labelsize=9)
            for spine in ax_iv.spines.values():
                spine.set_color(border_color)
            ax_iv.grid(True, color=border_color, linestyle="--", alpha=0.4)
            leg_iv = ax_iv.legend(loc="best", framealpha=0.9, facecolor=bg_card_color,
                                  edgecolor=border_color, fontsize=9, labelcolor=text_color)
            leg_iv.get_frame().set_linewidth(0.5)
        else:
            ax.set_xlabel("日期", color=text_dim_color, fontsize=10)

        fig.autofmt_xdate(rotation=25)
        fig.tight_layout()

        canvas = FigureCanvasTkAgg(fig, master=self.bt_chart_frame)
        canvas.draw()
        canvas.get_tk_widget().grid(row=0, column=0, sticky="nsew")

        self._bt_figure = fig
        self._bt_canvas = canvas

        mean_basis = float(np.mean(mkt_arr - theo_arr))
        corr = float(np.corrcoef(theo_arr, mkt_arr)[0, 1]) if len(theo) > 1 else float("nan")
        status_parts = [
            f"✅ {len(dates)} 个采样点",
            f"平均基差(市价-理论)={mean_basis:+.2f}",
            f"相关系数={corr:.3f}",
        ]
        if has_iv:
            iv_valid = iv_arr[np.isfinite(iv_arr)]
            hv_arr = np.array(sigmas)
            hv_for_iv = hv_arr[np.isfinite(iv_arr)]
            if iv_valid.size:
                mean_iv_hv = float(np.mean(iv_valid - hv_for_iv)) * 100
                status_parts.append(f"IV-HV 均值={mean_iv_hv:+.2f}pp")
        self.v_bt_status.set("  ·  ".join(status_parts))
        self.btn_bt_png.configure(state="normal")
        self.btn_bt_csv.configure(state="normal")

    # ── 隐含波动率反解 ──────────────────────────────────────
    def _solve_iv(self):
        try:
            target = float(self.v_market_price.get().strip())
        except ValueError:
            messagebox.showwarning("提示", "请在「市价 ¥」处填入有效数字 (如 110.5)")
            return
        if target <= 0:
            messagebox.showwarning("提示", "市价必须为正数")
            return
        self.btn_iv.configure(state="disabled")
        self._start_progress(f"反解 IV (target={target:.2f})")
        threading.Thread(target=self._solve_iv_worker, args=(target,), daemon=True).start()

    def _solve_iv_worker(self, target):
        try:
            params = self._collect_params()
            pricer = UniversalCBPricer(**params["pricer"])
            m = params["model"]
            iv = pricer.solve_implied_vol(
                target_price=target, r=m["r"], base_spread=m["base_spread"],
                p_down=m["p_down"], distress_k=m["distress_k"],
                M=max(150, m["M"] // 3), N=max(500, m["N"] // 3),
            )
            if iv != iv:  # NaN
                self.after(0, lambda: self.v_iv.set("—"))
                self.after(0, lambda: self.v_status.set(
                    f"❌ 反解失败: 市价 {target:.2f} 在 σ ∈ [5%, 200%] 区间内无解"))
            else:
                self.after(0, lambda: self.v_iv.set(f"{iv*100:.2f}%"))
                hist = float(self.v_sigma.get())
                gap = iv * 100 - hist
                self.after(0, lambda: self.v_status.set(
                    f"反解 IV = {iv*100:.2f}% (匹配市价 {target:.2f}); "
                    f"历史 σ = {hist:.2f}%, 差 {gap:+.2f}pp"))
        except Exception as exc:
            self.after(0, self._on_error, f"反解 IV 失败: {exc}")
        finally:
            self.after(0, self._stop_progress)
            self.after(0, lambda: self.btn_iv.configure(state="normal"))

    # ── 现金流可视化 ────────────────────────────────────────
    def _show_cashflow(self):
        try:
            params = self._collect_params()
        except Exception as exc:
            messagebox.showerror("参数错误", str(exc))
            return
        try:
            pricer = UniversalCBPricer(**params["pricer"])
        except Exception as exc:
            messagebox.showerror("构造失败", str(exc))
            return

        # 现金流序列: 非末期 → 每期票息; 末期 → redemption_price (含末期利息+面值+赎回溢价)
        labels, amounts, kinds = [], [], []
        for p in pricer.coupon_periods:
            if p["is_final"]:
                labels.append(p["end"].isoformat())
                amounts.append(float(pricer.redemption_price))
                kinds.append("到期兑付")
            else:
                labels.append(p["end"].isoformat())
                amounts.append(float(p["coupon_amount"]))
                kinds.append(f"票息 {p['rate']*100:.2f}%")

        if not labels:
            messagebox.showinfo("提示", "没有可显示的现金流")
            return

        win = ctk.CTkToplevel(self)
        win.title(f"现金流: {self.v_bond_code.get() or '未命名'}")
        win.geometry("900x500")
        win.configure(fg_color=BG_APP)
        win.transient(self)

        bg = get_color(BG_CARD)
        bg_in = get_color(BG_INPUT)
        txt = get_color(TEXT)
        txt_dim = get_color(TEXT_DIM)
        brd = get_color(BORDER)
        accent = get_color(ACCENT)
        orange = get_color(ORANGE)

        fig = Figure(figsize=(9, 4.5), dpi=100, facecolor=bg)
        ax = fig.add_subplot(111, facecolor=bg_in)

        x_pos = np.arange(len(labels))
        colors = [orange if k == "到期兑付" else accent for k in kinds]
        bars = ax.bar(x_pos, amounts, color=colors, edgecolor=brd, linewidth=0.5)

        for bar, amt in zip(bars, amounts):
            ax.annotate(f"{amt:.2f}",
                        xy=(bar.get_x() + bar.get_width() / 2, bar.get_height()),
                        xytext=(0, 3), textcoords="offset points",
                        ha="center", fontsize=9, color=txt)

        ax.set_xticks(x_pos)
        ax.set_xticklabels(labels, rotation=30, ha="right", fontsize=9)
        ax.set_ylabel("现金流 (¥/百元面值)", color=txt_dim, fontsize=10)
        ax.set_title(f"{self.v_bond_code.get() or '可转债'} 现金流计划",
                     color=txt, fontsize=13, fontweight="bold")
        ax.tick_params(colors=txt_dim, labelsize=9)
        for spine in ax.spines.values():
            spine.set_color(brd)
        ax.grid(True, axis="y", color=brd, linestyle="--", alpha=0.4)

        from matplotlib.patches import Patch
        legend = ax.legend(handles=[
            Patch(facecolor=accent, label="期间票息"),
            Patch(facecolor=orange, label="到期兑付 (面值+末期利息+溢价)"),
        ], loc="best", framealpha=0.9, facecolor=bg, edgecolor=brd,
            fontsize=9, labelcolor=txt)
        legend.get_frame().set_linewidth(0.5)

        fig.tight_layout()
        canvas = FigureCanvasTkAgg(fig, master=win)
        canvas.draw()
        canvas.get_tk_widget().pack(fill="both", expand=True, padx=12, pady=12)

        total = sum(amounts)
        ctk.CTkLabel(
            win, text=f"现金流合计 (未折现) = {total:.2f}  ·  共 {len(labels)} 笔",
            text_color=TEXT_DIM, font=(FONT_FAMILY, 11)).pack(pady=(0, 10))

    # ── 收敛诊断 ────────────────────────────────────────────
    def _convergence_check(self):
        self.btn_conv.configure(state="disabled")
        self._start_progress("收敛诊断 (M, N → 2M, 2N)")
        threading.Thread(target=self._convergence_worker, daemon=True).start()

    def _convergence_worker(self):
        try:
            params = self._collect_params()
            pricer = UniversalCBPricer(**params["pricer"])
            m = params["model"]
            theo_a = float(pricer.price(**m))
            m2 = dict(m)
            m2["M"] = m["M"] * 2
            m2["N"] = m["N"] * 2
            theo_b = float(pricer.price(**m2))
            diff = theo_b - theo_a
            rel = abs(diff) / max(abs(theo_b), 1e-9)
            verdict = "已收敛" if rel < 1e-3 else ("基本收敛" if rel < 5e-3 else "未收敛, 建议加密")
            self.after(0, lambda: self.v_status.set(
                f"收敛诊断: M={m['M']},N={m['N']} → {theo_a:.4f}; 翻倍 → {theo_b:.4f}; "
                f"Δ={diff:+.4f} ({rel*100:.3f}%)  [{verdict}]"))
        except Exception as exc:
            self.after(0, self._on_error, f"收敛诊断失败: {exc}")
        finally:
            self.after(0, self._stop_progress)
            self.after(0, lambda: self.btn_conv.configure(state="normal"))

    # ── 参数预设保存/加载 ──────────────────────────────────
    _PRESET_VARS = (
        "v_bond_code", "v_S0", "v_K", "v_face", "v_redemp",
        "v_cur_date", "v_mat_date", "v_iss_date", "v_conv_date",
        "v_coupons", "v_sigma", "v_r", "v_spread", "v_p_down", "v_dk",
        "v_call_ratio", "v_put_ratio", "v_put_years", "v_call_notice", "v_M", "v_N",
        "v_vol_window", "v_market_price",
        "v_bt_start", "v_bt_end", "v_bt_freq",
        "v_data_source",
    )

    def _save_preset(self):
        path = filedialog.asksaveasfilename(
            title="保存参数预设",
            defaultextension=".json",
            filetypes=[("JSON", "*.json"), ("所有文件", "*.*")],
            initialfile=(self.v_bond_code.get().strip() or "cb_preset") + ".json",
        )
        if not path:
            return
        try:
            data = {name: getattr(self, name).get() for name in self._PRESET_VARS}
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            self.v_status.set(f"已保存预设到 {path}")
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))

    def _load_preset(self):
        path = filedialog.askopenfilename(
            title="加载参数预设",
            filetypes=[("JSON", "*.json"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._programmatic_update = True
            self._suppress_bond_autoload = True
            try:
                for name in self._PRESET_VARS:
                    if name in data:
                        getattr(self, name).set(data[name])
            finally:
                self._suppress_bond_autoload = False
                self._programmatic_update = False
            for src in (
                self.v_src_S0, self.v_src_K, self.v_src_face, self.v_src_redemp,
                self.v_src_cur_date, self.v_src_mat_date, self.v_src_iss_date,
                self.v_src_conv_date, self.v_src_coupons, self.v_src_sigma,
                self.v_src_r, self.v_src_spread, self.v_src_p_down, self.v_src_dk,
                self.v_src_call_ratio, self.v_src_put_ratio, self.v_src_put_years,
                self.v_src_call_notice,
            ):
                src.set("预设")
            self.v_status.set(f"已加载预设 {path}")
        except Exception as exc:
            messagebox.showerror("加载失败", str(exc))

    # ── 回测结果导出 ──────────────────────────────────────
    def _export_bt_png(self):
        if self._bt_figure is None:
            messagebox.showinfo("提示", "请先运行回测")
            return
        path = filedialog.asksaveasfilename(
            title="导出回测图",
            defaultextension=".png",
            filetypes=[("PNG", "*.png"), ("PDF", "*.pdf"), ("SVG", "*.svg")],
            initialfile=(self.v_bond_code.get().strip() or "backtest") + ".png",
        )
        if not path:
            return
        try:
            self._bt_figure.savefig(path, dpi=150, bbox_inches="tight",
                                    facecolor=self._bt_figure.get_facecolor())
            self.v_bt_status.set(f"已导出图表到 {path}")
        except Exception as exc:
            messagebox.showerror("导出失败", str(exc))

    def _export_bt_csv(self):
        if not self._last_bt_result or not self._last_bt_result.get("dates"):
            messagebox.showinfo("提示", "请先运行回测")
            return
        path = filedialog.asksaveasfilename(
            title="导出回测序列",
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv"), ("所有文件", "*.*")],
            initialfile=(self.v_bond_code.get().strip() or "backtest") + ".csv",
        )
        if not path:
            return
        try:
            r = self._last_bt_result
            n = len(r["dates"])
            bf = r.get("bond_floors") or [float("nan")] * n
            par = r.get("parities") or [float("nan")] * n
            iv = r.get("ivs") or [float("nan")] * n
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                w.writerow(["date", "theoretical_price", "market_price", "stock_price",
                            "sigma", "bond_floor", "parity", "implied_vol"])
                for d, t, m, s, sg, b, p, ivv in zip(
                        r["dates"], r["theo_prices"], r["market_prices"],
                        r["stock_prices"], r["sigmas"], bf, par, iv):
                    w.writerow([d.isoformat(), f"{t:.4f}", f"{m:.4f}", f"{s:.4f}",
                                f"{sg:.6f}", f"{b:.4f}", f"{p:.4f}", f"{ivv:.6f}"])
            self.v_bt_status.set(f"已导出 {len(r['dates'])} 条记录到 {path}")
        except Exception as exc:
            messagebox.showerror("导出失败", str(exc))


# ── 启动 ──────────────────────────────────────────────────
def main():
    app = CBPricerApp()
    app.mainloop()

if __name__ == "__main__":
    main()

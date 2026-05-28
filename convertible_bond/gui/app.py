#!/usr/bin/env python3
"""
可转债理论定价 GUI — 主应用模块.

本文件是 ``CBPricerApp`` 的"壳层": 状态变量、UI 构建、生命周期、主题切换、
preset I/O 等基础设施留在这里; 各业务域 (定价/回测/敏感性/下修/事件/同步)
拆到 ``controllers/`` 包内的 mixin, 通过多继承组装。
"""

import json
import logging
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk
import matplotlib

logger = logging.getLogger(__name__)

matplotlib.use("TkAgg")

# 解决 Matplotlib 中文显示和负号问题
matplotlib.rcParams['font.sans-serif'] = ['PingFang SC', 'Heiti TC', 'Arial Unicode MS', 'SimHei', 'Microsoft YaHei', 'sans-serif']
matplotlib.rcParams['axes.unicode_minus'] = False

from ..cache import TermsBundle, project_bundle_path
from ..cb_events import CBEventStore, project_events_path
from ..batch_pricing import DEFAULT_MIN_CREDIT_RATING, DEFAULT_MIN_OUTSTANDING_BALANCE
from ..paths import asset_path, seed_data_files
from .constants import (
    BOND_CODE_RE,
    DEFAULT_DOWN_RESET_TRIGGER_PCT,
    DEFAULT_DISTRESS_K_PCT,
    DEFAULT_P_DOWN_PCT,
    EVENT_SYNC_STALE_HOURS,    # noqa: F401  (re-export for legacy callers)
    normalize_strategy_history_mode,
)
from .controllers import (
    BacktestMixin,
    DownResetMixin,
    EventsMixin,
    PricingMixin,
    SensitivityMixin,
    WindSyncMixin,
)
from .tabs import backtest as backtest_tab
from .tabs import batch as batch_tab
from .tabs import pricing as pricing_tab
from .tabs import sensitivity as sensitivity_tab
from .tabs import strategy as strategy_tab
from .theme import (
    ACCENT, ACCENT_HOVER,
    BG_APP, BG_CARD, BG_INPUT, BORDER,
    BTN_HOVER,
    FONT_FAMILY, FONT_MONO,
    RED,
    TEXT, TEXT_DIM,
    VOL_WINDOW_DEFAULT,
    get_color,
    E,
)
from .widgets import AutocompleteEntry, Tooltip

ctk.set_default_color_theme("blue")


class CBPricerApp(
    ctk.CTk,
    PricingMixin,
    BacktestMixin,
    SensitivityMixin,
    DownResetMixin,
    EventsMixin,
    WindSyncMixin,
):
    """主应用窗口 — 状态/壳层留这里, 业务方法分散到各 mixin."""

    _RESPONSIVE_PROFILES = {
        "compact": {
            "pricing_left": 420,
        },
        "normal": {
            "pricing_left": 470,
        },
        "wide": {
            "pricing_left": 540,
        },
        "xl": {
            "pricing_left": 600,
        },
    }

    def __init__(self):
        # Windows: 在 Tk 创建前开 per-monitor DPI awareness, 避免 4K 屏上字体糊
        # / 控件被系统自动放大 (默认 system DPI 模式会让 Tk 控件看起来失真)
        self._enable_windows_dpi_awareness()
        super().__init__()
        ctk.set_appearance_mode("System")  # 跟随系统主题; 用户通过开关可手动覆盖

        self.title("CBLens — 可转债定价工作台")
        self._app_icon_image = None
        self._set_app_icon()
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        target_w = max(1280, int(screen_w * 0.76))
        initial_w = min(1320, target_w, max(1120, screen_w - 80))
        initial_h = min(920, max(760, int(screen_h * 0.86)))
        self.geometry(f"{initial_w}x{initial_h}")
        self.minsize(1120, 740)
        self.configure(fg_color=BG_APP)

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._build_vars()
        self._animating = False
        self._responsive_profile_name = None
        self._responsive_after_id = None
        self._responsive_last_size = None
        self._pricing_sash_left = None
        self._active_tab_name = None
        self._build_ui()
        self.bind("<Configure>", self._on_root_configure, add="+")
        self.after_idle(self._apply_responsive_layout)

    # ── 应用图标 / DPI ──────────────────────────────────────
    @staticmethod
    def _asset_path(name: str) -> Path:
        return asset_path(name)

    @staticmethod
    def _enable_windows_dpi_awareness() -> None:
        """Windows: per-monitor DPI v2, 让 Tk 控件在 4K / HiDPI 上不被 OS 拉伸放大."""
        if sys.platform != "win32":
            return
        try:
            import ctypes
            # PROCESS_PER_MONITOR_DPI_AWARE_V2 = -4 (Win 10 1703+); 失败时退回 v1 (=2)
            try:
                ctypes.windll.user32.SetProcessDpiAwarenessContext(-4)
            except (AttributeError, OSError):
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception as exc:
            logger.debug("设置 Windows DPI awareness 失败 (可能是旧版系统): %s", exc)

    def _set_app_icon(self) -> None:
        """设置 Tk 窗口图标; Windows 走 .ico + AppUserModelID, macOS 走 AppKit."""
        # Windows: 用 .ico (Tk 在 Win 下用 iconbitmap 比 iconphoto 稳, 任务栏分组也对)
        if sys.platform == "win32":
            self._set_windows_app_icon()
            return

        icon_path = self._asset_path("cblens-icon.png")
        if not icon_path.exists():
            logger.warning("应用图标不存在: %s", icon_path)
            return
        try:
            # Pillow → ImageTk: 比 Tk 原生 PhotoImage 更稳, 且能正确处理 RGBA 透明
            # (Tk 8.6 之前对带 alpha 的 PNG 支持不完善)
            from PIL import Image, ImageTk

            image = Image.open(icon_path).convert("RGBA")
            self._app_icon_image = ImageTk.PhotoImage(image)
            self.iconphoto(True, self._app_icon_image)
        except Exception as exc:
            logger.warning("设置窗口图标失败: %s", exc)

        if sys.platform == "darwin":
            self._set_macos_dock_icon(icon_path)

    def _set_windows_app_icon(self) -> None:
        """Windows: iconbitmap + iconphoto + AppUserModelID, 让任务栏显示高清图标."""
        ico_path = self._asset_path("cblens-icon.ico")
        png_path = self._asset_path("cblens-icon-win.png")
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("CBLens.Pricer")
        except Exception as exc:
            logger.debug("AppUserModelID 设置失败: %s", exc)
        # iconbitmap: 设置 .ico 给标题栏等小尺寸使用
        if ico_path.exists():
            try:
                self.iconbitmap(default=str(ico_path))
            except Exception as exc:
                logger.warning("iconbitmap 设置失败: %s", exc)
        # iconphoto: 设置大尺寸 PNG 给任务栏高 DPI 显示，解决图标模糊问题
        icon_png = png_path if png_path.exists() else self._asset_path("cblens-icon.png")
        if icon_png.exists():
            try:
                from PIL import Image, ImageTk
                image = Image.open(icon_png).convert("RGBA")
                self._app_icon_image = ImageTk.PhotoImage(image)
                self.iconphoto(True, self._app_icon_image)
            except Exception as exc:
                logger.warning("iconphoto 失败: %s", exc)

    @staticmethod
    def _set_macos_dock_icon(icon_path: Path) -> None:
        """在安装了 PyObjC 的 macOS 环境中同步 Dock 图标."""
        try:
            from AppKit import NSApplication, NSImage  # type: ignore

            ns_image = NSImage.alloc().initWithContentsOfFile_(str(icon_path))
            if ns_image is not None:
                NSApplication.sharedApplication().setApplicationIconImage_(ns_image)
        except Exception as exc:
            logger.debug("跳过 macOS Dock 图标同步: %s", exc)

    # ── 变量 ──────────────────────────────────────────────
    def _build_vars(self):
        # 确保打包进 PyInstaller 的 data/*.json 种子文件已被复制到可写数据目录
        seed_data_files()

        # 单债字段保持空白, 避免虚构示例债; 通用条款比例保留模型默认值。
        self.v_bond_code = ctk.StringVar()
        self.v_S0        = ctk.StringVar(value="")
        self.v_K         = ctk.StringVar(value="")
        self.v_face      = ctk.StringVar(value="100")
        self.v_redemp    = ctk.StringVar(value="")
        self.v_cur_date  = ctk.StringVar(value=date.today().isoformat())
        self.v_mat_date  = ctk.StringVar(value="")
        self.v_iss_date  = ctk.StringVar(value="")
        self.v_conv_date = ctk.StringVar(value="")
        self.v_coupons   = ctk.StringVar(value="")
        self.v_sigma     = ctk.StringVar(value="28")
        self.v_r         = ctk.StringVar(value="2.2")
        self.v_q         = ctk.StringVar(value="0")
        self.v_spread    = ctk.StringVar(value="3.0")
        self.v_p_down    = ctk.StringVar(value=f"{DEFAULT_P_DOWN_PCT:g}")
        # 实时换算: 用户输入的是年化强度 λ, 这里展示对应的 P(1年内至少 1 次) = 1 - exp(-λ)
        self.v_p_down_hint = ctk.StringVar(value="")
        self.v_dk        = ctk.StringVar(value=f"{DEFAULT_DISTRESS_K_PCT:g}")
        # 下修事件覆盖 (per-bond) — 默认由 cb_events 自动解析; 面板仅作维护/确认
        self.v_dr_announce_date = ctk.StringVar(value="")
        self.v_dr_cooldown      = ctk.StringVar(value="")
        self.v_dr_block_until   = ctk.StringVar(value="—")
        self.v_dr_note          = ctk.StringVar(value="")
        self.v_dr_status        = ctk.StringVar(value="无事件")
        self.v_call_ratio  = ctk.StringVar(value="130")
        self.v_down_reset_trigger_ratio = ctk.StringVar(value=f"{DEFAULT_DOWN_RESET_TRIGGER_PCT:g}")
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

        self.v_bt_mode = ctk.StringVar(value="单债回测")
        self.v_st_start = ctk.StringVar(value=(today - timedelta(days=365)).isoformat())
        self.v_st_end = ctk.StringVar(value=today.isoformat())
        self.v_st_freq = ctk.StringVar(value="月")
        self.v_st_top_n = ctk.StringVar(value="10")
        self.v_st_template = ctk.StringVar(value="自定义")
        self.v_st_view = ctk.StringVar(value="低估候选")
        self.v_st_pool_mode = ctk.StringVar(value="本地全市场")
        self.v_st_history_mode = ctk.StringVar(value="标准")
        self.v_st_min_price = ctk.StringVar(value="")
        self.v_st_max_price = ctk.StringVar(value="")
        self.v_st_min_premium = ctk.StringVar(value="")
        self.v_st_max_premium = ctk.StringVar(value="")
        self.v_st_min_deviation = ctk.StringVar(value="")
        self.v_st_max_deviation = ctk.StringVar(value="")
        self.v_st_min_sigma = ctk.StringVar(value="")
        self.v_st_max_sigma = ctk.StringVar(value="")
        self.v_st_min_balance = ctk.StringVar(
            value="" if DEFAULT_MIN_OUTSTANDING_BALANCE is None else str(DEFAULT_MIN_OUTSTANDING_BALANCE)
        )
        self.v_st_min_rating = ctk.StringVar(value=DEFAULT_MIN_CREDIT_RATING or "")
        self.v_st_min_turnover = ctk.StringVar(value="")
        self.v_st_delist_window = ctk.StringVar(value="0")
        self.v_st_cost = ctk.StringVar(value="20")
        self.v_st_benchmark = ctk.BooleanVar(value=True)
        self.v_st_codes = ctk.StringVar(value="")
        self.v_st_status = ctk.StringVar(value="就绪 · 调整参数后点击「运行策略」")
        self.v_st_precheck = ctk.StringVar(value="预检: 尚未运行")

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
        self.v_deviation    = ctk.StringVar(value="—")

        # 单债条款事件 (定价页只读展示)
        self.v_term_event_alert = ctk.StringVar(value="")
        self.v_term_event_down_status = ctk.StringVar(value="下修估值 —")
        self.v_term_event_down_detail = ctk.StringVar(value="—")
        self.v_term_event_down_progress = ctk.StringVar(value="—")
        self.v_term_event_call_status = ctk.StringVar(value="强赎 —")
        self.v_term_event_call_detail = ctk.StringVar(value="—")
        self.v_term_event_call_progress = ctk.StringVar(value="—")
        self.v_term_event_put_status = ctk.StringVar(value="回售 —")
        self.v_term_event_put_detail = ctk.StringVar(value="—")
        self.v_term_event_put_progress = ctk.StringVar(value="—")
        self.v_term_event_conv_status = ctk.StringVar(value="转股价 —")
        self.v_term_event_conv_detail = ctk.StringVar(value="—")
        self.v_term_event_conv_progress = ctk.StringVar(value="—")
        self.v_term_event_risk_status = ctk.StringVar(value="风险 —")
        self.v_term_event_risk_detail = ctk.StringVar(value="—")
        self.v_term_event_risk_progress = ctk.StringVar(value="—")
        self._term_event_widgets: dict = {}

        self._current_projected_terms = None
        self._current_terms_projection = None
        self._last_pricing_impact = None

        self._sens_figure     = None
        self._sens_canvas     = None
        self._last_sens_args  = None
        self._bt_figure       = None
        self._bt_canvas       = None
        self._last_bt_result  = None
        self._strategy_bt_figure = None
        self._strategy_bt_canvas = None
        self._last_strategy_bt_result = None
        self._strategy_bt_cancel = None
        self._strategy_bt_running = False
        self._strategy_pricing_cache = {}
        self._strategy_compare_results = []

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
        self.v_src_q           = ctk.StringVar(value="默认")
        self.v_src_spread      = ctk.StringVar(value="手工")
        self.v_src_p_down      = ctk.StringVar(value="模型")
        self.v_src_dk          = ctk.StringVar(value="模型")
        self.v_src_call_ratio  = ctk.StringVar(value="手工")
        self.v_src_down_reset_trigger_ratio = ctk.StringVar(value="默认")
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
        self._announcement_preview_in_flight: set[str] = set()

        self._attach_manual_source_tracking()
        self.v_p_down.trace_add("write", lambda *_: self._refresh_p_down_hint())
        self._refresh_p_down_hint()
        self._bond_code_trace = self.v_bond_code.trace_add("write", self._on_bond_code_write)

    def _refresh_p_down_hint(self) -> None:
        """把用户输入的 hazard λ 换算成 P(1 年内至少 1 次)。

        UI 输入是 % 形式 (e.g. 15 → λ=0.15); 用户惯于按"年内概率"思考,
        所以旁边挂一个 read-only 提示, 让强度与概率的口径都看得见。
        """
        try:
            lam = float(str(self.v_p_down.get()).strip()) / 100.0
        except (TypeError, ValueError):
            self.v_p_down_hint.set("")
            return
        if lam <= 0:
            self.v_p_down_hint.set("≈ 0% /年")
        elif lam >= 6.0:
            self.v_p_down_hint.set("≈ 100% /年")
        else:
            import math
            p1y = 1.0 - math.exp(-lam)
            self.v_p_down_hint.set(f"≈ {p1y * 100:.1f}% /年")

    # ── UI 构建 ────────────────────────────────────────────
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
        # 投资者工作流: 先筛候选 → 钻单债 → 验模型 → 做压力测试
        self._tab_names = [E("📦 批量"), E("⚡ 定价"), E("📈 回测"), E("🎯 策略"), E("🔥 敏感性")]

        header = ctk.CTkFrame(self, fg_color=BG_CARD, corner_radius=0, height=60)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(0, weight=1)
        header.grid_columnconfigure(1, weight=0)
        header.grid_columnconfigure(2, weight=1)
        header.grid_propagate(False)

        left_frame = ctk.CTkFrame(header, fg_color="transparent")
        left_frame.grid(row=0, column=0, sticky="w", padx=(18, 12), pady=15)
        ctk.CTkLabel(left_frame, text="CBLens", font=(FONT_FAMILY, 20, "bold"), text_color=TEXT).pack(side="left")
        ctk.CTkLabel(left_frame, text="PDE", font=(FONT_FAMILY, 10, "bold"), text_color=ACCENT,
                     fg_color=BG_INPUT, corner_radius=4, padx=6, pady=2).pack(side="left", padx=(8, 16), pady=(0, 2))

        is_dark = ctk.get_appearance_mode() == "Dark"
        self.theme_switch = ctk.CTkSwitch(left_frame, text="深色模式" if is_dark else "浅色模式",
                                          command=self._toggle_theme, width=40, progress_color=ACCENT,
                                          font=(FONT_FAMILY, 12), text_color=TEXT_DIM)
        self.theme_switch.pack(side="left")
        if is_dark:
            self.theme_switch.select()
        else:
            self.theme_switch.deselect()

        self.tab_seg = ctk.CTkSegmentedButton(
            header, values=self._tab_names, command=self._switch_tab,
            font=(FONT_FAMILY, 13, "bold"), height=30,
            selected_color=ACCENT, selected_hover_color=ACCENT_HOVER,
            unselected_color=BG_INPUT, unselected_hover_color=BTN_HOVER,
            text_color=TEXT, text_color_disabled=TEXT_DIM,
            corner_radius=8)
        # 启动默认进入批量复核页 — 投资入口先看候选池, 再钻到单债
        self.tab_seg.set(E("📦 批量"))
        self.tab_seg.grid(row=0, column=1, pady=15)

        right_frame = ctk.CTkFrame(header, fg_color="transparent")
        right_frame.grid(row=0, column=2, sticky="e", padx=(12, 18), pady=15)

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
                "选择行情和利率来源；转债基础信息固定读取本地条款库, 并可由 Wind 刷新")

        # 全市场 cb_data / 状态字段同步入口 — 替代命令行 cb-sync-* 调用
        self.btn_sync_pool = ctk.CTkButton(
            right_frame, text=E("🌐 同步池"),
            command=self._open_pool_sync_menu,
            fg_color=BG_INPUT, hover_color=BTN_HOVER, text_color=TEXT,
            font=(FONT_FAMILY, 12), width=82, height=30, corner_radius=6)
        self.btn_sync_pool.pack(side="left", padx=(0, 10))
        Tooltip(self.btn_sync_pool,
                "弹出菜单: 同步全市场基础信息、刷新停牌强赎等状态字段、同步公告事件")

        AutocompleteEntry(
            right_frame, textvariable=self.v_bond_code,
            get_suggestions=self._search_bond_index,
            on_select=self._on_bond_code_selected,
            width=130, font=(FONT_MONO, 13), placeholder_text="如 128009.SZ",
            border_width=0, corner_radius=6, fg_color=BG_INPUT, height=30,
        ).pack(side="left")
        self.btn_wind = ctk.CTkButton(
            right_frame, text=E("📥 同步"), command=self._fetch_wind,
            fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=("#ffffff", "#11111b"),
            font=(FONT_FAMILY, 12, "bold"), width=76, height=30, corner_radius=6)
        self.btn_wind.pack(side="left", padx=(6, 0))
        Tooltip(self.btn_wind, "读取本地条款库, 拉取正股行情和历史波动率")

        self.btn_refresh_terms = ctk.CTkButton(
            right_frame, text=E("🔄"), command=self._refresh_terms,
            fg_color=BG_INPUT, hover_color=BTN_HOVER, text_color=TEXT,
            font=(FONT_FAMILY, 14), width=30, height=30, corner_radius=6)
        self.btn_refresh_terms.pack(side="left", padx=(4, 0))
        Tooltip(self.btn_refresh_terms,
                "强制用 Wind 刷新当前债的本地条款\n适用于下修或评级变更后")

        self.btn_save = ctk.CTkButton(right_frame, text=E("💾"), command=self._save_preset, width=30, height=30, fg_color=BG_INPUT, hover_color=BTN_HOVER, text_color=TEXT, font=(FONT_FAMILY, 14), corner_radius=6)
        self.btn_save.pack(side="left", padx=(8, 0))
        Tooltip(self.btn_save, "保存当前参数预设  (Ctrl+S)")
        self.btn_load = ctk.CTkButton(right_frame, text=E("📂"), command=self._load_preset, width=30, height=30, fg_color=BG_INPUT, hover_color=BTN_HOVER, text_color=TEXT, font=(FONT_FAMILY, 14), corner_radius=6)
        self.btn_load.pack(side="left", padx=(6, 0))
        Tooltip(self.btn_load, "加载参数预设  (Ctrl+O)")

    def _build_statusbar(self):
        sb = ctk.CTkFrame(self, fg_color=BG_CARD, corner_radius=0, height=28)
        sb.grid(row=2, column=0, sticky="ew")
        sb.grid_columnconfigure(2, weight=1)
        sb.grid_propagate(False)

        # 左: 数据时效 (cb_data 文件 mtime + 最近一次行情拉取相对时间)
        self.v_data_freshness = ctk.StringVar(value="")
        self.lbl_data_freshness = ctk.CTkLabel(
            sb, textvariable=self.v_data_freshness,
            text_color=TEXT_DIM, font=(FONT_FAMILY, 11))
        self.lbl_data_freshness.grid(row=0, column=0, sticky="w", padx=15, pady=4)

        ctk.CTkLabel(sb, text="信息", text_color=TEXT_DIM,
                     font=(FONT_FAMILY, 11)).grid(row=0, column=1, sticky="w", padx=(8, 8), pady=4)
        self.lbl_ref = ctk.CTkLabel(sb, textvariable=self.v_ref_info,
                                     text_color=TEXT_DIM, font=(FONT_FAMILY, 11))
        self.lbl_ref.grid(row=0, column=2, sticky="w", padx=(0, 15), pady=4)
        Tooltip(self.lbl_ref, self.v_ref_detail)
        self.lbl_status = ctk.CTkLabel(sb, textvariable=self.v_status,
                                        text_color=TEXT, font=(FONT_FAMILY, 11, "bold"))
        self.lbl_status.grid(row=0, column=3, sticky="e", padx=15, pady=4)

        self._last_quote_fetch_ts = None  # 由 wind_sync 等模块在拉取行情时更新
        self._last_batch_saved_ts = None  # 由 batch tab 加载/保存缓存时更新
        self._update_data_freshness()
        self._schedule_data_freshness_tick()

    # ── 数据时效 ──────────────────────────────────────────
    def _update_data_freshness(self):
        from pathlib import Path
        from datetime import datetime as _dt
        cb_path = Path(project_bundle_path())
        parts: list[str] = []
        if cb_path.exists():
            mtime = _dt.fromtimestamp(cb_path.stat().st_mtime)
            parts.append(f"条款库 {self._humanize_age(mtime)}")
        else:
            parts.append("条款库 未同步")
        if self._last_quote_fetch_ts is not None:
            parts.append(f"行情 {self._humanize_age(self._last_quote_fetch_ts)}")
        batch_ts = getattr(self, "_last_batch_saved_ts", None)
        if batch_ts is not None:
            parts.append(f"批量 {self._humanize_age(batch_ts)}")
        self.v_data_freshness.set("  ·  ".join(parts))

    def _set_batch_freshness(self, saved_at_iso: str | None) -> None:
        """缓存元数据中的 saved_at (ISO) → 状态栏 '批量 Xh前'."""
        from datetime import datetime as _dt
        if not saved_at_iso:
            self._last_batch_saved_ts = None
        else:
            try:
                self._last_batch_saved_ts = _dt.fromisoformat(saved_at_iso)
            except ValueError:
                self._last_batch_saved_ts = None
        if hasattr(self, "v_data_freshness"):
            self._update_data_freshness()

    def _schedule_data_freshness_tick(self):
        # 每 60s 刷新一次相对时间
        self.after(60_000, self._on_data_freshness_tick)

    def _on_data_freshness_tick(self):
        try:
            self._update_data_freshness()
        finally:
            self._schedule_data_freshness_tick()

    @staticmethod
    def _humanize_age(ts) -> str:
        from datetime import datetime as _dt
        delta = _dt.now() - ts
        secs = int(delta.total_seconds())
        if secs < 60:
            return "刚刚"
        if secs < 3600:
            return f"{secs // 60}min前"
        if secs < 86400:
            hours = secs / 3600
            return f"{hours:.1f}h前" if hours < 10 else f"{int(hours)}h前"
        days = secs / 86400
        return f"{int(days)}d前"

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

        # 默认显示批量页 (与 tab_seg 初始选中一致)
        pricing_tab.build(self, self._tab_frames[E("⚡ 定价")])
        backtest_tab.build(self, self._tab_frames[E("📈 回测")])
        strategy_tab.build(self, self._tab_frames[E("🎯 策略")])
        sensitivity_tab.build(self, self._tab_frames[E("🔥 敏感性")])
        batch_tab.build(self, self._tab_frames[E("📦 批量")])
        self._active_tab_name = E("📦 批量")
        self._sync_active_tab_frame()

    # ── 响应式布局 ────────────────────────────────────────
    def _responsive_profile_for_size(self, width: int, height: int) -> str:
        if width >= 2100 and height >= 900:
            return "xl"
        if width >= 1650 and height >= 820:
            return "wide"
        if width < 1220 or height < 760:
            return "compact"
        return "normal"

    def _on_root_configure(self, event=None):
        if event is not None and event.widget is not self:
            return
        if self._responsive_after_id is not None:
            self.after_cancel(self._responsive_after_id)
        # Tk 在拖动窗口时会密集触发 Configure; 等尺寸稳定后再做昂贵的 CTk 缩放。
        self._responsive_after_id = self.after(250, self._apply_responsive_layout)

    def _apply_responsive_layout(self):
        self._responsive_after_id = None
        width = max(1, self.winfo_width())
        height = max(1, self.winfo_height())
        profile_name = self._responsive_profile_for_size(width, height)
        profile = self._RESPONSIVE_PROFILES[profile_name]
        last_size = self._responsive_last_size
        profile_changed = profile_name != self._responsive_profile_name
        major_resize = (
            last_size is None
            or abs(width - last_size[0]) >= 120
            or abs(height - last_size[1]) >= 80
        )
        if not profile_changed and not major_resize:
            return
        self._responsive_last_size = (width, height)

        if profile_changed:
            self._responsive_profile_name = profile_name
            batch_tab.refresh_theme(self)
            self.after_idle(self._sync_active_tab_frame)

        if self._active_tab_name == E("⚡ 定价"):
            self._place_pricing_sash(width=width)

    def _place_pricing_sash(self, width: int | None = None):
        paned = getattr(self, "pricing_paned", None)
        if paned is None:
            return
        profile_name = self._responsive_profile_name or self._responsive_profile_for_size(
            self.winfo_width(), self.winfo_height())
        base_left = self._RESPONSIVE_PROFILES[profile_name]["pricing_left"]
        width = max(1, width or self.winfo_width())
        left = min(max(base_left, int(width * 0.28)), int(width * 0.42))
        if self._pricing_sash_left is not None and abs(left - self._pricing_sash_left) < 24:
            return
        try:
            paned.sash_place(0, left, 1)
            self._pricing_sash_left = left
        except Exception:
            pass

    def _sync_active_tab_frame(self):
        selected = self._active_tab_name or E("📦 批量")
        for name, f in self._tab_frames.items():
            if name == selected:
                f.grid(row=0, column=0, sticky="nsew")
                f.tkraise()
            else:
                f.grid_remove()

    def _switch_tab(self, selected):
        if selected not in self._tab_frames:
            selected = E("📦 批量")
        self._active_tab_name = selected
        self._sync_active_tab_frame()
        if selected == E("⚡ 定价"):
            self.after_idle(self._place_pricing_sash)

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
            (self.v_q, self.v_src_q),
            (self.v_spread, self.v_src_spread),
            (self.v_p_down, self.v_src_p_down),
            (self.v_dk, self.v_src_dk),
            (self.v_down_reset_trigger_ratio, self.v_src_down_reset_trigger_ratio),
            (self.v_call_ratio, self.v_src_call_ratio),
            (self.v_put_ratio, self.v_src_put_ratio),
            (self.v_put_years, self.v_src_put_years),
            (self.v_call_notice, self.v_src_call_notice),
        )
        for value_var, source_var in pairs:
            handle = value_var.trace_add(
                "write",
                lambda *_args, var=value_var, src=source_var: self._mark_manual_source(
                    src, value_var=var),
            )
            self._source_trace_handles.append((value_var, handle))

    def _mark_manual_source(self, source_var, *, value_var=None):
        if self._programmatic_update:
            return
        source_var.set("手工")
        if (
            any(
                value_var is var
                for var in (
                    self.v_S0,
                    self.v_K,
                    self.v_down_reset_trigger_ratio,
                    self.v_cur_date,
                )
            )
            and hasattr(self, "_auto_fill_p_down_from_current_x")
        ):
            self.after_idle(lambda: self._auto_fill_p_down_from_current_x())
        if hasattr(self, "_refresh_terms_snapshot_card"):
            self.after_idle(self._refresh_terms_snapshot_card)

    def _set_field(self, value_var, value, source_var=None, source=None):
        self._programmatic_update = True
        try:
            value_var.set(value)
        finally:
            self._programmatic_update = False
        if source_var is not None and source is not None:
            source_var.set(source)
        if hasattr(self, "_refresh_terms_snapshot_card"):
            self.after_idle(self._refresh_terms_snapshot_card)

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

    def _flush_pending_bond_autoload(self):
        """立即触发挂起的 650ms 自动加载防抖, 让缓存字段同步可用.

        用于 sensitivity / pricing 等运行入口, 场景: 用户输代码后立刻点运行,
        防抖还没到, 字段仍是空 → _collect_params 报错. 调用此方法可同步触发
        cache fill (K, dates 等); 行情 S0/σ 仍走异步 Wind/akshare.
        """
        if getattr(self, "_auto_fetch_after", None) is None:
            return
        self.after_cancel(self._auto_fetch_after)
        self._auto_fetch_after = None
        code = self._normalize_bond_code(self.v_bond_code.get())
        if code and BOND_CODE_RE.match(code):
            self._auto_load_bond_code(code)

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
            return str(raw.get("_meta", {}).get("source") or "条款库")
        except Exception:
            return "条款库"

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
        return "条款库"

    # ── 快捷键 ────────────────────────────────────────────
    def _bind_shortcuts(self):
        self.bind_all("<Control-Return>", lambda e: self._run_pricing())
        self.bind_all("<Control-KP_Enter>", lambda e: self._run_pricing())
        self.bind_all("<Control-s>", lambda e: self._save_preset())
        self.bind_all("<Control-o>", lambda e: self._load_preset())
        # 收敛诊断 (开发者工具): UI 已下线, 仅保留快捷键供调试时触发
        self.bind_all("<Control-d>", lambda e: self._convergence_check())

    # ── 主题切换 ──────────────────────────────────────────
    def _toggle_theme(self):
        """切换深浅色模式"""
        if self.theme_switch.get() == 1:
            ctk.set_appearance_mode("Dark")
            self.theme_switch.configure(text="深色模式")
        else:
            ctk.set_appearance_mode("Light")
            self.theme_switch.configure(text="浅色模式")

        # 刷新 ttk Treeview 样式 + 行标签色彩 (批量表 + 关注池)
        batch_tab.refresh_theme(self)
        if hasattr(self, "pricing_paned"):
            self.pricing_paned.configure(
                bg=get_color(BORDER),
                sashrelief="flat",
            )

        # 刷新 matplotlib 图表色彩
        if self._last_bt_result is not None:
            self._render_backtest_chart(self._last_bt_result)
        if self._last_strategy_bt_result is not None:
            self._render_strategy_backtest_result(self._last_strategy_bt_result)
        if getattr(self, "_last_sens_args", None) is not None:
            self._render_sensitivity_chart(*self._last_sens_args)

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

    # ── 错误处理 ───────────────────────────────────────────
    def _on_error(self, msg, show_dialog=True):
        self._stop_progress()
        self.v_status.set(E(f"❌ {msg}"))
        if show_dialog:
            self.v_ref_info.set("❌ 获取失败")
            self.v_result.set("—")
            self.lbl_result.configure(text_color=RED)
            messagebox.showerror("错误", str(msg))

    # ── 参数预设保存/加载 ──────────────────────────────────
    _PRESET_VARS = (
        "v_bond_code", "v_S0", "v_K", "v_face", "v_redemp",
        "v_cur_date", "v_mat_date", "v_iss_date", "v_conv_date",
        "v_coupons", "v_sigma", "v_r", "v_q", "v_spread", "v_p_down", "v_dk",
        "v_down_reset_trigger_ratio", "v_call_ratio", "v_put_ratio",
        "v_put_years", "v_call_notice", "v_M", "v_N",
        "v_vol_window", "v_market_price",
        "v_bt_start", "v_bt_end", "v_bt_freq",
        "v_data_source",
        # 策略页配置 (模板/选债逻辑/范围过滤/成本); 文件路径与日期不纳入, 保持预设可移植
        "v_st_freq", "v_st_top_n", "v_st_template", "v_st_view",
        "v_st_pool_mode", "v_st_history_mode", "v_st_codes",
        "v_st_min_price", "v_st_max_price",
        "v_st_min_premium", "v_st_max_premium", "v_st_min_deviation", "v_st_max_deviation",
        "v_st_min_sigma", "v_st_max_sigma", "v_st_min_rating", "v_st_min_balance",
        "v_st_min_turnover", "v_st_delist_window", "v_st_cost", "v_st_benchmark",
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
            target = Path(path)
            tmp = target.with_name(target.name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            tmp.replace(target)
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
                        value = data[name]
                        if name == "v_st_history_mode":
                            value = normalize_strategy_history_mode(value)
                        getattr(self, name).set(value)
            finally:
                self._suppress_bond_autoload = False
                self._programmatic_update = False
            for src in (
                self.v_src_S0, self.v_src_K, self.v_src_face, self.v_src_redemp,
                self.v_src_cur_date, self.v_src_mat_date, self.v_src_iss_date,
                self.v_src_conv_date, self.v_src_coupons, self.v_src_sigma,
                self.v_src_r, self.v_src_q, self.v_src_spread, self.v_src_p_down, self.v_src_dk,
                self.v_src_down_reset_trigger_ratio, self.v_src_call_ratio,
                self.v_src_put_ratio, self.v_src_put_years,
                self.v_src_call_notice,
            ):
                src.set("预设")
            self.v_status.set(f"已加载预设 {path}")
        except Exception as exc:
            messagebox.showerror("加载失败", str(exc))


# ── 启动 ──────────────────────────────────────────────────
def main():
    app = CBPricerApp()
    app.mainloop()


if __name__ == "__main__":
    main()

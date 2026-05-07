"""公告事件面板 + 事件同步."""
from __future__ import annotations

import logging
import threading
from datetime import date, datetime, timedelta
from tkinter import messagebox

import customtkinter as ctk

from ...announcement_pdf import (
    fetch_only as _fetch_announcement,
    open_with_system_viewer as _open_pdf_with_system_viewer,
    render_pdf_pages as _render_pdf_pages,
)
from ...cb_event_sync import sync_cb_events
from ...cb_events import (
    CBEvent,
    CBEventStore,
    project_events_path,
    reload_default_event_store,
)
from ..constants import BOND_CODE_RE, EVENT_SYNC_STALE_HOURS
from ..theme import (
    ACCENT, BG_INPUT, BORDER,
    BTN_CTRL, BTN_HOVER,
    FONT_FAMILY,
    ORANGE, TEXT, TEXT_DIM,
)
from ..widgets import Tooltip, create_card


logger = logging.getLogger(__name__)


class EventsMixin:
    """公告事件面板 / 同步 / 应用回 cb_data."""

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

        # "写回条款库" 按钮已移除: 日常定价直接读事件表, 不需要手工固化;
        # 维护动作改为通过 ``apply_events_to_terms`` 公共函数 / CLI 触发。

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

            # 公告 PDF 预览: 整行可点击 + 右侧 📄 affordance
            if ev.url:
                preview_btn = ctk.CTkLabel(
                    row_frame, text="📄", text_color=ACCENT,
                    font=(FONT_FAMILY, 13), width=22, height=18,
                    fg_color="transparent", cursor="hand2")
                preview_btn.grid(row=0, column=3, padx=(2, 6), pady=4, sticky="e")
                Tooltip(preview_btn, "预览公告原文 (首次会下载到本地缓存)")
                handler = (lambda _e=None, _ev=ev: self._open_announcement_preview(_ev))
                for w in (row_frame, badge, info_lbl, src_lbl, preview_btn):
                    w.bind("<Button-1>", handler)
                try:
                    row_frame.configure(cursor="hand2")
                    info_lbl.configure(cursor="hand2")
                except Exception:
                    pass

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
            "underlying_suspension": "#fe640b",
            "underlying_st_risk":    "#d20f39",
            "underlying_st_clear":   "#40a02b",
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
            "underlying_suspension": "正股停牌",
            "underlying_st_risk":    "正股ST",
            "underlying_st_clear":   "撤销ST",
        }.get(event_type, event_type[:4])

    def _open_announcement_preview(self, event: CBEvent) -> None:
        """点击事件行 → 在 APP 内开新窗口预览 PDF.

        旧版直接调用系统阅读器, 现在改为 ``CTkToplevel`` 内嵌图片预览; 失败
        时仍可单击 "↗ 系统阅读器打开" 切到系统阅读器作为兜底。
        """
        url = event.url
        if not url:
            messagebox.showinfo(
                "无 PDF 链接",
                "该事件没有 URL — 可能是手工录入或来源 provider 未提供.")
            return
        if url in self._announcement_preview_in_flight:
            return
        self._announcement_preview_in_flight.add(url)
        self.v_event_summary.set("📄 公告下载中…")

        win = self._build_pdf_preview_window(event)
        threading.Thread(
            target=self._announcement_preview_worker,
            args=(event, url, win),
            daemon=True,
        ).start()

    def _build_pdf_preview_window(self, event: CBEvent):
        """组装 PDF 预览窗口 (顶部状态条 + 滚动正文), 返回 win 句柄字典."""
        win = ctk.CTkToplevel(self)
        win.title(f"📄 {event.bond_code} {event.event_date.isoformat()}")
        win.geometry("840x920")
        win.transient(self)

        header = ctk.CTkFrame(win, fg_color="transparent")
        header.pack(fill="x", padx=12, pady=(10, 6))
        ctk.CTkLabel(
            header,
            text=f"{event.bond_code}  ·  {event.event_date.isoformat()}  ·  "
                 f"{(event.raw_title or '')[:48]}",
            font=(FONT_FAMILY, 13, "bold"), text_color=TEXT,
        ).pack(side="left")

        status_var = ctk.StringVar(value="📄 下载并渲染中…")
        ctk.CTkLabel(
            header, textvariable=status_var, text_color=TEXT_DIM,
            font=(FONT_FAMILY, 11),
        ).pack(side="left", padx=(10, 0))

        sysbtn = ctk.CTkButton(
            header, text="↗ 系统阅读器打开", width=140, height=26,
            font=(FONT_FAMILY, 11), state="disabled",
            fg_color=BTN_CTRL, hover_color=BTN_HOVER, text_color=TEXT_DIM,
        )
        sysbtn.pack(side="right")

        body = ctk.CTkScrollableFrame(win, fg_color=BG_INPUT)
        body.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        # 把 image 引用挂到 frame 上, 防 GC
        body._image_refs = []  # type: ignore[attr-defined]

        return {"win": win, "body": body, "status_var": status_var, "sysbtn": sysbtn}

    def _announcement_preview_worker(self, event: CBEvent, url: str, ui: dict):
        """后台线程: 下载 → 渲染 → 主线程填充图片. 失败时回退系统阅读器."""
        try:
            path = _fetch_announcement(event.bond_code, event.event_date, url)
            self.after(0, lambda: self._wire_pdf_sysbtn(ui, path))
            self.after(0, lambda: ui["status_var"].set("📄 渲染中…"))
            try:
                images = _render_pdf_pages(path, dpi=110)
            except RuntimeError as render_exc:
                self.after(
                    0,
                    lambda exc=render_exc: ui["status_var"].set(
                        f"⚠ 内嵌渲染失败 ({exc}), 已切到系统阅读器"))
                try:
                    _open_pdf_with_system_viewer(path)
                except Exception as open_exc:
                    self.after(
                        0,
                        lambda exc=open_exc: messagebox.showerror(
                            "系统阅读器失败", str(exc)))
                return
            self.after(0, lambda: self._populate_pdf_pages(ui, images, path))
            self.after(0, lambda: self.v_event_summary.set(f"📄 已打开: {path.name}"))
        except Exception as exc:
            self.after(0, lambda exc=exc: ui["status_var"].set(f"❌ 加载失败: {exc}"))
            self.after(0, lambda exc=exc: self.v_event_summary.set(f"📄 预览失败: {exc}"))
            self.after(
                0,
                lambda exc=exc: messagebox.showerror(
                    "公告预览失败",
                    f"无法下载公告 PDF:\n{exc}\n\nURL: {url}"))
        finally:
            self._announcement_preview_in_flight.discard(url)

    def _wire_pdf_sysbtn(self, ui: dict, path) -> None:
        """下载完成后, 把 "系统阅读器打开" 按钮激活."""
        btn = ui.get("sysbtn")
        if btn is None:
            return
        try:
            btn.configure(
                state="normal", text_color=TEXT,
                command=lambda p=path: self._open_pdf_in_system_viewer(p))
        except Exception:
            pass

    @staticmethod
    def _open_pdf_in_system_viewer(path) -> None:
        try:
            _open_pdf_with_system_viewer(path)
        except Exception as exc:
            messagebox.showerror("系统阅读器失败", str(exc))

    def _populate_pdf_pages(self, ui: dict, images, path) -> None:
        """把渲染好的 PIL.Image 列表显示到滚动区, 自适应窗口宽度."""
        body = ui.get("body")
        status_var = ui.get("status_var")
        if body is None or not body.winfo_exists():
            return  # 窗口已关闭, 静默丢弃
        target_w = 780  # ScrollableFrame 内部宽度 (840 - padding)
        for idx, img in enumerate(images):
            ratio = target_w / img.width if img.width else 1.0
            out_h = max(1, int(img.height * ratio))
            ctkimg = ctk.CTkImage(
                light_image=img, dark_image=img, size=(target_w, out_h))
            lbl = ctk.CTkLabel(body, image=ctkimg, text="")
            lbl.pack(pady=(0, 6) if idx == len(images) - 1 else (0, 4))
            body._image_refs.append(ctkimg)  # type: ignore[attr-defined]
        if status_var is not None:
            status_var.set(f"📄 共 {len(images)} 页  ·  {path.name}")

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
            from ...cninfo_provider import CninfoAnnouncementProvider
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
            from ...cninfo_provider import CninfoAnnouncementProvider
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

    # 注: ``_apply_events_to_current`` (写回事件到本地条款库) 已下线 —
    # 该按钮在 GUI 上几乎没人点 (日常定价直接读事件表), 维护动作请走
    # ``convertible_bond.cb_event_sync`` 公共函数 / CLI。

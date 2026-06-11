"""
可复用 GUI 组件: 表单行, 卡片, 折叠面板, 悬浮提示, 自动补全输入框, 日期选择.
"""
import calendar
from datetime import date, datetime
import tkinter as tk
import customtkinter as ctk

from .theme import (
    BG_CARD,
    BG_INPUT,
    BORDER,
    TEXT,
    TEXT_DIM,
    ORANGE,
    ACCENT,
    GREEN,
    BTN_HOVER,
    FONT_FAMILY,
    FONT_MONO,
    get_color,
)


def _source_label_color(source: str):
    """根据参数来源字符串返回配色元组 (浅色, 深色).

    行情/历史数据 → 绿; 手工/预设 → 橙; 其余 (模型/系统/默认) → 暗."""
    if source in ("Wind", "行情", "历史", "利率", "股息"):
        return GREEN
    if source in ("手工", "预设"):
        return ORANGE
    return TEXT_DIM


def _source_label_text(source: str) -> str:
    """字段级来源不显示在表单里, 保持输入区安静。"""
    return ""


ENTRY_HEIGHT = 28
LABEL_SLOT_WIDTH = 130
EXTRA_SLOT_WIDTH = 82
SOURCE_SLOT_WIDTH = 50


def _form_row(parent, label_text, var, row, wind=False, extra_widget=None,
              width=130, source_var=None, tooltip=None, show_source=False,
              *, compact=False, custom_widget=None):
    """统一表单行布局.

    列结构:

        [label]                 [primary] [extra-slot] [source-slot]
        ←── col 0 ──→     ←──────── ent_container (col 1, sticky=w) ────────→

    - ``primary`` 默认是 ``CTkEntry``; 传 ``custom_widget`` (factory) 时换成它
      自己创建的控件, 例如波动率下拉。
    - label 槽宽固定, 输入区从同一 x 坐标开始, 避免中文标签长短造成输入框参差。
    - ``extra-slot`` / ``source-slot`` 即使没内容也保留固定宽度的空 spacer,
      所以同一 section 内不同行的操作按钮、来源标签也垂直对齐。
    - ``compact=True`` 时跳过两个 spacer (用于 各年票息 这种需要更长输入框的行,
      但仍保持输入框左边沿对齐)。
    """
    row_frame = ctk.CTkFrame(parent, fg_color="transparent")
    row_frame.grid(row=row, column=0, sticky="ew", padx=16, pady=4)
    row_frame.grid_columnconfigure(0, minsize=LABEL_SLOT_WIDTH)
    row_frame.grid_columnconfigure(1, weight=1)

    lbl = ctk.CTkLabel(row_frame, text=f"  {label_text}", text_color=TEXT_DIM,
                       font=(FONT_FAMILY, 13), width=LABEL_SLOT_WIDTH,
                       anchor="w")
    lbl.grid(row=0, column=0, sticky="w")

    ent_container = ctk.CTkFrame(row_frame, fg_color="transparent")
    ent_container.grid(row=0, column=1, sticky="w")

    if custom_widget is not None:
        primary = custom_widget(ent_container)
        primary.grid(row=0, column=0, sticky="e")
    else:
        primary = ctk.CTkEntry(
            ent_container, textvariable=var, width=width, font=(FONT_MONO, 13),
            border_width=0, corner_radius=6,
            fg_color=BG_INPUT, text_color=TEXT, height=ENTRY_HEIGHT)
        primary.grid(row=0, column=0, sticky="e")

    if not compact:
        # extra slot — 即使没内容也占位
        extra_cell = ctk.CTkFrame(
            ent_container, fg_color="transparent",
            width=EXTRA_SLOT_WIDTH, height=ENTRY_HEIGHT)
        extra_cell.grid(row=0, column=1, padx=(6, 0))
        extra_cell.grid_propagate(False)
        if extra_widget is not None:
            extra_widget(extra_cell).pack(side="left")

        # source slot — 同上
        src_cell = ctk.CTkFrame(
            ent_container, fg_color="transparent",
            width=SOURCE_SLOT_WIDTH, height=ENTRY_HEIGHT)
        src_cell.grid(row=0, column=2, padx=(4, 0))
        src_cell.grid_propagate(False)
        if show_source and source_var is not None:
            src_lbl = ctk.CTkLabel(
                src_cell, text=_source_label_text(source_var.get()), anchor="w",
                text_color=_source_label_color(source_var.get()),
                font=(FONT_FAMILY, 10))
            src_lbl.pack(side="left", fill="both", expand=True)

            def _on_src_change(*_, lbl=src_lbl, var=source_var):
                val = var.get()
                lbl.configure(
                    text=_source_label_text(val),
                    text_color=_source_label_color(val),
                )

            source_var.trace_add("write", _on_src_change)
    else:
        # compact: 没 spacer; 如果传了 extra_widget 就紧贴 entry 右侧
        if extra_widget is not None:
            extra_widget(ent_container).grid(row=0, column=1, padx=(6, 0))

    if tooltip:
        Tooltip(lbl, tooltip)
        if isinstance(primary, ctk.CTkEntry):
            Tooltip(primary, tooltip)

    return primary


def create_card(parent, title, row, col, icon=""):
    card = ctk.CTkFrame(parent, fg_color=BG_CARD, corner_radius=12)
    card.grid(row=row, column=col, sticky="nsew", padx=6, pady=6)
    card.grid_columnconfigure(0, weight=1)

    header = ctk.CTkFrame(card, fg_color="transparent")
    header.grid(row=0, column=0, sticky="ew", padx=16, pady=(12, 4))

    title_lbl = ctk.CTkLabel(header, text=f"{icon} {title}" if icon else title, font=(FONT_FAMILY, 14, "bold"), text_color=TEXT)
    title_lbl.pack(side="left")

    content = ctk.CTkFrame(card, fg_color="transparent")
    content.grid(row=1, column=0, sticky="nsew", pady=(0, 10))
    content.grid_columnconfigure(0, weight=1)
    return content


class CollapsibleSection(ctk.CTkFrame):
    """可折叠面板: 点击标题行展开/收起内容

    Header 设计 (强化可见性):
    - 浅色背景块 (BG_INPUT) + 圆角, 像一个可点击容器
    - 全色 (TEXT) bold 文字, 不再被当作 ambient hint
    - ▾/▸ 实心三角箭头, 状态切换时方向变化
    """
    _ARROW_OPEN = "▾"
    _ARROW_CLOSED = "▸"

    def __init__(self, parent, title, expanded=False, **kw):
        kw.setdefault("fg_color", "transparent")
        super().__init__(parent, **kw)
        self.grid_columnconfigure(0, weight=1)
        self._expanded = expanded
        self._title = title
        self.header_btn = ctk.CTkButton(
            self, text=self._header_text(), command=self.toggle,
            anchor="w", fg_color=BG_INPUT, hover_color=BTN_HOVER,
            text_color=TEXT, font=(FONT_FAMILY, 13, "bold"),
            height=34, corner_radius=8)
        self.header_btn.grid(row=0, column=0, sticky="ew")
        self.content = ctk.CTkFrame(self, fg_color="transparent")
        self.content.grid_columnconfigure(0, weight=1)
        if expanded:
            self.content.grid(row=1, column=0, sticky="nsew", pady=(6, 0))

    def _header_text(self) -> str:
        arrow = self._ARROW_OPEN if self._expanded else self._ARROW_CLOSED
        return f"   {arrow}   {self._title}"

    def toggle(self):
        self._expanded = not self._expanded
        self.header_btn.configure(text=self._header_text())
        if self._expanded:
            self.content.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
        else:
            self.content.grid_remove()


class Tooltip:
    """轻量悬浮提示: 鼠标悬停 delay_ms 后弹出, 离开/点击立即收起."""
    def __init__(self, widget, text, delay_ms=450):
        self.widget = widget
        self.text = text
        self.delay = delay_ms
        self._after_id = None
        self._tip = None
        widget.bind("<Enter>", self._on_enter)
        widget.bind("<Leave>", self._on_leave)
        widget.bind("<ButtonPress>", self._on_leave)

    def _on_enter(self, _event=None):
        self._cancel()
        self._after_id = self.widget.after(self.delay, self._show)

    def _on_leave(self, _event=None):
        self._cancel()
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None

    def _cancel(self):
        if self._after_id is not None:
            self.widget.after_cancel(self._after_id)
            self._after_id = None

    def _show(self):
        if self._tip is not None:
            return
        text = self.text() if callable(self.text) else self.text
        if hasattr(text, "get"):
            text = text.get()
        text = str(text or "").strip()
        if not text:
            return
        x = self.widget.winfo_rootx() + self.widget.winfo_width() // 2
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 8
        tip = ctk.CTkToplevel(self.widget)
        tip.wm_overrideredirect(True)
        try:
            tip.attributes("-topmost", True)
        except Exception:
            pass
        tip.configure(fg_color=BG_CARD)
        # 用 frame + 1px border 加细灰描边, 比纯色卡片更精致
        frame = ctk.CTkFrame(
            tip, fg_color=BG_CARD, corner_radius=8,
            border_width=1, border_color=BORDER)
        frame.pack()
        lbl = ctk.CTkLabel(
            frame, text=text, font=(FONT_FAMILY, 12),
            text_color=TEXT, fg_color=BG_CARD,
            justify="left", anchor="w", wraplength=320,
            padx=12, pady=8)
        lbl.pack()
        tip.update_idletasks()
        # 居中对齐到目标控件下方
        tip.geometry(f"+{x - tip.winfo_width() // 2}+{y}")
        self._tip = tip


class AutocompleteEntry(ctk.CTkFrame):
    """带下拉建议的输入框.

    输入内容变化时调用 `get_suggestions(query)` 获取候选, 弹下拉列表;
    支持 ↑/↓ 选择, Enter 确认, Esc/失焦关闭, 鼠标点击选中.

    Parameters
    ----------
    parent
    textvariable : ctk.StringVar
    get_suggestions : Callable[[str], list[tuple[str, str]]]
        返回 [(写回 entry 的值, 显示在下拉的标签), ...]; query 为空通常返回 [].
    on_select : Callable[[str], None] | None
        选中后的回调 (textvariable 已被设置).
    max_rows : int
    其余 kwargs 透传给内部 CTkEntry (width/height/font/...).
    """
    def __init__(self, parent, textvariable, get_suggestions,
                 on_select=None, max_rows=8, **entry_kw):
        super().__init__(parent, fg_color="transparent")
        self.var = textvariable
        self.get_suggestions = get_suggestions
        self.on_select = on_select
        self.max_rows = max_rows

        self.entry = ctk.CTkEntry(self, textvariable=textvariable, **entry_kw)
        self.entry.pack(fill="both", expand=True)

        self._popup = None
        self._listbox = None
        self._items = []
        self._hide_after = None
        self._suppress = False

        self._trace = textvariable.trace_add("write", self._on_var_write)
        self.entry.bind("<Down>", self._on_down)
        self.entry.bind("<Up>", self._on_up)
        self.entry.bind("<Return>", self._on_return)
        self.entry.bind("<KP_Enter>", self._on_return)
        self.entry.bind("<Escape>", lambda _e: self._hide())
        self.entry.bind("<FocusOut>", self._on_focus_out)

    def _on_var_write(self, *_):
        if self._suppress:
            return
        query = self.var.get().strip()
        items = list(self.get_suggestions(query))
        self._items = items[: self.max_rows]
        if not self._items:
            self._hide()
            return
        self._show()

    def _ensure_popup(self):
        if self._popup is not None:
            return
        bg = get_color(BG_INPUT)
        fg = get_color(TEXT)
        sel = get_color(ACCENT)
        bd = get_color(BORDER)
        top = tk.Toplevel(self.entry)
        top.wm_overrideredirect(True)
        top.attributes("-topmost", True)
        top.configure(bg=bd)
        lb = tk.Listbox(
            top, activestyle="none",
            bg=bg, fg=fg, selectbackground=sel, selectforeground="#ffffff",
            highlightthickness=0, borderwidth=0,
            font=(FONT_MONO, 12), exportselection=False)
        lb.pack(fill="both", expand=True, padx=1, pady=1)
        lb.bind("<Button-1>", self._on_lb_click)
        lb.bind("<Return>", self._on_return)
        lb.bind("<KP_Enter>", self._on_return)
        self._popup = top
        self._listbox = lb

    def _show(self):
        self._ensure_popup()
        lb = self._listbox
        lb.delete(0, "end")
        for _val, label in self._items:
            lb.insert("end", label)
        n = len(self._items)
        lb.configure(height=n)
        if n > 0:
            lb.selection_clear(0, "end")
            lb.selection_set(0)
        self.entry.update_idletasks()
        x = self.entry.winfo_rootx()
        y = self.entry.winfo_rooty() + self.entry.winfo_height() + 2
        w = self.entry.winfo_width()
        self._popup.update_idletasks()
        h = self._popup.winfo_reqheight()
        self._popup.geometry(f"{w}x{h}+{x}+{y}")
        self._popup.deiconify()

    def _hide(self):
        self._cancel_hide()
        if self._popup is not None:
            try:
                self._popup.destroy()
            except Exception:
                pass
            self._popup = None
            self._listbox = None

    def _cancel_hide(self):
        if self._hide_after is not None:
            try:
                self.entry.after_cancel(self._hide_after)
            except Exception:
                pass
            self._hide_after = None

    def _on_focus_out(self, _e):
        # 给鼠标点击下拉留 150ms 时间; 若期间触发 _select 会主动 _hide
        self._cancel_hide()
        self._hide_after = self.entry.after(150, self._hide)

    def _on_down(self, _e):
        if not self._listbox or self._listbox.size() == 0:
            return None
        cur = self._listbox.curselection()
        i = (cur[0] + 1) if cur else 0
        if i >= self._listbox.size():
            i = 0
        self._listbox.selection_clear(0, "end")
        self._listbox.selection_set(i)
        self._listbox.see(i)
        return "break"

    def _on_up(self, _e):
        if not self._listbox or self._listbox.size() == 0:
            return None
        cur = self._listbox.curselection()
        i = (cur[0] - 1) if cur else 0
        if i < 0:
            i = self._listbox.size() - 1
        self._listbox.selection_clear(0, "end")
        self._listbox.selection_set(i)
        self._listbox.see(i)
        return "break"

    def _on_return(self, _e):
        if not self._listbox or self._listbox.size() == 0:
            return None
        cur = self._listbox.curselection()
        if not cur:
            return None
        self._select(cur[0])
        return "break"

    def _on_lb_click(self, e):
        idx = self._listbox.nearest(e.y)
        self._select(idx)

    def _select(self, idx):
        if idx < 0 or idx >= len(self._items):
            return
        value, _label = self._items[idx]
        self._suppress = True
        try:
            self.var.set(value)
        finally:
            self._suppress = False
        self.entry.icursor("end")
        self._hide()
        if self.on_select:
            self.on_select(value)


# ── 日期选择: entry + 📅 按钮, 点击弹出月历 ────────────────────────────────

class DatePickerPopup(ctk.CTkToplevel):
    """月历弹窗: 解析 var 当前值定位, 选定后写回 var 并关闭.

    关闭策略 (避开 macOS Tk 8.6 的 overrideredirect+focus_force 闪退):
    - 在 root 上绑定 Button-1, 点击 popup 外部区域时关闭
    - ESC 键关闭
    - 选中日期时关闭
    """

    def __init__(self, master, var, anchor, on_close=None):
        super().__init__(master)
        self.var = var
        self._anchor = anchor
        self._on_close_callback = on_close
        # 不能用 self._root 命名 — 会覆盖 tkinter.Misc._root 方法
        self._master_top = master.winfo_toplevel()

        self.overrideredirect(True)
        try:
            self.transient(self._master_top)
        except Exception:
            pass
        try:
            self.attributes("-topmost", True)
        except Exception:
            pass
        self.configure(fg_color=BG_CARD)

        try:
            today = datetime.strptime(var.get(), "%Y-%m-%d").date()
        except (ValueError, AttributeError, TypeError):
            today = date.today()
        self._selected = today
        self._view = today.replace(day=1)

        self._build_ui()
        self._render_grid()

        # 定位在 anchor 控件下方
        self.update_idletasks()
        x = anchor.winfo_rootx()
        y = anchor.winfo_rooty() + anchor.winfo_height() + 4
        self.geometry(f"+{x}+{y}")
        self.lift()  # macOS: 确保 popup 在所有窗口最上层

        # 监听主窗 Configure (移动/缩放) → 关闭 popup, 避免位置错位
        # 监听 Unmap (最小化/切 tab) → 关闭
        self._config_bind_id = self._master_top.bind(
            "<Configure>", self._on_master_configure, add="+")
        self._unmap_bind_id = self._master_top.bind(
            "<Unmap>", lambda _e: self._close(), add="+")

        # 外部点击检测: after_idle 推迟到当前 Button-1 处理完毕之后,
        # 否则触发本次创建的同一个 Button-1 事件会立刻把刚开的弹窗关掉.
        self._root_bind_id = None

        def _install_root_bind():
            try:
                self._root_bind_id = self._master_top.bind(
                    "<Button-1>", self._on_root_click, add="+")
            except Exception:
                self._root_bind_id = None

        self.after_idle(_install_root_bind)
        self.bind("<Escape>", lambda _e: self._close())
        self.bind("<Destroy>", self._on_destroy)

    def _on_master_configure(self, _event=None):
        # 主窗位置/大小变化, popup 不再可信, 关闭即可
        self._close()

    def _on_root_click(self, event):
        if not self.winfo_exists():
            return
        x, y = event.x_root, event.y_root
        # 点击在 popup 内部 → 不关
        wx, wy = self.winfo_rootx(), self.winfo_rooty()
        ww, wh = self.winfo_width(), self.winfo_height()
        if wx <= x < wx + ww and wy <= y < wy + wh:
            return
        # 点击在 anchor entry 上 → 不关 (再次点击同一 entry 应该是切换, 而非关闭)
        try:
            if self._anchor is not None and self._anchor.winfo_exists():
                ax = self._anchor.winfo_rootx()
                ay = self._anchor.winfo_rooty()
                aw = self._anchor.winfo_width()
                ah = self._anchor.winfo_height()
                if ax <= x < ax + aw and ay <= y < ay + ah:
                    return
        except Exception:
            pass
        self._close()

    def _on_destroy(self, _event=None):
        for attr, seq in (("_root_bind_id", "<Button-1>"),
                          ("_config_bind_id", "<Configure>"),
                          ("_unmap_bind_id", "<Unmap>")):
            bid = getattr(self, attr, None)
            if bid is not None:
                try:
                    self._master_top.unbind(seq, bid)
                except Exception:
                    pass
                setattr(self, attr, None)
        if self._on_close_callback is not None:
            try:
                self._on_close_callback()
            except Exception:
                pass
            self._on_close_callback = None

    def _close(self):
        try:
            if self.winfo_exists():
                self.destroy()
        except Exception:
            pass

    def _build_ui(self):
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=8, pady=(8, 4))
        ctk.CTkButton(
            header, text="◀", width=28, height=24, corner_radius=4,
            fg_color="transparent", hover_color=BTN_HOVER, text_color=TEXT,
            font=(FONT_FAMILY, 11, "bold"),
            command=lambda: self._shift_month(-1)
        ).pack(side="left")
        self.title_lbl = ctk.CTkLabel(
            header, font=(FONT_FAMILY, 13, "bold"), text_color=TEXT)
        self.title_lbl.pack(side="left", expand=True)
        ctk.CTkButton(
            header, text="▶", width=28, height=24, corner_radius=4,
            fg_color="transparent", hover_color=BTN_HOVER, text_color=TEXT,
            font=(FONT_FAMILY, 11, "bold"),
            command=lambda: self._shift_month(1)
        ).pack(side="right")

        dow = ctk.CTkFrame(self, fg_color="transparent")
        dow.pack(fill="x", padx=10)
        for d in ("一", "二", "三", "四", "五", "六", "日"):
            ctk.CTkLabel(dow, text=d, width=30, font=(FONT_FAMILY, 10),
                         text_color=TEXT_DIM).pack(side="left", padx=1)

        self._grid = ctk.CTkFrame(self, fg_color="transparent")
        self._grid.pack(padx=10, pady=(2, 10))

    def _shift_month(self, delta):
        m = self._view.month + delta
        y = self._view.year
        while m < 1:
            m += 12
            y -= 1
        while m > 12:
            m -= 12
            y += 1
        self._view = self._view.replace(year=y, month=m)
        self._render_grid()

    def _render_grid(self):
        for child in self._grid.winfo_children():
            child.destroy()
        self.title_lbl.configure(text=f"{self._view.year} 年 {self._view.month} 月")

        weeks = calendar.Calendar(firstweekday=0).monthdayscalendar(
            self._view.year, self._view.month)
        while len(weeks) < 6:
            weeks.append([0] * 7)

        today = date.today()
        for week in weeks:
            row = ctk.CTkFrame(self._grid, fg_color="transparent")
            row.pack(fill="x")
            for day in week:
                if day == 0:
                    ctk.CTkLabel(row, text="", width=30, height=24).pack(
                        side="left", padx=1, pady=1)
                    continue
                cell_date = self._view.replace(day=day)
                is_selected = (cell_date == self._selected)
                is_today = (cell_date == today)
                if is_selected:
                    fg, txt = ACCENT, ("#ffffff", "#11111b")
                elif is_today:
                    fg, txt = "transparent", ACCENT
                else:
                    fg, txt = "transparent", TEXT
                ctk.CTkButton(
                    row, text=str(day), width=30, height=24,
                    font=(FONT_MONO, 11), corner_radius=4,
                    fg_color=fg, hover_color=BTN_HOVER, text_color=txt,
                    command=lambda d=day: self._pick(d)
                ).pack(side="left", padx=1, pady=1)

    def _pick(self, day):
        picked = self._view.replace(day=day)
        self.var.set(picked.strftime("%Y-%m-%d"))
        self._close()


def make_date_picker(parent, var, *, entry_width=120):
    """返回 CTkEntry: 点击时弹出 DatePickerPopup; 仍可手动输入(ESC 关闭弹窗)."""
    entry = ctk.CTkEntry(
        parent, textvariable=var, font=(FONT_MONO, 13),
        fg_color=BG_INPUT, border_width=0, corner_radius=6,
        text_color=TEXT, height=28, width=entry_width)
    entry._date_popup = None

    def _log(msg):
        try:
            with open("/tmp/cb-datepicker.log", "a") as _f:
                _f.write(msg + "\n")
        except Exception:
            pass

    def _open(_event=None):
        _log(f"[OPEN] click, current popup={entry._date_popup}")
        try:
            existing = entry._date_popup
            if existing is not None and existing.winfo_exists():
                _log("[OPEN] already open, skip")
                return
            popup = DatePickerPopup(
                entry.winfo_toplevel(), var, anchor=entry,
                on_close=lambda: (_log("[CLOSE-CB] ref cleared"),
                                  setattr(entry, "_date_popup", None)))
            entry._date_popup = popup
            _log(f"[OPEN] popup created at "
                 f"({popup.winfo_rootx()},{popup.winfo_rooty()}) "
                 f"size=({popup.winfo_width()}x{popup.winfo_height()})")
        except Exception:
            import traceback
            _log(f"[OPEN-ERR] {traceback.format_exc()}")

    entry.bind("<Button-1>", _open)
    return entry

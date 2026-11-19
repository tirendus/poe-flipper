"""Stash window: what is in the stash, what it is worth on the currency
exchange, and which of it may be sold.

    pythonw stash_window.py            (or start_stash.bat)

Read-only: it talks to the website and to poe.ninja, never to the game.
All data handling lives in stash_data.py; this file is only the UI.
"""
import argparse
import copy
import ctypes
import queue
import sys
import threading
import time
import traceback
import tkinter as tk
import tkinter.font as tkfont
from ctypes import wintypes
from pathlib import Path
from tkinter import ttk

import stash_data as sd

BG = "#1e1a16"
PANEL = "#2b251e"
FG = "#e8ddc8"
ACCENT = "#c8a24a"
BTN_BG = "#3a332a"
DIM = "#8a8272"
FAINT = "#5a5448"
BAD = "#d06050"

ON, OFF, NONE = "☑", "☐", "—"     # ticked box, empty box, dash


def fmt_int(n):
    return f"{int(round(n)):,}".replace(",", " ")


def fmt_chaos(v):
    """A chaos total: whole numbers once the decimals stop mattering."""
    if v >= 100:
        return fmt_int(v)
    return f"{v:.1f}" if v >= 10 else f"{v:.2f}"


def fmt_each(v):
    return fmt_int(v) if v >= 1000 else f"{v:.4g}"


def fmt_age(seconds):
    if seconds < 90:
        return "just now"
    if seconds < 90 * 60:
        return f"{round(seconds / 60)} min ago"
    if seconds < 36 * 3600:
        return f"{round(seconds / 3600)} h ago"
    return f"{round(seconds / 86400)} d ago"


def window_dpi(widget):
    """DPI of the monitor `widget`'s window is on (96 = 100% scale)."""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetParent(widget.winfo_id()) or widget.winfo_id()
        dpi = user32.GetDpiForWindow(wintypes.HWND(hwnd))
        if dpi:
            return int(dpi)
    except (OSError, AttributeError, tk.TclError):
        pass                                    # before Windows 10 1607
    return int(round(widget.winfo_fpixels("1i")))


def log_error(text):
    try:
        with open(sd.DATA_DIR / "stash_window.log", "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {text}\n")
    except OSError:
        pass


class StashWindow:
    #        id       heading      chars  anchor
    COLS = (("sell", "✓", 5, "center"),
            ("name", "Item", 36, "w"),
            ("qty", "Qty", 10, "e"),
            ("each", "Each, c", 11, "e"),
            ("total", "Total, c", 12, "e"),
            ("tabs", "Tabs", 24, "w"))

    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.busy = False
        self.state = sd.load_state()
        self.cache = sd.load_stash_cache()
        self.prices = None
        self.table = {}
        self.rows, self.unpriced, self.by_name = [], [], {}
        self.sort_col, self.sort_desc = "total", True
        self.message = None             # (text, is_error) shown until replaced
        self.tab_vars = {}
        self.session = None             # running selling session, if any

        # Fonts are sized in pixels for the monitor the window is on.  A
        # point size would follow Tk's one scaling for the whole process
        # (the primary monitor's), and that must stay as it is: the popup
        # and the selling bar are drawn with it on the game's screen.
        self.dpi = window_dpi(root)
        self.fonts = [(tkfont.Font(family="Segoe UI", weight=weight), points)
                      for points, weight in ((10, "normal"), (10, "bold"),
                                             (9, "normal"), (12, "bold"))]
        self.font, self.bold, self.small, self.big = (
            font for font, _points in self.fonts)
        self._size_fonts()
        self._build_window()
        self._build_layout()
        # an error inside a button handler must be seen, not swallowed:
        # under pythonw there is no console for the traceback to land in
        self.root.report_callback_exception = self._callback_error
        self._build_tabs()
        self._rebuild()
        self.root.after(80, self._poll)
        self._start_prices(force=False)

    def _callback_error(self, exc, val, tb):
        log_error("unhandled error:\n"
                  + "".join(traceback.format_exception(exc, val, tb)))
        try:
            self._say(f"Error: {val} - details in stash_window.log",
                      error=True)
        except tk.TclError:
            pass

    # ---------------------------------------------------------- layout ---

    def _size_fonts(self):
        for font, points in self.fonts:
            font.configure(size=-max(1, round(points * self.dpi / 72)))

    def _build_window(self):
        root = self.root
        root.title("PoE Flipper - stash")
        root.configure(bg=BG)
        try:
            root.iconbitmap(str(sd.APP_DIR / "flipper.ico"))
        except tk.TclError:
            pass
        root.bind("<Configure>", self._on_resize)

    def _rescale(self, dpi):
        """The window now sits on a monitor with another scale.  Tk does not
        follow that by itself — the window would keep its size in pixels and
        fill most of a smaller screen — so the fonts are resized for the new
        monitor and everything is laid out again around them."""
        ratio = dpi / self.dpi
        self.dpi = dpi
        self._size_fonts()
        width, height = self.root.winfo_width(), self.root.winfo_height()
        self.shell.destroy()
        self._build_layout(first=False)
        self._build_tabs()
        self._rebuild()
        self._set_busy(self.busy)
        if self.session is not None:
            self.btn_sell.configure(text="Stop selling")
        self.root.geometry(f"{max(1, round(width * ratio))}x"
                           f"{max(1, round(height * ratio))}")

    def _build_layout(self, first=True):
        """Everything inside the window.  Every size in here derives from
        the fonts, so building it again after _size_fonts is all a change
        of monitor scale needs."""
        # the content lives in one frame that _rescale can throw away
        root = self.shell = tk.Frame(self.root, bg=BG)
        root.pack(fill="both", expand=True)
        unit = self.font.measure("0")
        pad = self.pad = unit

        top = tk.Frame(root, bg=BG)
        top.pack(fill="x", padx=pad, pady=(pad, 0))
        self.title = tk.Label(top, text="Stash", bg=BG, fg=ACCENT, font=self.big)
        self.title.pack(side="left")
        self.btn_prices = self._button(top, "Refresh prices",
                                       lambda: self._start_prices(force=True))
        self.btn_prices.pack(side="right")
        self.btn_stash = self._button(top, "Refresh stash", self._start_stash,
                                      primary=True)
        self.btn_stash.pack(side="right", padx=(0, pad))

        self.status = tk.Label(root, text="", bg=BG, fg=DIM, font=self.small,
                               anchor="w", justify="left",
                               wraplength=unit * 100)
        self.status.pack(fill="x", padx=pad, pady=(2, 0))

        self.tabs_frame = tk.Frame(root, bg=BG)
        self.tabs_frame.pack(fill="x", padx=pad, pady=(pad // 2, 0))

        self.summary = tk.Label(root, text="", bg=BG, fg=FG, font=self.bold,
                                anchor="w", justify="left")
        self.summary.pack(fill="x", padx=pad, pady=(pad // 2, 0))
        self.summary2 = tk.Label(root, text="", bg=BG, fg=DIM,
                                 font=self.small, anchor="w", justify="left")
        self.summary2.pack(fill="x", padx=pad, pady=(0, 3))

        sellbar = tk.Frame(root, bg=BG)
        sellbar.pack(side="bottom", fill="x", padx=pad, pady=(0, pad // 2))
        self.btn_sell = self._button(sellbar, "Start selling",
                                     self._toggle_selling, primary=True)
        self.btn_sell.pack(side="right")
        self._button(sellbar, "Sell selected",
                     lambda: self._start_selling(selected=True)
                     ).pack(side="right", padx=(0, pad // 2))
        self.sell_all_var = tk.BooleanVar(value=self.state["sell_all"])
        self._check(sellbar, "Sell everything (don't ask how many)",
                    self.sell_all_var,
                    lambda: self._set_option("sell_all", self.sell_all_var)
                    ).pack(side="left")
        self.undercut_var = tk.BooleanVar(value=self.state["undercut"])
        self._check(sellbar, "Undercut all (don't ask the price)",
                    self.undercut_var,
                    lambda: self._set_option("undercut", self.undercut_var)
                    ).pack(side="left", padx=(pad * 2, 0))

        bottom = tk.Frame(root, bg=BG)
        bottom.pack(side="bottom", fill="x", padx=pad, pady=pad // 2)
        self._button(bottom, "Tick all",
                     lambda: self._set_all(True)).pack(side="left")
        self._button(bottom, "Untick all",
                     lambda: self._set_all(False)).pack(side="left",
                                                        padx=(pad // 2, pad * 2))
        tk.Label(bottom, text="Hide stacks under", bg=BG, fg=DIM,
                 font=self.small).pack(side="left")
        self.min_var = tk.StringVar(value=f"{self.state['min_total']:g}")
        entry = tk.Entry(bottom, textvariable=self.min_var, width=6,
                         bg=PANEL, fg=FG, insertbackground=FG, relief="flat",
                         font=self.font, justify="right")
        entry.pack(side="left", padx=4, ipady=1)
        entry.bind("<Return>", self._apply_min)
        entry.bind("<FocusOut>", self._apply_min)
        tk.Label(bottom, text="c", bg=BG, fg=DIM,
                 font=self.small).pack(side="left")
        self.unpriced_var = tk.BooleanVar(value=self.state["show_unpriced"])
        self.unpriced_box = self._check(bottom, "Show unpriced",
                                        self.unpriced_var, self._toggle_unpriced)
        self.unpriced_box.pack(side="right")

        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("Stash.Treeview", background=PANEL,
                        fieldbackground=PANEL, foreground=FG, borderwidth=0,
                        font=self.font,
                        rowheight=int(self.font.metrics("linespace") * 1.45))
        style.configure("Stash.Treeview.Heading", background=BTN_BG,
                        foreground=ACCENT, relief="flat", font=self.bold,
                        padding=(4, 3))
        style.map("Stash.Treeview", background=[("selected", "#4a4030")],
                  foreground=[("selected", "#fff6e0")])
        style.map("Stash.Treeview.Heading",
                  background=[("active", "#4a4234")])
        style.configure("Stash.Vertical.TScrollbar", background=BTN_BG,
                        troughcolor=BG, bordercolor=BG, arrowcolor=ACCENT,
                        lightcolor=BTN_BG, darkcolor=BTN_BG)

        body = tk.Frame(root, bg=BG)
        body.pack(fill="both", expand=True, padx=pad)
        self.tree = ttk.Treeview(body, style="Stash.Treeview", show="headings",
                                 columns=[c[0] for c in self.COLS],
                                 selectmode="extended")
        bar = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview,
                            style="Stash.Vertical.TScrollbar")
        self.tree.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        for cid, _title, chars, anchor in self.COLS:
            self.tree.column(cid, width=unit * chars, anchor=anchor,
                             stretch=cid in ("name", "tabs"),
                             minwidth=unit * 4)
            self.tree.heading(cid, command=lambda c=cid: self._sort_by(c))
        self.tree.tag_configure("keep", foreground=DIM)
        self.tree.tag_configure("fixed", foreground=ACCENT)
        self.tree.tag_configure("unpriced", foreground=FAINT)
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<Double-Button-1>", self._on_double)
        self.tree.bind("<space>", self._on_space)

        if first:
            width = unit * (sum(c[2] for c in self.COLS) + 6)
            height = int(self.font.metrics("linespace") * 34)
            self.root.geometry(f"{width}x{height}")
        self.root.minsize(unit * 70,
                          int(self.font.metrics("linespace") * 16))
        self._on_resize(None)

    def _button(self, parent, text, command, primary=False):
        return tk.Button(
            parent, text=text, command=command, relief="flat", padx=10,
            pady=2, cursor="hand2", font=self.bold if primary else self.font,
            bg=ACCENT if primary else BTN_BG, fg=BG if primary else FG,
            activebackground="#e0be6a" if primary else "#4a4234",
            activeforeground=BG if primary else FG,
            disabledforeground=FAINT)

    def _check(self, parent, text, var, command):
        """A tick box drawn with the same glyphs as the table — tk's own
        indicator stays a few pixels wide on a scaled 4K display."""
        label = tk.Label(parent, bg=BG, font=self.small, anchor="w",
                         cursor="hand2")
        label.caption = text

        def paint(*_):
            try:
                label.configure(
                    text=f"{ON if var.get() else OFF} {label.caption}",
                    fg=FG if var.get() else DIM)
            except tk.TclError:     # label already destroyed (tabs rebuilt)
                pass

        def click(_event):
            var.set(not var.get())
            command()

        label.paint = paint
        var.trace_add("write", paint)
        label.bind("<Button-1>", click)
        paint()
        return label

    def _on_resize(self, event):
        """Keep the wrapping text as wide as the window (`event` is None
        when the layout has just been rebuilt)."""
        if event is not None and event.widget is not self.root:
            return
        width = event.width if event is not None else self.root.winfo_width()
        wrap = max(300, width - 2 * self.pad)
        try:
            for label in (self.status, self.summary, self.summary2):
                label.configure(wraplength=wrap)
        except (AttributeError, tk.TclError):   # mid-rebuild
            pass

    def _build_tabs(self):
        for child in self.tabs_frame.winfo_children():
            child.destroy()
        self.tab_vars = {}
        tabs = (self.cache or {}).get("tabs", [])
        if not tabs:
            return
        tk.Label(self.tabs_frame, text="Tabs to include:", bg=BG,
                 fg=DIM, font=self.small).grid(row=0, column=0, columnspan=5,
                                               sticky="w")
        for n, tab in enumerate(tabs):
            var = tk.BooleanVar(value=sd.tab_enabled(self.state, tab))
            kind = tab["type"].removesuffix("Stash") or "?"
            label = tab["name"] if tab["name"] == kind else \
                f"{tab['name']} · {kind}"
            box = self._check(self.tabs_frame, label, var,
                              lambda t=tab: self._toggle_tab(t))
            box.grid(row=1 + n // 5, column=n % 5, sticky="w", padx=(0, 14))
            self.tab_vars[tab["id"]] = var
        for col in range(5):
            self.tabs_frame.grid_columnconfigure(col, weight=1, uniform="tab")

    # ------------------------------------------------------------ data ---

    def _league(self):
        return sd.config_league()

    def _rebuild(self):
        self.table = sd.price_table(self.prices) if self.prices else {}
        if self.cache:
            self.rows, self.unpriced = sd.build_rows(self.cache, self.table,
                                                     self.state)
        else:
            self.rows, self.unpriced = [], []
        self.by_name = {r["name"]: r for r in self.rows}
        self._fill_tree()
        self._update_summary()
        self._update_status()

    def _sorted_rows(self):
        col = self.sort_col
        if col == "name":
            def key(r): return r["name"].lower()
        elif col == "tabs":
            def key(r): return ", ".join(r["tabs"]).lower()
        elif col == "sell":
            def key(r): return (r["fixed"], r["sell"])
        else:
            def key(r): return r[col]
        return sorted(self.rows, key=key, reverse=self.sort_desc)

    def _values(self, row):
        mark = NONE if row["fixed"] else (ON if row["sell"] else OFF)
        return (mark, row["name"], fmt_int(row["qty"]), fmt_each(row["each"]),
                fmt_chaos(row["total"]), self._tabs_text(row))

    @staticmethod
    def _tabs_text(row):
        # leading gap: the column sits right against the right-aligned total
        return "   " + ", ".join(row["tabs"])

    @staticmethod
    def _tag(row):
        return "fixed" if row["fixed"] else ("" if row["sell"] else "keep")

    def _fill_tree(self):
        keep_sel = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        floor = self.state["min_total"]
        for row in self._sorted_rows():
            if sd.is_small(row, floor):
                continue
            self.tree.insert("", "end", iid=row["name"],
                             values=self._values(row), tags=(self._tag(row),))
        if self.state["show_unpriced"]:
            for row in self.unpriced:
                self.tree.insert(
                    "", "end", iid="?" + row["name"], tags=("unpriced",),
                    values=(NONE, row["name"], fmt_int(row["qty"]), NONE, NONE,
                            self._tabs_text(row)))
        self.tree.selection_set([i for i in keep_sel if self.tree.exists(i)])
        for cid, title, _chars, _anchor in self.COLS:
            arrow = ""
            if cid == self.sort_col:
                arrow = " ▼" if self.sort_desc else " ▲"
            self.tree.heading(cid, text=title + arrow)
        self.unpriced_box.caption = f"Show unpriced ({len(self.unpriced)})"
        self.unpriced_box.paint()

    def _update_summary(self):
        s = sd.summarize(self.rows, self.state["min_total"])
        div = sd.divine_price(self.table)

        def money(chaos):
            text = f"{fmt_chaos(chaos)} c"
            if div:
                text += f" ≈ {chaos / div:.1f} div"
            return text

        if not self.cache:
            self.summary.configure(text="")
            self.summary2.configure(text="")
            return
        self.summary.configure(text=f"To sell: {money(s['selling'])} "
                                    f"({s['selling_count']} items)")
        parts = [f"Everything the exchange takes: {money(s['all'])}"]
        if s["chaos"]:
            parts.append(f"chaos already held: {fmt_int(s['chaos'])}")
        self.summary2.configure(text="  ·  ".join(parts))

    def _update_status(self):
        if self.message:
            text, is_error = self.message
            self.status.configure(text=text, fg=BAD if is_error else DIM)
            return
        now = time.time()
        league = self._league()
        self.title.configure(text=f"Stash · {league}" if league else "Stash")
        if not self.cache:
            self.status.configure(
                fg=DIM, text="No stash loaded yet - press Refresh stash.")
            return
        bits = [f"Stash: {fmt_age(now - self.cache.get('fetched_at', 0))}"]
        if self.prices and self.prices.get("types"):
            bits.append(
                f"prices: {fmt_age(now - self.prices.get('fetched_at', 0))}")
            if self.prices.get("errors"):
                bits.append("some prices did not refresh: "
                            + ", ".join(self.prices["errors"]))
        else:
            bits.append("no prices yet")
        s = sd.summarize(self.rows, self.state["min_total"])
        if s["small_count"]:
            bits.append(f"small change hidden: {s['small_count']} items worth "
                        f"{fmt_chaos(s['small'])} c")
        waiting = [t["name"] for t in self.cache["tabs"]
                   if sd.tab_enabled(self.state, t) and not t.get("fetched")]
        if waiting:
            bits.append("not loaded: " + ", ".join(waiting)
                        + " - press Refresh stash")
        self.status.configure(text="  ·  ".join(bits), fg=DIM)

    # --------------------------------------------------------- actions ---

    def _save(self):
        sd.save_state(self.state)

    def _set_sell(self, names, value):
        """Tick (True), untick (False) or flip (None) the given rows."""
        unchecked = set(self.state["unchecked"])
        changed = False
        for name in names:
            row = self.by_name.get(name)
            if row is None or row["fixed"]:
                continue
            new = (not row["sell"]) if value is None else value
            if new == row["sell"]:
                continue
            row["sell"] = new
            (unchecked.discard if new else unchecked.add)(name)
            if self.tree.exists(name):
                self.tree.item(name, values=self._values(row),
                               tags=(self._tag(row),))
            changed = True
        if changed:
            self.state["unchecked"] = sorted(unchecked)
            self._save()
            self._update_summary()

    def _set_all(self, value):
        self._set_sell([r["name"] for r in self.rows
                        if not sd.is_small(r, self.state["min_total"])], value)

    def _on_click(self, event):
        if self.tree.identify_region(event.x, event.y) == "cell" and \
                self.tree.identify_column(event.x) == "#1":
            iid = self.tree.identify_row(event.y)
            if iid:
                self._set_sell([iid], None)

    def _on_double(self, event):
        if self.tree.identify_region(event.x, event.y) == "cell":
            iid = self.tree.identify_row(event.y)
            if iid:
                self._set_sell([iid], None)

    def _on_space(self, _event):
        names = [i for i in self.tree.selection() if i in self.by_name]
        if names:
            # one key press gives the whole selection the same answer
            self._set_sell(names, not self.by_name[names[0]]["sell"])
        return "break"

    def _sort_by(self, col):
        if col == self.sort_col:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_col = col
            self.sort_desc = col in ("qty", "each", "total", "sell")
        self._fill_tree()

    def _toggle_tab(self, tab):
        self.state["tabs"][tab["id"]] = bool(self.tab_vars[tab["id"]].get())
        self._save()
        self.message = None
        self._rebuild()

    def _set_option(self, key, var):
        # the selling session reads this same dict for every item
        self.state[key] = bool(var.get())
        self._save()

    def _toggle_selling(self):
        if self.session is not None:
            self.session.stop()
        else:
            self._start_selling(selected=False)

    def _start_selling(self, selected):
        """Queue up everything ticked, or — `selected` — only the rows
        highlighted in the table (whatever their tick boxes say: handy for
        trying one cheap item, or selling a few things out of turn)."""
        if self.session is not None:
            return
        if selected:
            rows = [dict(self.by_name[i], sell=True)
                    for i in self.tree.selection()
                    if i in self.by_name and not self.by_name[i]["fixed"]]
            floor = 0.0
            if not rows:
                self._say("Select a row in the table first: click its name "
                          "(Ctrl+click for several).", error=True)
                return
        else:
            rows, floor = self.rows, self.state["min_total"]
        import seller
        if seller.SellQueue(rows, floor).current() is None:
            self._say("Nothing to sell: no ticked items with a price.",
                      error=True)
            return
        import sell_ui
        self.session = sell_ui.SellSession(
            self.root, rows, self.state, on_end=self._selling_ended,
            min_total=floor)
        self.message = None
        self._update_status()
        self.btn_sell.configure(text="Stop selling")

    def _selling_ended(self):
        self.session = None
        try:
            self.btn_sell.configure(text="Start selling")
        except tk.TclError:
            pass

    def _toggle_unpriced(self):
        self.state["show_unpriced"] = bool(self.unpriced_var.get())
        self._save()
        self._fill_tree()

    def _apply_min(self, _event=None):
        raw = self.min_var.get().strip().replace(",", ".")
        try:
            value = max(0.0, float(raw)) if raw else 0.0
        except ValueError:
            value = self.state["min_total"]
        self.min_var.set(f"{value:g}")
        if value != self.state["min_total"]:
            self.state["min_total"] = value
            self._save()
            self._fill_tree()
            self._update_summary()
            self._update_status()

    # ------------------------------------------------------ background ---

    def _set_busy(self, busy):
        self.busy = busy
        state = "disabled" if busy else "normal"
        self.btn_stash.configure(state=state)
        self.btn_prices.configure(state=state)

    def _say(self, text, error=False):
        self.message = (text, error)
        self._update_status()

    def _start_stash(self):
        if self.busy:
            return
        try:
            cfg = sd.load_config()
        except sd.StashError as e:
            self._say(str(e), error=True)
            return
        self._set_busy(True)
        self._say("Stash: connecting to the site...")
        state = copy.deepcopy(self.state)
        threading.Thread(target=self._stash_worker, args=(cfg, state),
                         daemon=True).start()

    def _stash_worker(self, cfg, state):
        try:
            cache = sd.fetch_stash(
                cfg, lambda tab: sd.tab_enabled(state, tab),
                progress=lambda m: self.q.put(("progress", "Stash: " + m)))
            self.q.put(("stash", cache))
        except sd.StashError as e:
            self.q.put(("error", str(e)))
        except Exception:
            log_error("stash refresh failed:\n" + traceback.format_exc())
            self.q.put(("error", "Stash refresh failed - details in "
                                 "stash_window.log"))

    def _start_prices(self, force):
        league = self._league()
        if self.busy or not league:
            return
        self._set_busy(True)
        threading.Thread(target=self._prices_worker, args=(league, force),
                         daemon=True).start()

    def _prices_worker(self, league, force):
        try:
            prices = sd.load_prices(
                league, force=force,
                progress=lambda m: self.q.put(("progress", m.capitalize())))
            self.q.put(("prices", prices))
        except sd.StashError as e:
            self.q.put(("error", str(e)))
        except Exception:
            log_error("price refresh failed:\n" + traceback.format_exc())
            self.q.put(("error", "Price refresh failed - details in "
                                 "stash_window.log"))

    def _follow_monitor(self):
        """Rescale once the window has been dropped on a monitor with
        another scale (not mid-drag: resizing under the cursor fights the
        move)."""
        try:
            dpi = window_dpi(self.root)
            dragging = ctypes.windll.user32.GetAsyncKeyState(0x01) & 0x8000
            if dpi != self.dpi and not dragging:
                self._rescale(dpi)
        except (tk.TclError, OSError, AttributeError):
            pass

    def _poll(self):
        self._follow_monitor()
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "progress":
                    self._say(payload)
                elif kind == "error":
                    self._set_busy(False)
                    self._say(payload, error=True)
                elif kind == "stash":
                    self.cache = payload
                    self.message = None
                    self._set_busy(False)
                    self._build_tabs()
                    self._rebuild()
                    self._start_prices(force=False)     # no-op while fresh
                elif kind == "prices":
                    self.prices = payload
                    self.message = None
                    self._set_busy(False)
                    self._rebuild()
        except queue.Empty:
            pass
        self.root.after(80, self._poll)


def relaunch_in_venv():
    """Double-clicking this file runs it with the system Python, which has
    none of the packages the selling part needs — they live in .venv.  The
    table would still open and the sell buttons would silently do nothing,
    so start over with the venv's interpreter instead.  True = relaunched."""
    venv = sd.APP_DIR / ".venv"
    exe = venv / "Scripts" / "pythonw.exe"
    try:
        if Path(sys.prefix).resolve() == venv.resolve() or not exe.exists():
            return False
    except OSError:
        return False
    import subprocess
    subprocess.Popen([str(exe), str(Path(__file__).resolve())] + sys.argv[1:],
                     cwd=str(sd.APP_DIR))
    return True


def set_dpi_awareness():
    """Per-monitor DPI awareness, the same as flipper.py: screenshots,
    clicks and windows are all in physical pixels, and the popup and the
    selling bar — always on the game's monitor — come out right wherever
    the stash window happens to be.  Tk does not follow a window to a
    monitor with another scale, so StashWindow rescales itself (_rescale).

    System awareness looked simpler, since Windows then scales the window,
    but it also scales every window created while this one sits on the
    other monitor: the popup came out half size on the game's screen.

    The "v2" flavour is asked for first — with it Windows scales the title
    bar too when the window changes monitor — then plain per-monitor."""
    try:    # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2, Windows 10 1703+
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(
                ctypes.c_void_p(-4)):
            return
    except (OSError, AttributeError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (OSError, AttributeError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (OSError, AttributeError):
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", metavar="DIR",
                    help="keep config, caches and tick boxes in another folder")
    args = ap.parse_args()
    if relaunch_in_venv():
        return
    if args.data_dir:
        sd.DATA_DIR = Path(args.data_dir)
    set_dpi_awareness()
    root = tk.Tk()
    StashWindow(root)
    root.mainloop()


if __name__ == "__main__":
    main()

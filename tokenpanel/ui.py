"""System tray app — PySide6.

Levels:
  0  Overview : total tokens, Claude/Codex share, daily chart of the last 30 days, Codex rate limits
  1  Clients  : Claude/Codex × CLI/Desktop/VS Code/ACP, models used
     (or, from the chart, the daily table)
  2  Threads  : threads of the selected client
  3  Thread   : breakdown by model, token type and "spent on"
"""

from __future__ import annotations

import getpass
import math
import sys
import time
from dataclasses import dataclass
from datetime import datetime

from PySide6.QtCore import QMetaObject, QObject, QPoint, QRect, QSettings, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QColor, QCursor, QFont, QGuiApplication, QIcon, QPainter, QPainterPath, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSystemTrayIcon,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from . import autostart, fmt
from .limits import KIND_LABELS, ClaudeLimits
from .model import DEFAULT_METRIC, METRICS, SOURCE_LABELS, Usage
from .paths import wsl_distros
from .store import DAILY_DAYS, RANGES, ClientRow, DayRow, Store, Summary, ThreadRow, range_start

REFRESH_MS = 5000
# Limit windows reset and "x min ago" texts age without any log change; re-summarize at least this often.
RESUMMARIZE_S = 60
MAC = sys.platform == "darwin"


# --- Theme ------------------------------------------------------------------------


@dataclass(frozen=True)
class Theme:
    surface: str
    raised: str
    hover: str
    border: str
    text: str
    text2: str
    muted: str
    track: str
    series: dict
    warning: str
    critical: str


LIGHT = Theme(
    surface="#fcfcfb",
    raised="#f4f3f0",
    hover="#ecebe7",
    border="#e1e0db",
    text="#0b0b0b",
    text2="#52514e",
    muted="#7a7974",
    track="#e8e7e3",
    # Categorical slots 2, 1, 3 of the dataviz palette; validated together in stack order, both themes.
    series={"claude": "#eb6834", "codex": "#2a78d6", "opencode": "#1baf7a"},
    warning="#b47800",
    critical="#c62828",
)
DARK = Theme(
    surface="#1a1a19",
    raised="#232322",
    hover="#2d2d2b",
    border="#383835",
    text="#ffffff",
    text2="#c3c2b7",
    muted="#8f8e86",
    track="#33332f",
    series={"claude": "#d95926", "codex": "#3987e5", "opencode": "#199e70"},
    warning="#e0a43a",
    critical="#e66767",
)


def current_theme() -> Theme:
    hints = QGuiApplication.styleHints()
    # Qt.ColorScheme exists from Qt 6.5; older versions fall back to the palette.
    color_scheme = getattr(Qt, "ColorScheme", None)
    if color_scheme is not None and hasattr(hints, "colorScheme"):
        scheme = hints.colorScheme()
        if scheme == color_scheme.Dark:
            return DARK
        if scheme == color_scheme.Light:
            return LIGHT
    return DARK if QGuiApplication.palette().window().color().lightness() < 128 else LIGHT


# --- Background worker -------------------------------------------------------------


class Worker(QObject):
    updated = Signal(object, object)  # (summary for the selected range, summary for today)

    def __init__(self, make_store, metric: str, include_wsl: bool = False):
        super().__init__()
        self._make_store = make_store
        self.store: Store | None = None
        self.range_key = "today"
        self.metric_key = metric
        self.include_wsl = include_wsl
        self.timer: QTimer | None = None
        self.last_emit = 0.0

    @Slot()
    def start(self):
        self.store = self._make_store(self.include_wsl)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(REFRESH_MS)
        self.tick(force=True)

    @Slot()
    def tick(self, force: bool = False):
        if self.store.refresh() or force or time.monotonic() - self.last_emit > RESUMMARIZE_S:
            self._emit()

    @Slot()
    def stop(self):
        # The timer must be stopped and deleted in its own thread.
        if self.timer is not None:
            self.timer.stop()
            self.timer.setParent(None)
            self.timer = None

    @Slot(str)
    def set_range(self, key: str):
        self.range_key = key
        if self.store is not None:
            self._emit()

    @Slot(str)
    def set_metric(self, metric: str):
        self.metric_key = metric
        if self.store is not None:
            self._emit()

    @Slot(bool)
    def set_wsl(self, on: bool):
        # The log directories change, so start over with a fresh store.
        self.include_wsl = on
        if self.store is not None:
            self.store = self._make_store(on)
            self.tick(force=True)

    @Slot()
    def refresh_now(self):
        if self.store is not None:
            self.store.refresh(discover=True)
            self._emit()

    def _emit(self):
        self.last_emit = time.monotonic()
        today = self.store.summarize("today", self.metric_key)
        current = today if self.range_key == "today" else self.store.summarize(self.range_key, self.metric_key)
        self.updated.emit(current, today)


# --- Small widgets -------------------------------------------------------------


class Bar(QWidget):
    """Thin ratio bar. segments: [(value, color)], with a 2px surface gap between them."""

    def __init__(self, theme: Theme, height: int = 6):
        super().__init__()
        self.theme = theme
        self.segments: list[tuple[float, str]] = []
        self.whole = 1.0
        self.setFixedHeight(height)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set(self, segments, whole):
        self.segments, self.whole = segments, max(whole, 1e-9)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        r = h / 2
        track = QPainterPath()
        track.addRoundedRect(0, 0, w, h, r, r)
        p.fillPath(track, QColor(self.theme.track))
        x = 0.0
        for i, (value, color) in enumerate(self.segments):
            seg = w * value / self.whole
            if seg <= 0:
                continue
            gap = 2 if i < len(self.segments) - 1 else 0
            path = QPainterPath()
            path.addRoundedRect(x, 0, max(seg - gap, min(seg, 2)), h, r, r)
            p.fillPath(path, QColor(color))
            x += seg
        p.end()


class Dot(QWidget):
    def __init__(self, color: str, size: int = 8):
        super().__init__()
        self.color = color
        self.setFixedSize(size, size)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QColor(self.color))
        p.setPen(Qt.NoPen)
        p.drawEllipse(self.rect())
        p.end()


def nice_tick(peak: float) -> float:
    """The largest round number (1, 2 or 5 × 10^n) not above peak, for the chart's one gridline."""
    if peak <= 0:
        return 0.0
    mag = 10 ** math.floor(math.log10(peak))
    return max(m * mag for m in (1, 2, 5, 10) if m * mag <= peak)


class DailyChart(QWidget):
    """Stacked columns, one per day, Claude Code below Codex.

    Days outside the selected range are drawn faded, so the chart shows where that range sits. Hovering a
    column shows its numbers; clicking opens the daily table (the chart's table view).
    """

    PLOT_H, AXIS_H, TOP_PAD = 96, 16, 14

    def __init__(self, theme: Theme, days: list[DayRow], metric: str, range_from: float, on_click=None):
        super().__init__()
        # Not "self.metric": that would shadow QWidget.metric(), which QPainter calls, and crash.
        self.theme, self.days, self.metric_key, self.on_click = theme, days, metric, on_click
        self.range_from = range_from
        self.hover = -1
        self.setFixedHeight(self.TOP_PAD + self.PLOT_H + self.AXIS_H)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMouseTracking(True)
        if on_click:
            self.setCursor(Qt.PointingHandCursor)

    def _values(self, d) -> list[tuple[str, float]]:
        return [(k, d.by_source.get(k, Usage()).value(self.metric_key)) for k in SOURCE_LABELS]

    def _slot(self) -> float:
        return self.width() / max(len(self.days), 1)

    def paintEvent(self, _):
        th = self.theme
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        font = p.font()
        font.setPixelSize(10)
        p.setFont(font)
        w = self.width()
        base = self.TOP_PAD + self.PLOT_H
        peak = max((sum(v for _, v in self._values(d)) for d in self.days), default=0)
        scale = self.PLOT_H / (peak * 1.08) if peak > 0 else 0

        # One hairline gridline at a round value, labelled; the baseline below.
        tick = nice_tick(peak)
        tick_y = round(base - tick * scale)
        if tick:
            p.fillRect(QRect(0, tick_y, w, 1), QColor(th.border))
        p.fillRect(QRect(0, base, w, 1), QColor(th.border))

        slot = self._slot()
        bw = min(24.0, slot - 2)
        radius = min(3.0, bw / 2)
        for i, d in enumerate(self.days):
            x = i * slot + (slot - bw) / 2
            if i == self.hover:
                band = QPainterPath()
                band.addRoundedRect(i * slot, self.TOP_PAD - 4, slot, self.PLOT_H + 4, 4, 4)
                p.fillPath(band, QColor(th.hover))
            in_range = datetime.combine(d.day, datetime.min.time()).timestamp() + 86400 > self.range_from
            p.setOpacity(1.0 if in_range else 0.35)
            segs = [(k, v * scale) for k, v in self._values(d) if v > 0]
            y = float(base)
            for j, (k, h) in enumerate(segs):
                top = j == len(segs) - 1
                h_draw = max(h - (0 if top else 2), 1.0)  # 2px surface gap between stacked segments
                path = QPainterPath()
                if top:
                    path.addRoundedRect(x, y - h_draw, bw, h_draw, radius, radius)
                    square = QPainterPath()
                    square.addRect(x, y - min(h_draw, radius), bw, min(h_draw, radius))
                    path = path.united(square)  # square at the baseline end
                else:
                    path.addRect(x, y - h_draw, bw, h_draw)
                p.fillPath(path, QColor(th.series.get(k, th.muted)))
                y -= h
            p.setOpacity(1.0)

        # Labels go on top of the columns, on a surface-colored backing so a tall column can't hide them.
        p.setPen(QColor(th.muted))
        if tick:
            text = fmt.short(tick)
            box = QRect(0, tick_y - 13, p.fontMetrics().horizontalAdvance(text) + 6, 12)
            p.fillRect(box, QColor(th.surface))
            p.drawText(box, Qt.AlignLeft | Qt.AlignBottom, text)
        if self.days:
            first = self.days[0].day
            p.drawText(QRect(0, base + 3, 120, 12), Qt.AlignLeft | Qt.AlignTop, f"{first:%b} {first.day}")
            p.drawText(QRect(w - 120, base + 3, 120, 12), Qt.AlignRight | Qt.AlignTop, "Today")
        p.end()

    def _index_at(self, x: float) -> int:
        i = int(x // self._slot()) if self.width() else -1
        return i if 0 <= i < len(self.days) else -1

    def mouseMoveEvent(self, e):
        i = self._index_at(e.position().x())
        if i != self.hover:
            self.hover = i
            self.update()
        if i >= 0:
            d = self.days[i]
            vals = self._values(d)
            lines = [f"{d.day:%a, %b} {d.day.day}"]
            lines += [f"{SOURCE_LABELS[k]}: {fmt.short(v)}" for k, v in vals]
            lines.append(f"Total: {fmt.short(sum(v for _, v in vals))}")
            QToolTip.showText(e.globalPosition().toPoint(), "\n".join(lines), self)

    def leaveEvent(self, _):
        self.hover = -1
        self.update()

    def mouseReleaseEvent(self, e):
        if self.on_click and e.button() == Qt.LeftButton:
            self.on_click()


def label(text: str, color: str, size: int = 13, bold: bool = False, elide: bool = False) -> QLabel:
    lb = QLabel(text)
    f = lb.font()
    f.setPixelSize(size)
    f.setBold(bold)
    lb.setFont(f)
    lb.setStyleSheet(f"color: {color}; background: transparent;")
    if elide:
        lb.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        lb.setMinimumWidth(10)
    return lb


class Row(QFrame):
    """Clickable row: title + subtitle + value + ratio bar."""

    def __init__(self, theme, title, subtitle, value, bar_segments, whole, tooltip="", on_click=None, dot=None):
        super().__init__()
        self.on_click = on_click
        self.setObjectName("row")
        self.setCursor(Qt.PointingHandCursor if on_click else Qt.ArrowCursor)
        hover = f"QFrame#row:hover {{ background: {theme.hover}; }}" if on_click else ""
        self.setStyleSheet(f"QFrame#row {{ border-radius: 8px; background: transparent; }} {hover}")
        if tooltip:
            self.setToolTip(tooltip)

        v = QVBoxLayout(self)
        v.setContentsMargins(10, 8, 10, 8)
        v.setSpacing(4)
        top = QHBoxLayout()
        top.setSpacing(8)
        if dot:
            top.addWidget(Dot(dot), 0, Qt.AlignVCenter)
        t = label(title, theme.text, 13, True, elide=True)
        t.setText(t.fontMetrics().elidedText(title, Qt.ElideRight, 230 if dot else 245))
        top.addWidget(t, 1)
        top.addWidget(label(value, theme.text, 13, True), 0, Qt.AlignRight)
        if on_click:
            top.addWidget(label("›", theme.muted, 15), 0)
        v.addLayout(top)
        if subtitle:
            s = label("", theme.text2, 11, elide=True)
            s.setText(s.fontMetrics().elidedText(subtitle, Qt.ElideRight, 300))
            v.addWidget(s)
        if bar_segments is not None:
            b = Bar(theme, 4)
            b.set(bar_segments, whole)
            v.addWidget(b)

    def mouseReleaseEvent(self, e):
        if self.on_click and e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self.on_click()


def usage_tooltip(u: Usage) -> str:
    return (
        f"Raw total: {fmt.full(u.total)}\n"
        f"New input: {fmt.full(u.input)}\n"
        f"Cache reads: {fmt.full(u.cache_read)}\n"
        f"Cache writes: {fmt.full(u.cache_write)}\n"
        f"Output: {fmt.full(u.output)} (thinking: {fmt.full(u.reasoning)})\n"
        f"{cost_text(u)}"
    )


def cost_text(u: Usage) -> str:
    """'API cost ≈ $12.40', with a note when some tokens have no known price."""
    text = f"API cost ≈ {fmt.money(u.cost)}"
    if u.unpriced:
        text += f" (+ {fmt.short(u.unpriced)} tokens of models without a known price)"
    return text


COST_HINT = (
    "Estimated: what these model calls would cost at pay-as-you-go API prices, with cache reads and "
    "writes at their own rates. Subscriptions are billed differently. Prices can be overridden in "
    "prices.json (see the README)."
)


def stat_row(theme: Theme, u: Usage, metric: str) -> QHBoxLayout:
    """Input · Output · API cost, as three small figures."""
    inp, out = u.split(metric)
    row = QHBoxLayout()
    row.setContentsMargins(10, 0, 10, 0)
    row.setSpacing(6)
    cost = fmt.money(u.cost) + ("+" if u.unpriced else "")
    for name, value, tip in (
        ("Input", fmt.short(inp), f"{fmt.full(inp)} input tokens under this metric"),
        ("Output", fmt.short(out), f"{fmt.full(out)} output tokens (thinking included)"),
        ("API cost", cost, cost_text(u) + "\n\n" + COST_HINT),
    ):
        box = QFrame()
        box.setObjectName("stat")
        box.setStyleSheet(f"QFrame#stat {{ background: {theme.raised}; border-radius: 8px; }}")
        box.setToolTip(tip)
        v = QVBoxLayout(box)
        v.setContentsMargins(10, 6, 10, 6)
        v.setSpacing(1)
        v.addWidget(label(name, theme.muted, 10))
        v.addWidget(label(value, theme.text, 14, True))
        row.addWidget(box, 1)
    return row


def models_text(models: dict[str, Usage], metric: str) -> str:
    return ", ".join(m for m, _ in sorted(models.items(), key=lambda x: -x[1].value(metric)))


# --- Main window ------------------------------------------------------------------


class Panel(QWidget):
    range_changed = Signal(str)
    metric_changed = Signal(str)
    refresh_requested = Signal()

    WIDTH, HEIGHT = 380, 560

    def __init__(self, metric: str = DEFAULT_METRIC):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.metric_key = metric
        self.setWindowTitle("Token Panel")
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(self.WIDTH, self.HEIGHT)
        self.theme = current_theme()
        self.summary: Summary | None = None
        self.range_key = "today"
        # Navigation state is kept as keys, so a data refresh redraws the same page.
        self.level = 0
        self.sel_client: tuple[str, str] | None = None
        self.sel_thread: str | None = None
        self.daily_view = False  # level 1 shows the daily table instead of the clients
        self.hidden_at = 0.0
        # Pages are rebuilt on every refresh; the menu is a persistent object owned by the panel.
        self._metric_menu = QMenu(self)
        self._build()

    # Setup
    def _build(self):
        th = self.theme
        self.frame = QFrame(self)
        self.frame.setObjectName("frame")
        self.frame.setGeometry(0, 0, self.WIDTH, self.HEIGHT)
        self.frame.setStyleSheet(
            f"""
            QFrame#frame {{ background: {th.surface}; border: 1px solid {th.border}; border-radius: 12px; }}
            QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; border: none; }}
            QScrollBar:vertical {{ width: 6px; background: transparent; }}
            QScrollBar::handle:vertical {{ background: {th.border}; border-radius: 3px; min-height: 30px; }}
            QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
            QToolTip {{ color: {th.text}; background: {th.raised}; border: 1px solid {th.border}; padding: 4px; }}
            """
        )
        root = QVBoxLayout(self.frame)
        root.setContentsMargins(14, 12, 14, 10)
        root.setSpacing(10)

        # Title + range selector
        head = QHBoxLayout()
        head.addWidget(label("Token Panel", th.text, 14, True))
        head.addStretch(1)
        self.range_group = QButtonGroup(self)
        seg_style = (
            f"QPushButton {{ color: {th.text2}; background: transparent; border: none; padding: 3px 7px;"
            f" border-radius: 6px; font-size: 11px; }}"
            f"QPushButton:hover {{ background: {th.hover}; }}"
            f"QPushButton:checked {{ color: {th.text}; background: {th.raised}; font-weight: bold; }}"
        )
        for key, text in (("today", "Today"), ("7d", "7d"), ("30d", "30d"), ("all", "All")):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setChecked(key == self.range_key)
            b.setStyleSheet(seg_style)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, k=key: self._set_range(k))
            self.range_group.addButton(b)
            head.addWidget(b)
        root.addLayout(head)

        # Back / breadcrumb row
        crumb = QHBoxLayout()
        self.back = QPushButton("‹ Back")
        self.back.setCursor(Qt.PointingHandCursor)
        self.back.setStyleSheet(
            f"QPushButton {{ color: {th.text2}; background: transparent; border: none; font-size: 12px;"
            f" padding: 2px 0; }}"
            f"QPushButton:hover {{ color: {th.text}; }}"
        )
        self.back.clicked.connect(self.go_back)
        self.crumb = label("", th.muted, 12, elide=True)
        crumb.addWidget(self.back)
        crumb.addWidget(self.crumb, 1)
        self.crumb_row = QWidget()
        self.crumb_row.setLayout(crumb)
        crumb.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self.crumb_row)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        root.addWidget(self.scroll, 1)

        # Footer
        foot = QHBoxLayout()
        self.status = label("Reading logs…", th.muted, 11)
        foot.addWidget(self.status, 1)
        refresh = QPushButton("Refresh")
        refresh.setCursor(Qt.PointingHandCursor)
        refresh.setStyleSheet(
            f"QPushButton {{ color: {th.text2}; background: transparent; border: none; font-size: 11px; }}"
            f"QPushButton:hover {{ color: {th.text}; }}"
        )
        refresh.clicked.connect(self.refresh_requested.emit)
        foot.addWidget(refresh)
        root.addLayout(foot)
        self.render()

    # Events
    def _set_range(self, key):
        self.range_key = key
        self.range_changed.emit(key)

    def _set_metric(self, key):
        self.metric_key = key
        self.metric_changed.emit(key)

    def v(self, u: Usage) -> float:
        return u.value(self.summary.metric if self.summary else self.metric_key)

    def set_summary(self, s: Summary):
        self.summary = s
        self.render()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.hide()
        elif e.key() == Qt.Key_Backspace and self.level > 0:
            self.go_back()

    def changeEvent(self, e):
        # Close like a popup when focus is lost (clicking elsewhere).
        if e.type() == e.Type.ActivationChange and not self.isActiveWindow() and self.isVisible():
            QTimer.singleShot(150, self._hide_if_inactive)
        super().changeEvent(e)

    def _hide_if_inactive(self):
        if not self.isActiveWindow() and not QApplication.activePopupWidget():
            self.hide()

    def hideEvent(self, e):
        self.hidden_at = time.monotonic()
        super().hideEvent(e)

    def open(self, level: int, client=None, thread=None):
        self.level = level
        if level == 1:
            self.daily_view = False
        if client is not None:
            self.sel_client = client
        if thread is not None:
            self.sel_thread = thread
        self.render(reset_scroll=True)

    def open_daily(self):
        self.level = 1
        self.daily_view = True
        self.render(reset_scroll=True)

    def go_back(self):
        self.level = max(0, self.level - 1)
        if self.level == 0:
            self.daily_view = False
        self.render(reset_scroll=True)

    # Rendering
    def render(self, reset_scroll=False):
        pos = self.scroll.verticalScrollBar().value()
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 4, 0)
        lay.setSpacing(2)
        s = self.summary
        if s is None:
            lay.addWidget(label("Reading logs…", self.theme.text2, 13))
            self.crumb_row.hide()
        else:
            client = self._find_client(s)
            thread = self._find_thread(client)
            if self.level >= 2 and client is None:
                self.level = 1
            if self.level >= 3 and thread is None:
                self.level = 2
            self.crumb_row.setVisible(self.level > 0)
            pages = [self._page_summary, self._page_clients, self._page_threads, self._page_thread]
            if self.daily_view and self.level == 1:
                pages[1] = self._page_daily
            pages[self.level](lay, s, client, thread)
            self.status.setText(
                f"Updated {fmt.clock(s.generated_at)} · {s.file_count} log files"
            )
        lay.addStretch(1)
        # Delete the old page later so it is not destroyed while its click is still being handled.
        old = self.scroll.takeWidget()
        self.scroll.setWidget(page)
        if old is not None:
            old.deleteLater()
        if not reset_scroll:
            QTimer.singleShot(0, lambda: self.scroll.verticalScrollBar().setValue(pos))

    def _find_client(self, s: Summary) -> ClientRow | None:
        for c in s.clients:
            if (c.source, c.client) == self.sel_client:
                return c
        return None

    def _find_thread(self, c: ClientRow | None) -> ThreadRow | None:
        if c is None:
            return None
        for t in c.threads:
            if t.thread_id == self.sel_thread:
                return t
        return None

    def _section(self, lay, text, color=None, value=""):
        lay.addSpacing(10)
        h = QHBoxLayout()
        h.setContentsMargins(10, 0, 10, 2)
        if color:
            h.addWidget(Dot(color), 0, Qt.AlignVCenter)
        h.addWidget(label(text, self.theme.text2, 11, True), 1)
        if value:
            h.addWidget(label(value, self.theme.text2, 11, True))
        lay.addLayout(h)

    # Level 0: overview
    def _page_summary(self, lay, s: Summary, *_):
        th = self.theme
        lay.addSpacing(14)
        v = self.v
        hero = label(fmt.short(v(s.total)), th.text, 46, True)
        hero.setAlignment(Qt.AlignHCenter)
        hero.setToolTip(usage_tooltip(s.total))
        lay.addWidget(hero)
        sub = label(f"tokens · {RANGES[s.range_key].lower()}", th.text2, 13)
        sub.setAlignment(Qt.AlignHCenter)
        lay.addWidget(sub)
        exact = label(fmt.full(v(s.total)), th.muted, 11)
        exact.setAlignment(Qt.AlignHCenter)
        lay.addWidget(exact)
        lay.addWidget(self._metric_button(s), 0, Qt.AlignHCenter)
        lay.addSpacing(8)
        lay.addLayout(stat_row(th, s.total, s.metric))
        lay.addSpacing(14)

        # Claude / Codex share
        srcs = [(k, s.by_source.get(k, Usage())) for k in SOURCE_LABELS]
        bar = Bar(th, 8)
        bar.set([(v(u), th.series[k]) for k, u in srcs], v(s.total) or 1)
        wrap = QHBoxLayout()
        wrap.setContentsMargins(10, 0, 10, 0)
        wrap.addWidget(bar)
        lay.addLayout(wrap)
        lay.addSpacing(6)
        for k, u in srcs:
            row = QHBoxLayout()
            row.setContentsMargins(10, 2, 10, 2)
            row.addWidget(Dot(th.series[k]), 0, Qt.AlignVCenter)
            row.addWidget(label(SOURCE_LABELS[k], th.text, 13), 1)
            row.addWidget(label(fmt.percent(v(u), v(s.total)), th.muted, 12))
            val = label(fmt.short(v(u)), th.text, 13, True)
            val.setMinimumWidth(56)
            val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            val.setToolTip(usage_tooltip(u))
            row.addWidget(val)
            lay.addLayout(row)

        if s.total.total:
            if s.metric == "raw":
                text = f"{fmt.percent(s.total.cache_read, s.total.total)} of this is input read from the cache."
            else:
                text = (
                    f"{fmt.short(s.total.cache_read)} tokens re-read from the cache are not counted "
                    f"(raw total {fmt.short(s.total.total)})."
                )
            note = label(text, th.muted, 11)
            note.setContentsMargins(10, 6, 10, 0)
            note.setWordWrap(True)
            lay.addWidget(note)

        if s.daily:
            self._section(lay, f"Daily · last {DAILY_DAYS} days")
            chart = DailyChart(th, s.daily, s.metric, range_start(s.range_key), on_click=self.open_daily)
            box = QHBoxLayout()
            box.setContentsMargins(10, 2, 10, 0)
            box.addWidget(chart)
            lay.addLayout(box)

        self._claude_limits(lay, s.claude_limits)
        self._limits(lay, s)

        lay.addSpacing(14)
        btn = QPushButton("Details  ›")
        btn.setCursor(Qt.PointingHandCursor)
        btn.setStyleSheet(
            f"QPushButton {{ color: {th.text}; background: {th.raised}; border: 1px solid {th.border};"
            f" border-radius: 8px; padding: 9px; font-size: 13px; font-weight: bold; }}"
            f"QPushButton:hover {{ background: {th.hover}; }}"
        )
        btn.clicked.connect(lambda: self.open(1))
        lay.addWidget(btn)

    def _metric_button(self, s: Summary) -> QPushButton:
        """Shows which tokens are counted; click to pick the metric."""
        th = self.theme
        btn = QPushButton(f"Metric: {METRICS[s.metric].lower()}  ▾")
        btn.setCursor(Qt.PointingHandCursor)
        btn.setStyleSheet(
            f"QPushButton {{ color: {th.text2}; background: transparent; border: none; font-size: 11px;"
            f" padding: 4px 8px; border-radius: 6px; }}"
            f"QPushButton:hover {{ background: {th.hover}; color: {th.text}; }}"
            "QPushButton::menu-indicator { image: none; width: 0; }"
        )
        menu = self._metric_menu
        menu.clear()
        hints = {
            "app": "Default. Claude: same method as 'Total tokens' in the Claude app, which counts every "
            "transcript line of a response split across lines. Codex: same as Codex's own counter "
            "(conversation compaction calls are not counted).",
            "io": "Every model call counted once, compaction calls included: actual spend.",
            "new": "Also counts context written to the cache for the first time.",
            "raw": "Also counts context re-read from the cache on every call; gets very large.",
        }
        for key, name in METRICS.items():
            act = QAction(name, menu)
            act.setCheckable(True)
            act.setChecked(key == s.metric)
            act.setToolTip(hints[key])
            act.triggered.connect(lambda _=False, k=key: self._set_metric(k))
            menu.addAction(act)
        menu.setToolTipsVisible(True)
        btn.setMenu(menu)
        btn.setToolTip(hints[s.metric])
        return btn

    def _limit_row(self, lay, name: str, state: str, pct: float | None, value: str, tooltip: str):
        th = self.theme
        box = QVBoxLayout()
        box.setContentsMargins(10, 4, 10, 4)
        box.setSpacing(4)
        top = QHBoxLayout()
        top.addWidget(label(name, th.text, 12), 1)
        top.addWidget(label(state, th.muted, 11))
        top.addSpacing(8)
        top.addWidget(label(value, th.text, 12, True))
        box.addLayout(top)
        if pct is not None:
            color = th.critical if pct >= 90 else th.warning if pct >= 75 else th.muted
            b = Bar(th, 6)
            b.set([(min(pct, 100), color)], 100)
            box.addWidget(b)
        holder = QWidget()
        holder.setLayout(box)
        holder.setToolTip(tooltip)
        lay.addWidget(holder)

    def _claude_limits(self, lay, est: ClaudeLimits | None):
        if est is None or not est.windows:
            return
        th = self.theme
        self._section(lay, "Claude usage limits" + ("" if est.official else " (estimate)"))
        if est.blocked_until:
            warn = label(
                f"{KIND_LABELS.get(est.blocked_kind, 'Usage')} limit reached · resets {fmt.clock(est.blocked_until)}",
                th.critical, 12, True,
            )
            warn.setContentsMargins(10, 0, 10, 2)
            lay.addWidget(warn)
        for w in est.windows:
            name = KIND_LABELS.get(w.kind, w.kind)
            if w.kind == "weekly" and not w.exact_end:
                state = "last 7 days"
            else:
                state = f"resets {'' if w.exact_end else '~'}{fmt.clock(w.end)}"
            if est.official:
                value, tip = f"{w.used_pct:.0f}%", "Claude Code's own figure for this window."
            elif w.used_pct is not None:
                value = f"≈{w.used_pct:.0f}%"
                tip = (
                    f"{fmt.money(w.cost)} used at API prices. You reached this limit on "
                    f"{fmt.clock(w.cap_from)} after {fmt.money(w.cap)}, so that amount is taken as the limit."
                )
            else:
                value = f"{fmt.money(w.cost)} used"
                tip = (
                    "Used so far, at API prices. No limit has been reached in the last 30 days, so there is "
                    "nothing to compare against yet; the percentage appears after the first one."
                )
            self._limit_row(lay, name, state, w.used_pct, value, tip)
        if est.official:
            note = f"From Claude Code, {fmt.ago(est.as_of)}"
        else:
            note = "Estimated from this computer's logs. /usage in Claude Code shows the exact figures."
        foot = label(note, th.muted, 10)
        foot.setWordWrap(True)
        foot.setContentsMargins(10, 0, 10, 0)
        lay.addWidget(foot)

    def _limits(self, lay, s: Summary):
        lim = s.limits
        if lim is None or (lim.primary is None and lim.secondary is None):
            return
        th = self.theme
        plan = f" ({lim.plan})" if lim.plan else ""
        self._section(lay, f"Codex usage limits{plan}")
        for name, w in (("5-hour", lim.primary), ("Weekly", lim.secondary)):
            if w is None:
                continue
            reset_passed = w.resets_at and w.resets_at < time.time()
            pct = 0.0 if reset_passed else w.used_percent
            color = th.critical if pct >= 90 else th.warning if pct >= 75 else th.muted
            box = QVBoxLayout()
            box.setContentsMargins(10, 4, 10, 4)
            box.setSpacing(4)
            top = QHBoxLayout()
            top.addWidget(label(name, th.text, 12), 1)
            state = "reset" if reset_passed else f"resets {fmt.clock(w.resets_at)}"
            top.addWidget(label(state, th.muted, 11))
            top.addSpacing(8)
            top.addWidget(label(f"{pct:.0f}%", th.text, 12, True))
            box.addLayout(top)
            b = Bar(th, 6)
            b.set([(pct, color)], 100)
            box.addWidget(b)
            lay.addLayout(box)
        age = label(f"Last reading: {fmt.ago(lim.ts)}", th.muted, 10)
        age.setContentsMargins(10, 0, 10, 0)
        lay.addWidget(age)

    # Level 1: clients
    def _page_clients(self, lay, s: Summary, *_):
        th = self.theme
        self.crumb.setText("Clients")
        if not s.clients:
            lay.addWidget(label("No usage in this range.", th.text2, 13))
            return
        v = self.v
        top = max(v(c.usage) for c in s.clients) or 1
        for src in SOURCE_LABELS:
            rows = [c for c in s.clients if c.source == src]
            if not rows:
                continue
            color = th.series[src]
            self._section(lay, SOURCE_LABELS[src], color, fmt.short(v(s.by_source[src])))
            for c in rows:
                n = len(c.threads)
                lay.addWidget(
                    Row(
                        th,
                        c.label,
                        f"≈ {fmt.money(c.usage.cost)} · {models_text(c.models, s.metric)}"
                        f" · {n} thread{'s' if n != 1 else ''}",
                        fmt.short(v(c.usage)),
                        [(v(c.usage), color)],
                        top,
                        tooltip=usage_tooltip(c.usage),
                        on_click=lambda c=c: self.open(2, client=(c.source, c.client)),
                    )
                )

    # Level 1, from the chart: the daily table
    def _page_daily(self, lay, s: Summary, *_):
        th = self.theme
        self.crumb.setText(f"Daily · last {DAILY_DAYS} days")
        v = self.v
        peak = max((v(d.total) for d in s.daily), default=0) or 1
        today = s.daily[-1].day if s.daily else None
        for d in reversed(s.daily):
            parts = [(k, d.by_source.get(k, Usage())) for k in SOURCE_LABELS]
            title = f"{d.day:%a, %b} {d.day.day}" + (" · today" if d.day == today else "")
            sub = " · ".join(f"{SOURCE_LABELS[k]} {fmt.short(v(u))}" for k, u in parts)
            lay.addWidget(
                Row(th, title, sub, fmt.short(v(d.total)), [(v(u), th.series[k]) for k, u in parts], peak,
                    tooltip=usage_tooltip(d.total))
            )
        note = label("Days follow this computer's local time.", th.muted, 10)
        note.setContentsMargins(10, 4, 10, 0)
        lay.addWidget(note)

    # Level 2: threads of one client
    def _page_threads(self, lay, s, c: ClientRow, _):
        th = self.theme
        self.crumb.setText(f"{SOURCE_LABELS[c.source]} · {c.label}")
        color = th.series[c.source]
        v = self.v
        self._section(lay, f"{len(c.threads)} thread{'s' if len(c.threads) != 1 else ''}", color, fmt.short(v(c.usage)))
        top = (v(c.threads[0].usage) if c.threads else 0) or 1
        for t in c.threads:
            models = models_text(t.model_usage, s.metric)
            lay.addWidget(
                Row(
                    th,
                    t.title,
                    f"≈ {fmt.money(t.usage.cost)} · {t.project} · {models} · {fmt.ago(t.last_ts)}",
                    fmt.short(v(t.usage)),
                    [(v(t.usage), color)],
                    top,
                    tooltip=f"{t.title}\n{t.cwd}\n\n{usage_tooltip(t.usage)}",
                    on_click=lambda t=t: self.open(3, thread=t.thread_id),
                )
            )

    # Level 3: one thread
    def _page_thread(self, lay, s, c: ClientRow, t: ThreadRow):
        th = self.theme
        color = th.series[t.source]
        self.crumb.setText(f"{SOURCE_LABELS[c.source]} · {c.label} · thread")
        title = label(t.title, th.text, 15, True)
        title.setWordWrap(True)
        title.setContentsMargins(10, 4, 10, 0)
        lay.addWidget(title)
        meta = [
            f"Project: {t.project}" + (f"  ·  branch: {t.branch}" if t.branch else ""),
            f"{fmt.clock(t.first_ts)} – {fmt.clock(t.last_ts)}  ·  {t.calls} model calls",
        ]
        if t.efforts:
            meta.append("Effort: " + ", ".join(sorted(t.efforts)))
        for m in meta:
            lb = label(m, th.text2, 11)
            lb.setContentsMargins(10, 2, 10, 0)
            lb.setToolTip(t.cwd)
            lay.addWidget(lb)
        v = self.v
        hero = label(fmt.short(v(t.usage)), th.text, 30, True)
        hero.setContentsMargins(10, 8, 10, 0)
        hero.setToolTip(usage_tooltip(t.usage))
        lay.addWidget(hero)
        lay.addSpacing(4)
        lay.addLayout(stat_row(th, t.usage, s.metric))

        total = v(t.usage) or 1
        self._section(lay, "Models")
        for m, u in sorted(t.model_usage.items(), key=lambda x: -v(x[1])):
            lay.addWidget(
                Row(th, m, f"{fmt.percent(v(u), total)} · ≈ {fmt.money(u.cost)}", fmt.short(v(u)), [(v(u), color)],
                    total, tooltip=usage_tooltip(u))
            )

        self._section(lay, "Token types")
        u = t.usage
        counted = {"app": {"input", "output"}, "io": {"input", "output"}, "new": {"input", "output", "cache_write"}}
        included = counted.get(s.metric, {"input", "output", "cache_write", "cache_read"})
        kinds = [
            ("input", "New input", u.input),
            ("output", "Output", u.output),
            ("cache_write", "Cache writes", u.cache_write),
            ("cache_read", "Cache reads", u.cache_read),
        ]
        for key, name, n in kinds:
            if not n:
                continue
            notes = []
            if key == "output" and u.reasoning:
                notes.append(f"{fmt.short(u.reasoning)} of it thinking")
            if key not in included:
                notes.append("not counted in this total")
            lay.addWidget(
                Row(th, name, " · ".join(notes), fmt.short(n), [(n, color if key in included else th.muted)],
                    u.total or 1, tooltip=fmt.full(n))
            )

        self._section(lay, "Spent on (approximate)")
        for name, tu in sorted(t.tools.items(), key=lambda x: -v(x[1]))[:12]:
            lay.addWidget(
                Row(th, name, fmt.percent(v(tu), total), fmt.short(v(tu)), [(v(tu), color)], total,
                    tooltip=usage_tooltip(tu))
            )
        note = label(
            "Each model call's tokens are split evenly across the tools used in that call.",
            th.muted,
            10,
        )
        note.setWordWrap(True)
        note.setContentsMargins(10, 4, 10, 0)
        lay.addWidget(note)

    # Positioning
    def place(self, tray_geo: QRect | None):
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        anchor = tray_geo.center() if tray_geo and tray_geo.isValid() and not tray_geo.isEmpty() else QCursor.pos()
        x = min(max(anchor.x() - self.WIDTH // 2, area.left() + 8), area.right() - self.WIDTH - 8)
        if anchor.y() < area.center().y():
            y = area.top() + 8
        else:
            y = area.bottom() - self.HEIGHT - 8
        self.move(QPoint(x, y))


# --- Tray icon --------------------------------------------------------------------


def make_icon(color: str = "#2a78d6") -> QIcon:
    icon = QIcon()
    for size in (16, 22, 24, 32, 48, 64):
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        bg = QPainterPath()
        bg.addRoundedRect(0, 0, size, size, size * 0.22, size * 0.22)
        p.fillPath(bg, QColor(color))
        bw = size * 0.16
        gap = size * 0.08
        x0 = (size - (3 * bw + 2 * gap)) / 2
        base = size * 0.78
        for i, frac in enumerate((0.35, 0.6, 0.45)):
            h = size * frac
            r = QPainterPath()
            r.addRoundedRect(x0 + i * (bw + gap), base - h, bw, h, bw / 3, bw / 3)
            p.fillPath(r, QColor("#ffffff"))
        p.end()
        icon.addPixmap(pm)
    return icon


def make_menubar_icon() -> QIcon:
    """macOS menu bar icon: black bars on transparent, marked as a template so macOS tints it for the
    light or dark menu bar."""
    icon = QIcon()
    for size in (16, 18, 32, 36):
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        bw = size * 0.2
        gap = size * 0.1
        x0 = (size - (3 * bw + 2 * gap)) / 2
        base = size * 0.88
        for i, frac in enumerate((0.45, 0.76, 0.58)):
            h = size * frac
            r = QPainterPath()
            r.addRoundedRect(x0 + i * (bw + gap), base - h, bw, h, bw / 3, bw / 3)
            p.fillPath(r, QColor("#000000"))
        p.end()
        icon.addPixmap(pm)
    icon.setIsMask(True)
    return icon


class App(QObject):
    request_range = Signal(str)
    request_refresh = Signal()
    request_metric = Signal(str)
    request_wsl = Signal(bool)

    def __init__(self, make_store, show: bool):
        super().__init__()
        self.settings = QSettings("tokenpanel", "tokenpanel")
        metric = self.settings.value("metric", DEFAULT_METRIC)
        if metric not in METRICS:
            metric = DEFAULT_METRIC
        has_wsl = bool(wsl_distros())
        include_wsl = has_wsl and self.settings.value("wsl", False, type=bool)
        self.panel = Panel(metric)
        self.tray_ok = QSystemTrayIcon.isSystemTrayAvailable()

        self.thread = QThread()
        self.worker = Worker(make_store, metric, include_wsl)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.start)
        self.worker.updated.connect(self.on_update)
        self.request_range.connect(self.worker.set_range)
        self.request_refresh.connect(self.worker.refresh_now)
        self.request_metric.connect(self.worker.set_metric)
        self.request_wsl.connect(self.worker.set_wsl)
        self.panel.range_changed.connect(self.request_range.emit)
        self.panel.metric_changed.connect(self.on_metric)
        self.panel.refresh_requested.connect(self.request_refresh.emit)
        self.thread.start()

        if self.tray_ok:
            self.tray = QSystemTrayIcon(make_menubar_icon() if MAC else make_icon(), self)
            self.tray.setToolTip("Token Panel — loading")
            menu = QMenu()
            act_open = QAction("Open panel", menu)
            act_open.triggered.connect(self.show_panel)
            act_refresh = QAction("Refresh", menu)
            act_refresh.triggered.connect(self.request_refresh.emit)
            act_quit = QAction("Quit", menu)
            act_quit.triggered.connect(self.quit)
            menu.addActions([act_open, act_refresh])
            menu.addSeparator()
            if autostart.supported():
                autostart.sync()
                act_auto = QAction(autostart.label(), menu)
                act_auto.setCheckable(True)
                act_auto.setChecked(autostart.enabled())
                act_auto.toggled.connect(autostart.set_enabled)
                menu.addAction(act_auto)
            if has_wsl:
                act_wsl = QAction("Include WSL logs", menu)
                act_wsl.setCheckable(True)
                act_wsl.setChecked(include_wsl)
                act_wsl.toggled.connect(self.on_wsl)
                menu.addAction(act_wsl)
            if not menu.actions()[-1].isSeparator():
                menu.addSeparator()
            menu.addAction(act_quit)
            self.menu = menu
            if not MAC:
                # On macOS a context menu would open on every click, so it is shown from on_tray instead.
                self.tray.setContextMenu(menu)
            self.tray.activated.connect(self.on_tray)
            self.tray.show()
        else:
            # Without a tray (e.g. GNOME without the extension) run as a normal window.
            self.panel.setWindowFlags(Qt.Window)
            self.panel.setAttribute(Qt.WA_TranslucentBackground, False)
            self.panel.changeEvent = lambda e: QWidget.changeEvent(self.panel, e)
            QApplication.instance().setQuitOnLastWindowClosed(True)
            show = True

        if show:
            self.show_panel()

    def on_tray(self, reason):
        if MAC and reason == QSystemTrayIcon.Context:  # right click or control-click
            self.panel.hide()
            self.menu.popup(QCursor.pos())
            return
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.MiddleClick):
            if self.panel.isVisible():
                self.panel.hide()
            elif time.monotonic() - self.panel.hidden_at > 0.3:  # just closed by losing focus; don't reopen
                self.show_panel()

    def show_panel(self):
        if self.tray_ok:
            self.panel.place(self.tray.geometry())
        self.panel.show()
        self.panel.raise_()
        self.panel.activateWindow()

    def on_metric(self, metric: str):
        self.settings.setValue("metric", metric)
        self.request_metric.emit(metric)

    def on_wsl(self, on: bool):
        self.settings.setValue("wsl", on)
        self.request_wsl.emit(on)

    @Slot(object, object)
    def on_update(self, current: Summary, today: Summary):
        self.panel.set_summary(current)
        if self.tray_ok:
            m = today.metric
            parts = " · ".join(f"{SOURCE_LABELS[k]} {fmt.short(u.value(m))}" for k, u in today.by_source.items())
            self.tray.setToolTip(f"Today: {fmt.short(today.total.value(m))} tokens\n{parts}")

    def shutdown(self):
        if self.thread.isRunning():
            QMetaObject.invokeMethod(self.worker, "stop", Qt.BlockingQueuedConnection)
            self.thread.quit()
            self.thread.wait(2000)

    def quit(self):
        self.shutdown()
        QApplication.quit()


def _instance_name() -> str:
    try:
        user = getpass.getuser()
    except Exception:  # no user name in the environment
        user = "user"
    return f"tokenpanel-{user}"


def _show_running_instance(name: str) -> bool:
    """If Token Panel is already running, asks it to open its panel and returns True."""
    sock = QLocalSocket()
    sock.connectToServer(name)
    if not sock.waitForConnected(500):
        return False
    sock.write(b"show")
    sock.waitForBytesWritten(500)
    sock.disconnectFromServer()
    return True


class SelfTest(QObject):
    """--self-test: renders every panel level once the first data arrives, then exits (used by CI)."""

    def __init__(self, app: QApplication, main: App):
        super().__init__()
        self.app, self.main = app, main
        self.done = False
        main.worker.updated.connect(self.check)
        # The worker may have sent its first update before this connection existed; ask for a fresh one.
        main.request_refresh.emit()
        QTimer.singleShot(60000, lambda: self.finish(3, "timed out"))

    @Slot(object, object)
    def check(self, current: Summary, today: Summary):
        if self.done:
            return
        try:
            p = self.main.panel
            p.grab()
            p.open_daily()
            p.grab()
            p.go_back()
            p.open(1)
            p.grab()
            levels = 2
            if current.clients:
                c = current.clients[0]
                p.open(2, client=(c.source, c.client))
                p.grab()
                levels += 1
                if c.threads:
                    p.open(3, thread=c.threads[0].thread_id)
                    p.grab()
                    levels += 1
            platform = self.app.platformName()
            self.finish(0, f"self-test ok: {levels} levels, {current.file_count} files, platform {platform}")
        except Exception as e:  # report instead of letting Qt swallow it
            self.finish(2, f"self-test failed: {e!r}")

    def finish(self, code: int, message: str):
        if self.done:
            return
        self.done = True
        print(message, flush=True)
        self.main.shutdown()
        self.app.exit(code)


def run(make_store, show: bool = False, self_test: bool = False) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("tokenpanel")
    app.setApplicationDisplayName("Token Panel")
    app.setQuitOnLastWindowClosed(False)
    app.setWindowIcon(make_icon())
    font = QFont(app.font())
    app.setFont(font)

    server = None
    if not self_test:
        name = _instance_name()
        if _show_running_instance(name):
            return 0
        QLocalServer.removeServer(name)  # left behind if a previous run crashed
        server = QLocalServer(app)
        server.listen(name)

    main = App(make_store, show or self_test)
    if server is not None:

        def on_connection():
            conn = server.nextPendingConnection()
            if conn is not None:
                conn.disconnected.connect(conn.deleteLater)
            main.show_panel()

        server.newConnection.connect(on_connection)
    tester = SelfTest(app, main) if self_test else None
    code = app.exec()
    main.shutdown()
    del tester
    return code

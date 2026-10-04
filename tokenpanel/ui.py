"""Menü çubuğu (system tray) uygulaması — PySide6.

Seviyeler:
  0  Özet       : toplam token, Claude/Codex payı, Codex limitleri
  1  İstemciler : Claude/Codex × CLI/Desktop/VS Code/ACP, kullanılan modeller
  2  Thread'ler : seçilen istemcideki thread'ler
  3  Thread     : model, token türü ve "ne üzerinde" dökümü
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass

from PySide6.QtCore import QObject, QPoint, QRect, QSize, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QColor, QCursor, QFont, QGuiApplication, QIcon, QPainter, QPainterPath, QPixmap
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
    QStackedWidget,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from . import fmt
from .model import SOURCE_LABELS, Usage
from .store import RANGES, ClientRow, Store, Summary, ThreadRow

REFRESH_MS = 5000


# --- Tema -----------------------------------------------------------------------


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
    series={"claude": "#2a78d6", "codex": "#eb6834"},
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
    series={"claude": "#3987e5", "codex": "#d95926"},
    warning="#e0a43a",
    critical="#e66767",
)


def current_theme() -> Theme:
    hints = QGuiApplication.styleHints()
    scheme = getattr(hints, "colorScheme", lambda: None)()
    if scheme == Qt.ColorScheme.Dark:
        return DARK
    if scheme == Qt.ColorScheme.Light:
        return LIGHT
    return DARK if QGuiApplication.palette().window().color().lightness() < 128 else LIGHT


# --- Arka plan işçisi -------------------------------------------------------------


class Worker(QObject):
    updated = Signal(object, object)  # (seçili aralık özeti, bugünün özeti)

    def __init__(self, make_store):
        super().__init__()
        self._make_store = make_store
        self.store: Store | None = None
        self.range_key = "today"
        self.timer: QTimer | None = None

    @Slot()
    def start(self):
        self.store = self._make_store()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(REFRESH_MS)
        self.tick(force=True)

    @Slot()
    def tick(self, force: bool = False):
        if self.store.refresh() or force:
            self._emit()

    @Slot(str)
    def set_range(self, key: str):
        self.range_key = key
        if self.store is not None:
            self._emit()

    @Slot()
    def refresh_now(self):
        if self.store is not None:
            self.store.refresh()
            self._emit()

    def _emit(self):
        today = self.store.summarize("today")
        current = today if self.range_key == "today" else self.store.summarize(self.range_key)
        self.updated.emit(current, today)


# --- Küçük bileşenler -------------------------------------------------------------


class Bar(QWidget):
    """İnce oran çubuğu. segments: [(değer, renk)]; aralarında 2px yüzey boşluğu."""

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
    """Tıklanabilir satır: başlık + alt satır + değer + oran çubuğu."""

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
        f"Toplam: {fmt.full(u.total)}\n"
        f"Yeni girdi: {fmt.full(u.input)}\n"
        f"Önbellekten okunan: {fmt.full(u.cache_read)}\n"
        f"Önbelleğe yazılan: {fmt.full(u.cache_write)}\n"
        f"Çıktı: {fmt.full(u.output)} (düşünme: {fmt.full(u.reasoning)})"
    )


def models_text(models: dict[str, int]) -> str:
    return ", ".join(m for m, _ in sorted(models.items(), key=lambda x: -x[1]))


# --- Ana pencere -----------------------------------------------------------------


class Panel(QWidget):
    range_changed = Signal(str)
    refresh_requested = Signal()

    WIDTH, HEIGHT = 380, 560

    def __init__(self):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setWindowTitle("Token Paneli")
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(self.WIDTH, self.HEIGHT)
        self.theme = current_theme()
        self.summary: Summary | None = None
        self.range_key = "today"
        # Gezinme durumu: anahtarlar üzerinden tutulur, veri yenilenince aynı sayfa yeniden çizilir.
        self.level = 0
        self.sel_client: tuple[str, str] | None = None
        self.sel_thread: str | None = None
        self.hidden_at = 0.0
        self._build()

    # Kurulum
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

        # Başlık + aralık seçici
        head = QHBoxLayout()
        head.addWidget(label("Token Paneli", th.text, 14, True))
        head.addStretch(1)
        self.range_group = QButtonGroup(self)
        seg_style = (
            f"QPushButton {{ color: {th.text2}; background: transparent; border: none; padding: 3px 7px;"
            f" border-radius: 6px; font-size: 11px; }}"
            f"QPushButton:hover {{ background: {th.hover}; }}"
            f"QPushButton:checked {{ color: {th.text}; background: {th.raised}; font-weight: bold; }}"
        )
        for key, text in (("today", "Bugün"), ("7d", "7 gün"), ("30d", "30 gün"), ("all", "Tümü")):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setChecked(key == self.range_key)
            b.setStyleSheet(seg_style)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, k=key: self._set_range(k))
            self.range_group.addButton(b)
            head.addWidget(b)
        root.addLayout(head)

        # Geri / konum satırı
        crumb = QHBoxLayout()
        self.back = QPushButton("‹ Geri")
        self.back.setCursor(Qt.PointingHandCursor)
        self.back.setStyleSheet(
            f"QPushButton {{ color: {th.text2}; background: transparent; border: none; font-size: 12px; padding: 2px 0; }}"
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

        # Alt bilgi
        foot = QHBoxLayout()
        self.status = label("Loglar okunuyor…", th.muted, 11)
        foot.addWidget(self.status, 1)
        refresh = QPushButton("Yenile")
        refresh.setCursor(Qt.PointingHandCursor)
        refresh.setStyleSheet(
            f"QPushButton {{ color: {th.text2}; background: transparent; border: none; font-size: 11px; }}"
            f"QPushButton:hover {{ color: {th.text}; }}"
        )
        refresh.clicked.connect(self.refresh_requested.emit)
        foot.addWidget(refresh)
        root.addLayout(foot)
        self.render()

    # Olaylar
    def _set_range(self, key):
        self.range_key = key
        self.range_changed.emit(key)

    def set_summary(self, s: Summary):
        self.summary = s
        self.render()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.hide()
        elif e.key() == Qt.Key_Backspace and self.level > 0:
            self.go_back()

    def changeEvent(self, e):
        # Odak kaybolunca (başka yere tıklanınca) açılır panel gibi kapan.
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
        if client is not None:
            self.sel_client = client
        if thread is not None:
            self.sel_thread = thread
        self.render(reset_scroll=True)

    def go_back(self):
        self.level = max(0, self.level - 1)
        self.render(reset_scroll=True)

    # Çizim
    def render(self, reset_scroll=False):
        pos = self.scroll.verticalScrollBar().value()
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 4, 0)
        lay.setSpacing(2)
        s = self.summary
        if s is None:
            lay.addWidget(label("Loglar okunuyor…", self.theme.text2, 13))
            self.crumb_row.hide()
        else:
            client = self._find_client(s)
            thread = self._find_thread(client)
            if self.level >= 2 and client is None:
                self.level = 1
            if self.level >= 3 and thread is None:
                self.level = 2
            self.crumb_row.setVisible(self.level > 0)
            [self._page_summary, self._page_clients, self._page_threads, self._page_thread][self.level](
                lay, s, client, thread
            )
            self.status.setText(
                f"Güncellendi {fmt.clock(s.generated_at)} · {s.file_count} log dosyası"
            )
        lay.addStretch(1)
        self.scroll.setWidget(page)
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

    # Seviye 0
    def _page_summary(self, lay, s: Summary, *_):
        th = self.theme
        lay.addSpacing(14)
        hero = label(fmt.short(s.total.total), th.text, 46, True)
        hero.setAlignment(Qt.AlignHCenter)
        hero.setToolTip(usage_tooltip(s.total))
        lay.addWidget(hero)
        sub = label(f"token · {RANGES[s.range_key].lower()}", th.text2, 13)
        sub.setAlignment(Qt.AlignHCenter)
        lay.addWidget(sub)
        exact = label(fmt.full(s.total.total), th.muted, 11)
        exact.setAlignment(Qt.AlignHCenter)
        lay.addWidget(exact)
        lay.addSpacing(16)

        # Claude / Codex payı
        srcs = [(k, s.by_source.get(k, Usage())) for k in SOURCE_LABELS]
        bar = Bar(th, 8)
        bar.set([(u.total, th.series[k]) for k, u in srcs], s.total.total or 1)
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
            row.addWidget(label(fmt.percent(u.total, s.total.total), th.muted, 12))
            v = label(fmt.short(u.total), th.text, 13, True)
            v.setMinimumWidth(56)
            v.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            v.setToolTip(usage_tooltip(u))
            row.addWidget(v)
            lay.addLayout(row)

        if s.total.total:
            cached = s.total.cache_read
            note = label(
                f"Bunun {fmt.percent(cached, s.total.total)}'i önbellekten okunan girdi.",
                th.muted,
                11,
            )
            note.setContentsMargins(10, 6, 10, 0)
            note.setWordWrap(True)
            lay.addWidget(note)

        self._limits(lay, s)

        lay.addSpacing(14)
        btn = QPushButton("Detaylar  ›")
        btn.setCursor(Qt.PointingHandCursor)
        btn.setStyleSheet(
            f"QPushButton {{ color: {th.text}; background: {th.raised}; border: 1px solid {th.border};"
            f" border-radius: 8px; padding: 9px; font-size: 13px; font-weight: bold; }}"
            f"QPushButton:hover {{ background: {th.hover}; }}"
        )
        btn.clicked.connect(lambda: self.open(1))
        lay.addWidget(btn)

    def _limits(self, lay, s: Summary):
        lim = s.limits
        if lim is None or (lim.primary is None and lim.secondary is None):
            return
        th = self.theme
        plan = f" ({lim.plan})" if lim.plan else ""
        self._section(lay, f"Codex kullanım limiti{plan}")
        for name, w in (("5 saatlik", lim.primary), ("Haftalık", lim.secondary)):
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
            state = "sıfırlandı" if reset_passed else f"sıfırlanma {fmt.clock(w.resets_at)}"
            top.addWidget(label(state, th.muted, 11))
            top.addSpacing(8)
            top.addWidget(label(f"%{pct:.0f}", th.text, 12, True))
            box.addLayout(top)
            b = Bar(th, 6)
            b.set([(pct, color)], 100)
            box.addWidget(b)
            lay.addLayout(box)
        age = label(f"Son ölçüm: {fmt.ago(lim.ts)}", th.muted, 10)
        age.setContentsMargins(10, 0, 10, 0)
        lay.addWidget(age)

    # Seviye 1
    def _page_clients(self, lay, s: Summary, *_):
        th = self.theme
        self.crumb.setText("İstemciler")
        if not s.clients:
            lay.addWidget(label("Bu aralıkta kullanım yok.", th.text2, 13))
            return
        top = max(c.usage.total for c in s.clients)
        for src in SOURCE_LABELS:
            rows = [c for c in s.clients if c.source == src]
            if not rows:
                continue
            color = th.series[src]
            self._section(lay, SOURCE_LABELS[src], color, fmt.short(s.by_source[src].total))
            for c in rows:
                n = len(c.threads)
                lay.addWidget(
                    Row(
                        th,
                        c.label,
                        f"{models_text(c.models)} · {n} thread",
                        fmt.short(c.usage.total),
                        [(c.usage.total, color)],
                        top,
                        tooltip=usage_tooltip(c.usage),
                        on_click=lambda c=c: self.open(2, client=(c.source, c.client)),
                    )
                )

    # Seviye 2
    def _page_threads(self, lay, s, c: ClientRow, _):
        th = self.theme
        self.crumb.setText(f"{SOURCE_LABELS[c.source]} · {c.label}")
        color = th.series[c.source]
        self._section(lay, f"{len(c.threads)} thread", color, fmt.short(c.usage.total))
        top = c.threads[0].usage.total if c.threads else 1
        for t in c.threads:
            models = ", ".join(sorted(t.model_usage, key=lambda m: -t.model_usage[m].total))
            lay.addWidget(
                Row(
                    th,
                    t.title,
                    f"{t.project} · {models} · {fmt.ago(t.last_ts)}",
                    fmt.short(t.usage.total),
                    [(t.usage.total, color)],
                    top,
                    tooltip=f"{t.title}\n{t.cwd}\n\n{usage_tooltip(t.usage)}",
                    on_click=lambda t=t: self.open(3, thread=t.thread_id),
                )
            )

    # Seviye 3
    def _page_thread(self, lay, s, c: ClientRow, t: ThreadRow):
        th = self.theme
        color = th.series[t.source]
        self.crumb.setText(f"{SOURCE_LABELS[c.source]} · {c.label} · thread")
        title = label(t.title, th.text, 15, True)
        title.setWordWrap(True)
        title.setContentsMargins(10, 4, 10, 0)
        lay.addWidget(title)
        meta = [
            f"Proje: {t.project}" + (f"  ·  branch: {t.branch}" if t.branch else ""),
            f"{fmt.clock(t.first_ts)} – {fmt.clock(t.last_ts)}  ·  {t.calls} model çağrısı",
        ]
        if t.efforts:
            meta.append("Effort: " + ", ".join(sorted(t.efforts)))
        for m in meta:
            lb = label(m, th.text2, 11)
            lb.setContentsMargins(10, 2, 10, 0)
            lb.setToolTip(t.cwd)
            lay.addWidget(lb)
        hero = label(fmt.short(t.usage.total), th.text, 30, True)
        hero.setContentsMargins(10, 8, 10, 0)
        hero.setToolTip(usage_tooltip(t.usage))
        lay.addWidget(hero)

        total = t.usage.total or 1
        self._section(lay, "Modeller")
        for m, u in sorted(t.model_usage.items(), key=lambda x: -x[1].total):
            lay.addWidget(
                Row(th, m, fmt.percent(u.total, total), fmt.short(u.total), [(u.total, color)], total,
                    tooltip=usage_tooltip(u))
            )

        self._section(lay, "Token türleri")
        u = t.usage
        kinds = [
            ("Önbellekten okunan girdi", u.cache_read),
            ("Yeni girdi", u.input),
            ("Önbelleğe yazılan girdi", u.cache_write),
            ("Çıktı", u.output),
        ]
        for name, v in kinds:
            if v:
                sub = f"bunun {fmt.short(u.reasoning)} kadarı düşünme" if name == "Çıktı" and u.reasoning else ""
                lay.addWidget(Row(th, name, sub, fmt.short(v), [(v, color)], total, tooltip=fmt.full(v)))

        self._section(lay, "Ne üzerinde (yaklaşık)")
        for name, v in sorted(t.tools.items(), key=lambda x: -x[1])[:12]:
            lay.addWidget(
                Row(th, name, fmt.percent(v, total), fmt.short(v), [(v, color)], total, tooltip=fmt.full(v))
            )
        note = label(
            "Her model çağrısının token'ı o çağrıda kullanılan araçlara eşit bölünür.",
            th.muted,
            10,
        )
        note.setWordWrap(True)
        note.setContentsMargins(10, 4, 10, 0)
        lay.addWidget(note)

    # Konumlandırma
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


# --- Tray simgesi ------------------------------------------------------------------


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


class App(QObject):
    request_range = Signal(str)
    request_refresh = Signal()

    def __init__(self, make_store, show: bool):
        super().__init__()
        self.panel = Panel()
        self.tray_ok = QSystemTrayIcon.isSystemTrayAvailable()

        self.thread = QThread()
        self.worker = Worker(make_store)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.start)
        self.worker.updated.connect(self.on_update)
        self.request_range.connect(self.worker.set_range)
        self.request_refresh.connect(self.worker.refresh_now)
        self.panel.range_changed.connect(self.request_range.emit)
        self.panel.refresh_requested.connect(self.request_refresh.emit)
        self.thread.start()

        if self.tray_ok:
            self.tray = QSystemTrayIcon(make_icon(), self)
            self.tray.setToolTip("Token Paneli — yükleniyor")
            menu = QMenu()
            act_open = QAction("Paneli aç", menu)
            act_open.triggered.connect(self.show_panel)
            act_refresh = QAction("Yenile", menu)
            act_refresh.triggered.connect(self.request_refresh.emit)
            act_quit = QAction("Çıkış", menu)
            act_quit.triggered.connect(self.quit)
            menu.addActions([act_open, act_refresh])
            menu.addSeparator()
            menu.addAction(act_quit)
            self.menu = menu
            self.tray.setContextMenu(menu)
            self.tray.activated.connect(self.on_tray)
            self.tray.show()
        else:
            # Tray yoksa (ör. eklentisiz GNOME) normal pencere olarak çalış.
            self.panel.setWindowFlags(Qt.Window)
            self.panel.setAttribute(Qt.WA_TranslucentBackground, False)
            self.panel.changeEvent = lambda e: QWidget.changeEvent(self.panel, e)
            QApplication.instance().setQuitOnLastWindowClosed(True)
            show = True

        if show:
            self.show_panel()

    def on_tray(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.MiddleClick):
            if self.panel.isVisible():
                self.panel.hide()
            elif time.monotonic() - self.panel.hidden_at > 0.3:  # odak kaybıyla az önce kapandıysa yeniden açma
                self.show_panel()

    def show_panel(self):
        if self.tray_ok:
            self.panel.place(self.tray.geometry())
        self.panel.show()
        self.panel.raise_()
        self.panel.activateWindow()

    @Slot(object, object)
    def on_update(self, current: Summary, today: Summary):
        self.panel.set_summary(current)
        if self.tray_ok:
            parts = " · ".join(f"{SOURCE_LABELS[k]} {fmt.short(u.total)}" for k, u in today.by_source.items())
            self.tray.setToolTip(f"Bugün: {fmt.short(today.total.total)} token\n{parts}")

    def quit(self):
        self.thread.quit()
        self.thread.wait(2000)
        QApplication.quit()


def run(make_store, show: bool = False) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("tokenpanel")
    app.setApplicationDisplayName("Token Paneli")
    app.setQuitOnLastWindowClosed(False)
    app.setWindowIcon(make_icon())
    font = QFont(app.font())
    app.setFont(font)
    main = App(make_store, show)
    code = app.exec()
    main.thread.quit()
    main.thread.wait(2000)
    return code

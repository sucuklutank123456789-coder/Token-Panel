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

from PySide6.QtCore import QMetaObject, QObject, QPoint, QRect, QSettings, QSize, Qt, QThread, QTimer, Signal, Slot
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
from .model import DEFAULT_METRIC, METRICS, SOURCE_LABELS, Usage
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
    series={"claude": "#eb6834", "codex": "#2a78d6"},
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
    series={"claude": "#d95926", "codex": "#3987e5"},
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

    def __init__(self, make_store, metric: str):
        super().__init__()
        self._make_store = make_store
        self.store: Store | None = None
        self.range_key = "today"
        self.metric_key = metric
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

    @Slot()
    def stop(self):
        # Zamanlayıcı kendi iş parçacığında durdurulup silinmeli.
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

    @Slot()
    def refresh_now(self):
        if self.store is not None:
            self.store.refresh()
            self._emit()

    def _emit(self):
        today = self.store.summarize("today", self.metric_key)
        current = today if self.range_key == "today" else self.store.summarize(self.range_key, self.metric_key)
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
        f"Ham toplam: {fmt.full(u.total)}\n"
        f"Yeni girdi: {fmt.full(u.input)}\n"
        f"Önbellekten okunan: {fmt.full(u.cache_read)}\n"
        f"Önbelleğe yazılan: {fmt.full(u.cache_write)}\n"
        f"Çıktı: {fmt.full(u.output)} (düşünme: {fmt.full(u.reasoning)})"
    )


def models_text(models: dict[str, Usage], metric: str) -> str:
    return ", ".join(m for m, _ in sorted(models.items(), key=lambda x: -x[1].value(metric)))


# --- Ana pencere -----------------------------------------------------------------


class Panel(QWidget):
    range_changed = Signal(str)
    metric_changed = Signal(str)
    refresh_requested = Signal()

    WIDTH, HEIGHT = 380, 560

    def __init__(self, metric: str = DEFAULT_METRIC):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.metric_key = metric
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
        # Sayfalar her yenilemede yeniden çizilir; menü panele ait kalıcı nesnedir.
        self._metric_menu = QMenu(self)
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
        # Eski sayfa, tıklama olayı hâlâ işlenirken silinmesin diye sonra silinir.
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

    # Seviye 0
    def _page_summary(self, lay, s: Summary, *_):
        th = self.theme
        lay.addSpacing(14)
        v = self.v
        hero = label(fmt.short(v(s.total)), th.text, 46, True)
        hero.setAlignment(Qt.AlignHCenter)
        hero.setToolTip(usage_tooltip(s.total))
        lay.addWidget(hero)
        sub = label(f"token · {RANGES[s.range_key].lower()}", th.text2, 13)
        sub.setAlignment(Qt.AlignHCenter)
        lay.addWidget(sub)
        exact = label(fmt.full(v(s.total)), th.muted, 11)
        exact.setAlignment(Qt.AlignHCenter)
        lay.addWidget(exact)
        lay.addWidget(self._metric_button(s), 0, Qt.AlignHCenter)
        lay.addSpacing(16)

        # Claude / Codex payı
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
                text = f"Bunun {fmt.percent(s.total.cache_read, s.total.total)}'i önbellekten okunan girdi."
            else:
                text = (
                    f"Önbellekten tekrar okunan {fmt.short(s.total.cache_read)} token sayılmadı "
                    f"(ham toplam {fmt.short(s.total.total)})."
                )
            note = label(text, th.muted, 11)
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

    def _metric_button(self, s: Summary) -> QPushButton:
        """Hangi token'ların sayıldığını gösterir; tıklayınca ölçü seçilir."""
        th = self.theme
        btn = QPushButton(f"Ölçü: {METRICS[s.metric].lower()}  ▾")
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
            "app": "Varsayılan. Claude uygulamasındaki 'Total tokens' ile aynı yöntem: log'a bölünerek "
            "yazılan aynı yanıtın her satırı ayrı sayılır. Codex'te Codex'in kendi sayacıyla aynıdır "
            "(sohbet sıkıştırma çağrıları sayılmaz).",
            "io": "Her model yanıtı bir kez sayılır; Codex CLI'ın toplamıyla aynı tanım.",
            "new": "Önbelleğe ilk kez yazılan bağlam da sayılır.",
            "raw": "Her çağrıda önbellekten tekrar okunan bağlam da sayılır; çok büyük çıkar.",
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
                        f"{models_text(c.models, s.metric)} · {n} thread",
                        fmt.short(v(c.usage)),
                        [(v(c.usage), color)],
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
        v = self.v
        self._section(lay, f"{len(c.threads)} thread", color, fmt.short(v(c.usage)))
        top = (v(c.threads[0].usage) if c.threads else 0) or 1
        for t in c.threads:
            models = models_text(t.model_usage, s.metric)
            lay.addWidget(
                Row(
                    th,
                    t.title,
                    f"{t.project} · {models} · {fmt.ago(t.last_ts)}",
                    fmt.short(v(t.usage)),
                    [(v(t.usage), color)],
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
        v = self.v
        hero = label(fmt.short(v(t.usage)), th.text, 30, True)
        hero.setContentsMargins(10, 8, 10, 0)
        hero.setToolTip(usage_tooltip(t.usage))
        lay.addWidget(hero)

        total = v(t.usage) or 1
        self._section(lay, "Modeller")
        for m, u in sorted(t.model_usage.items(), key=lambda x: -v(x[1])):
            lay.addWidget(
                Row(th, m, fmt.percent(v(u), total), fmt.short(v(u)), [(v(u), color)], total,
                    tooltip=usage_tooltip(u))
            )

        self._section(lay, "Token türleri")
        u = t.usage
        included = {"io": {"input", "output"}, "new": {"input", "output", "cache_write"}}.get(
            s.metric, {"input", "output", "cache_write", "cache_read"}
        )
        kinds = [
            ("input", "Yeni girdi", u.input),
            ("output", "Çıktı", u.output),
            ("cache_write", "Önbelleğe yazılan girdi", u.cache_write),
            ("cache_read", "Önbellekten okunan girdi", u.cache_read),
        ]
        for key, name, n in kinds:
            if not n:
                continue
            notes = []
            if key == "output" and u.reasoning:
                notes.append(f"bunun {fmt.short(u.reasoning)} kadarı düşünme")
            if key not in included:
                notes.append("bu sayıya dahil değil")
            lay.addWidget(
                Row(th, name, " · ".join(notes), fmt.short(n), [(n, color if key in included else th.muted)],
                    u.total or 1, tooltip=fmt.full(n))
            )

        self._section(lay, "Ne üzerinde (yaklaşık)")
        for name, tu in sorted(t.tools.items(), key=lambda x: -v(x[1]))[:12]:
            lay.addWidget(
                Row(th, name, fmt.percent(v(tu), total), fmt.short(v(tu)), [(v(tu), color)], total,
                    tooltip=usage_tooltip(tu))
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
    request_metric = Signal(str)

    def __init__(self, make_store, show: bool):
        super().__init__()
        self.settings = QSettings("tokenpanel", "tokenpanel")
        metric = self.settings.value("metric", DEFAULT_METRIC)
        if metric not in METRICS:
            metric = DEFAULT_METRIC
        self.panel = Panel(metric)
        self.tray_ok = QSystemTrayIcon.isSystemTrayAvailable()

        self.thread = QThread()
        self.worker = Worker(make_store, metric)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.start)
        self.worker.updated.connect(self.on_update)
        self.request_range.connect(self.worker.set_range)
        self.request_refresh.connect(self.worker.refresh_now)
        self.request_metric.connect(self.worker.set_metric)
        self.panel.range_changed.connect(self.request_range.emit)
        self.panel.metric_changed.connect(self.on_metric)
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

    def on_metric(self, metric: str):
        self.settings.setValue("metric", metric)
        self.request_metric.emit(metric)

    @Slot(object, object)
    def on_update(self, current: Summary, today: Summary):
        self.panel.set_summary(current)
        if self.tray_ok:
            m = today.metric
            parts = " · ".join(f"{SOURCE_LABELS[k]} {fmt.short(u.value(m))}" for k, u in today.by_source.items())
            self.tray.setToolTip(f"Bugün: {fmt.short(today.total.value(m))} token\n{parts}")

    def shutdown(self):
        if self.thread.isRunning():
            QMetaObject.invokeMethod(self.worker, "stop", Qt.BlockingQueuedConnection)
            self.thread.quit()
            self.thread.wait(2000)

    def quit(self):
        self.shutdown()
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
    main.shutdown()
    return code

from __future__ import annotations

import html
import re
import sys
from pathlib import Path

from PySide6.QtCore import QRectF, QSettings, QSize, Qt, QThreadPool, QTimer, QUrl
from PySide6.QtGui import (
    QAction,
    QColor,
    QDesktopServices,
    QFont,
    QIcon,
    QKeySequence,
    QPainter,
    QPen,
    QPixmap,
    QSyntaxHighlighter,
    QTextCharFormat,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractSpinBox,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTabBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from logviewer.parsing import level_for_line, parse_log_line, timestamp_text
from logviewer.search import (
    ALL_LEVELS,
    ContextLine,
    SearchQuery,
    SearchResult,
    format_results,
)
from logviewer.sources import LogSource, format_size
from logviewer.workers import ContextTask, DiscoverTask, LogPageTask, SearchTask


APP_NAME = "VeyraLens"
APP_VERSION = "2.0.0"
PROJECT_URL = "https://github.com/Hmmcrs/VeyraLens"
RELEASES_URL = f"{PROJECT_URL}/releases/latest"
LOG_PAGE_SIZE = 2000
RESULT_BATCH_SIZE = 25
ICONS = {
    "open_file": "\uE8E5",
    "open_folder": "\uE838",
    "search": "\uE721",
    "players": "\uE716",
    "filters": "\uE71C",
    "errors": "\uE783",
    "bookmark": "\uE734",
    "page": "\uE8A5",
    "expand": "\uE710",
    "previous": "\uE76B",
    "next": "\uE76C",
    "copy": "\uE8C8",
    "export": "\uE896",
    "delete": "\uE74D",
}

DISPLAY_LOG_LINE = re.compile(
    r"^(?P<timestamp>.+?)  (?P<marker>\u25cf) "
    r"(?P<level>INFO|WARN|ERROR|DEBUG|TRACE|AC)\s{2,}(?P<message>.*)$"
)


def asset_path(filename: str) -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / filename


def fluent_icon(glyph: str, color: str, size: int = 20) -> QIcon:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    painter.setPen(QColor(color))
    font = QFont("Segoe Fluent Icons")
    font.setPixelSize(max(10, size - 4))
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, glyph)
    painter.end()
    return QIcon(pixmap)


class TerminalLogo(QLabel):
    def __init__(self):
        super().__init__()
        self.setFixedSize(84, 42)
        self.dark_pixmap = QPixmap(str(asset_path("terminal-logo.png")))
        image = self.dark_pixmap.toImage()
        for y in range(image.height()):
            for x in range(image.width()):
                color = image.pixelColor(x, y)
                if (
                    color.alpha()
                    and max(color.red(), color.green(), color.blue())
                    - min(color.red(), color.green(), color.blue())
                    < 18
                    and color.red() > 80
                ):
                    replacement = QColor("#1d2432")
                    replacement.setAlpha(color.alpha())
                    image.setPixelColor(x, y, replacement)
        self.light_pixmap = QPixmap.fromImage(image)
        self.set_theme("dark")

    def set_theme(self, theme):
        pixmap = self.light_pixmap if theme == "light" else self.dark_pixmap
        self.setPixmap(
            pixmap.scaled(
                self.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )


class LoadingSpinner(QWidget):
    def __init__(self):
        super().__init__()
        self.setFixedSize(16, 16)
        self.angle = 0
        self.color = QColor("#8e98ad")
        self.running = False
        self.timer = QTimer(self)
        self.timer.setInterval(70)
        self.timer.timeout.connect(self.advance)

    def set_theme(self, theme):
        self.color = QColor("#687188" if theme == "light" else "#aeb6cc")
        self.update()

    def start(self):
        self.running = True
        if not self.timer.isActive():
            self.timer.start()
        self.update()

    def stop(self):
        self.running = False
        self.timer.stop()
        self.update()

    def advance(self):
        self.angle = (self.angle - 30) % 360
        self.update()

    def paintEvent(self, event):
        if not self.running:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(self.color, 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawArc(QRectF(2.5, 2.5, 11, 11), self.angle * 16, 280 * 16)


class ContextHighlighter(QSyntaxHighlighter):
    def __init__(self, document):
        super().__init__(document)
        self.query = SearchQuery()
        self.target_block = -1
        self.target_color = QColor("#39345d")
        self.timestamp_color = QColor("#727c92")

    def set_theme(self, theme):
        self.target_color = QColor("#e7e3ff" if theme == "light" else "#39345d")
        self.timestamp_color = QColor("#687188" if theme == "light" else "#727c92")
        self.rehighlight()

    def set_state(self, query: SearchQuery | None, target_block: int):
        self.query = query or SearchQuery()
        self.target_block = target_block
        self.rehighlight()

    def highlightBlock(self, text):
        selected = self.currentBlock().blockNumber() == self.target_block
        if selected:
            target_format = QTextCharFormat()
            target_format.setBackground(self.target_color)
            self.setFormat(0, len(text), target_format)

        display_match = DISPLAY_LOG_LINE.match(text)
        if display_match:
            timestamp_format = QTextCharFormat()
            timestamp_format.setForeground(self.timestamp_color)
            if selected:
                timestamp_format.setBackground(self.target_color)
            self.setFormat(
                display_match.start("timestamp"),
                len(display_match.group("timestamp")),
                timestamp_format,
            )
            level = display_match.group("level")
            level_format = QTextCharFormat()
            level_format.setForeground(
                QColor(
                    {
                        "INFO": "#aeb6cc",
                        "WARN": "#e3aa2d",
                        "ERROR": "#ff5964",
                        "DEBUG": "#8067e8",
                        "TRACE": "#8e98ad",
                        "AC": "#35c99a",
                    }[level]
                )
            )
            if selected:
                level_format.setBackground(self.target_color)
            level_start = display_match.start("marker")
            self.setFormat(
                level_start,
                display_match.end("level") - level_start,
                level_format,
            )

        match_format = QTextCharFormat()
        match_format.setForeground(QColor("#14151c"))
        match_format.setBackground(QColor("#f3d45b"))
        for start, end in query_spans(text, self.query):
            self.setFormat(start, end - start, match_format)


class ResultCard(QWidget):
    def __init__(self, result: SearchResult, query: SearchQuery):
        super().__init__()
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 7, 10, 7)
        layout.setSpacing(4)

        metadata = QLabel()
        metadata.setObjectName("resultMeta")
        parts = [html.escape(result.source.display_name), f"line {result.line_number + 1}"]
        if result.timestamp:
            parts.append(html.escape(result.timestamp))
        if result.event_type:
            parts.append(f"<b>{html.escape(result.event_type)}</b>")
        metadata.setText(" &nbsp;·&nbsp; ".join(parts))
        metadata.setTextFormat(Qt.TextFormat.RichText)
        metadata.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(metadata)

        line = QLabel(highlighted_html(result.line, query))
        line.setObjectName("resultLine")
        line.setTextFormat(Qt.TextFormat.RichText)
        line.setWordWrap(False)
        line.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(line)

        before = [entry for entry in result.context if entry.line_number < result.line_number]
        after = [entry for entry in result.context if entry.line_number > result.line_number]
        nearby = before[-1:] + after[:1]
        if nearby:
            preview = "  ·  ".join(entry.text.strip() for entry in nearby[:2])
            context = QLabel(html.escape(_elide(preview, 220)))
            context.setObjectName("resultContext")
            context.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            layout.addWidget(context)


def query_spans(text: str, query: SearchQuery) -> list[tuple[int, int]]:
    if query.mode == "errors":
        pattern = re.compile(r"error|severe|exception|fatal|crash", re.IGNORECASE)
    elif query.mode == "player" and query.text:
        pattern = re.compile(
            rf"(?<![A-Za-z0-9_]){re.escape(query.text)}(?![A-Za-z0-9_])",
            re.IGNORECASE,
        )
    elif query.mode == "regex" and query.text:
        try:
            pattern = re.compile(query.text, 0 if query.case_sensitive else re.IGNORECASE)
        except re.error:
            return []
    elif query.text:
        pattern = re.compile(
            re.escape(query.text), 0 if query.case_sensitive else re.IGNORECASE
        )
    else:
        return []
    return [match.span() for match in pattern.finditer(text)]


def highlighted_html(text: str, query: SearchQuery) -> str:
    text = _elide(text, 420)
    spans = query_spans(text, query)
    if not spans:
        return html.escape(text)
    fragments = []
    cursor = 0
    for start, end in spans:
        fragments.append(html.escape(text[cursor:start]))
        fragments.append(
            '<span style="background:#f3d45b;color:#14151c;font-weight:600">'
            f"{html.escape(text[start:end])}</span>"
        )
        cursor = end
    fragments.append(html.escape(text[cursor:]))
    return "".join(fragments)


def _elide(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[:limit - 1]}…"


def formatted_log_line(text: str) -> str:
    parsed = parse_log_line(text)
    if parsed is None:
        return text
    timestamp = parsed.time_text.replace(",", ".")
    return f"{timestamp}  \u25cf {level_for_line(text):<5}  {parsed.message}"


class LogViewer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(QIcon(str(asset_path("log-viewer.ico"))))
        self.resize(1440, 900)
        self.setMinimumSize(980, 680)
        self.setAcceptDrops(True)

        legacy_theme = QSettings("LogViewer", "LogViewer").value("theme", "dark")
        self.settings = QSettings(APP_NAME, APP_NAME)
        self.theme = self.settings.value("theme", legacy_theme)
        self.sources: list[LogSource] = []
        self.archive_label = ""
        self.results: list[SearchResult] = []
        self.player_results: list[SearchResult] = []
        self.error_results: list[SearchResult] = []
        self.bookmarks: list[SearchResult] = []
        self.current_query = SearchQuery()
        self.current_result: SearchResult | None = None
        self.current_context_radius = 5
        self.current_log_source: LogSource | None = None
        self.current_log_start = 0
        self.current_log_has_more = False
        self.log_line_counts: dict[str, int] = {}
        self.displayed_context: tuple[ContextLine, ...] = ()
        self.displayed_source: LogSource | None = None
        self.displayed_query = SearchQuery()
        self.context_mode = "empty"
        self.active_view = "search"
        self.filters_visible = True
        self.tasks = {}
        self.result_render_jobs = {}
        self.result_render_generation = 0
        self.thread_pool = QThreadPool(self)
        self.thread_pool.setMaxThreadCount(3)
        self.icon_targets = []

        self.build_ui()
        self.install_shortcuts()
        self.apply_theme(self.theme)
        self.show_empty_workspace()

    def build_ui(self):
        root = QWidget()
        root.setObjectName("appRoot")
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        header = QFrame()
        header.setObjectName("header")
        header.setFixedHeight(92)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(33, 8, 16, 8)
        header_layout.setSpacing(12)

        logo_shell = QFrame()
        logo_shell.setObjectName("logoShell")
        logo_shell.setFixedSize(102, 54)
        logo_layout = QHBoxLayout(logo_shell)
        logo_layout.setContentsMargins(9, 6, 9, 6)
        self.logo = TerminalLogo()
        logo_layout.addWidget(self.logo)
        header_layout.addWidget(logo_shell)
        header_layout.addStretch(1)

        file_card = QFrame()
        file_card.setObjectName("fileCard")
        file_card.setFixedSize(272, 50)
        file_card_layout = QHBoxLayout(file_card)
        file_card_layout.setContentsMargins(12, 5, 12, 5)
        file_card_layout.setSpacing(10)
        file_icon = QLabel(ICONS["page"])
        file_icon.setObjectName("fileCardIcon")
        file_icon.setFont(QFont("Segoe Fluent Icons", 16))
        file_card_layout.addWidget(file_icon)
        file_text_layout = QVBoxLayout()
        file_text_layout.setSpacing(0)
        self.archive_name = QLabel("No log selected")
        self.archive_name.setObjectName("fileCardTitle")
        self.archive_name.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.archive_meta = QLabel("Open a ZIP, log, or folder to begin")
        self.archive_meta.setObjectName("fileCardMeta")
        file_text_layout.addWidget(self.archive_name)
        file_text_layout.addWidget(self.archive_meta)
        file_card_layout.addLayout(file_text_layout, 1)
        header_layout.addWidget(file_card)

        self.header_open_btn = QPushButton("Open ZIP, Log, or Folder")
        self.header_open_btn.setObjectName("headerOpenButton")
        self.header_open_btn.setFixedHeight(48)
        self.header_open_btn.setMinimumWidth(240)
        self.header_open_menu = QMenu(self.header_open_btn)
        self.header_file_action = self.header_open_menu.addAction("Open ZIP or Log")
        self.header_file_action.triggered.connect(self.open_path_dialog)
        self.header_folder_action = self.header_open_menu.addAction("Open Folder")
        self.header_folder_action.triggered.connect(self.open_folder_dialog)
        self.header_open_btn.setMenu(self.header_open_menu)
        header_layout.addWidget(self.header_open_btn)
        self._register_icon(self.header_open_btn, ICONS["open_folder"])
        self._register_icon(self.header_file_action, ICONS["open_file"])
        self._register_icon(self.header_folder_action, ICONS["open_folder"])
        header_layout.addStretch(3)

        self.theme_btn = QPushButton()
        self.theme_btn.setObjectName("secondaryButton")
        self.theme_btn.clicked.connect(self.toggle_theme)
        header_layout.addWidget(self.theme_btn)
        root_layout.addWidget(header)

        body = QFrame()
        body.setObjectName("body")
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(10, 10, 10, 8)
        body_layout.setSpacing(10)

        nav = QFrame()
        nav.setObjectName("navRail")
        nav.setFixedWidth(84)
        nav_layout = QVBoxLayout(nav)
        nav_layout.setContentsMargins(6, 7, 6, 7)
        nav_layout.setSpacing(4)

        self.open_file_nav = self.nav_button(
            ICONS["page"], "Open file", self.open_path_dialog
        )
        self.open_folder_nav = self.nav_button(
            ICONS["open_folder"], "Open folder", self.open_folder_dialog
        )
        nav_layout.addWidget(self.open_file_nav)
        nav_layout.addWidget(self.open_folder_nav)
        nav_layout.addWidget(self.nav_separator())

        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        self.search_nav = self.nav_button(
            ICONS["search"], "Search", self.show_search, True
        )
        self.players_nav = self.nav_button(
            ICONS["players"], "Players", self.show_players, True
        )
        self.errors_nav = self.nav_button(
            ICONS["errors"], "Errors", self.show_errors, True
        )
        for button in (
            self.search_nav,
            self.players_nav,
            self.errors_nav,
        ):
            self.mode_group.addButton(button)
            nav_layout.addWidget(button)
        body_layout.addWidget(nav, 0, Qt.AlignmentFlag.AlignTop)

        content_splitter = QSplitter(Qt.Orientation.Horizontal)
        content_splitter.setChildrenCollapsible(False)
        content_splitter.setHandleWidth(7)

        source_panel = QFrame()
        source_panel.setObjectName("panel")
        source_panel.setMinimumWidth(190)
        source_panel.setMaximumWidth(320)
        source_layout = QVBoxLayout(source_panel)
        source_layout.setContentsMargins(12, 13, 12, 12)
        source_layout.setSpacing(8)

        source_title = QLabel("LOADED LOGS")
        source_title.setObjectName("sectionEyebrow")
        source_layout.addWidget(source_title)
        self.source_list = QListWidget()
        self.source_list.setObjectName("sourceList")
        self.source_list.setSpacing(2)
        self.source_list.currentItemChanged.connect(self.open_source_item)
        source_layout.addWidget(self.source_list, 1)
        self.source_count = QLabel("0 files")
        self.source_count.setObjectName("mutedText")
        source_layout.addWidget(self.source_count)
        content_splitter.addWidget(source_panel)

        workspace = QFrame()
        workspace.setObjectName("workspace")
        workspace_layout = QVBoxLayout(workspace)
        workspace_layout.setContentsMargins(0, 0, 0, 0)
        workspace_layout.setSpacing(8)

        self.search_panel = QFrame()
        self.search_panel.setObjectName("panel")
        search_layout = QVBoxLayout(self.search_panel)
        search_layout.setContentsMargins(10, 9, 10, 9)
        search_layout.setSpacing(7)

        primary_search = QHBoxLayout()
        primary_search.setSpacing(7)
        self.search_box = QLineEdit()
        self.search_box.setObjectName("mainSearch")
        self.search_box.setPlaceholderText("Enter a player name or keyword")
        self.search_box.returnPressed.connect(self.run_search)
        primary_search.addWidget(self.search_box, 1)

        self.all_logs_search = QCheckBox("Search all logs")
        self.all_logs_search.setToolTip(
            "Search every loaded log instead of only the selected log"
        )
        primary_search.addWidget(self.all_logs_search)

        self.search_btn = QPushButton("Search")
        self.search_btn.setObjectName("primaryButton")
        self.search_btn.clicked.connect(self.run_search)
        primary_search.addWidget(self.search_btn)
        self._register_icon(self.search_btn, "\uE721")

        self.filters_btn = QPushButton("Filters")
        self.filters_btn.setObjectName("secondaryButton")
        self.filters_btn.setCheckable(True)
        self.filters_btn.setChecked(True)
        self.filters_btn.setToolTip("Show or hide search filters")
        self.filters_btn.toggled.connect(self.toggle_filters)
        primary_search.addWidget(self.filters_btn)
        self._register_icon(self.filters_btn, ICONS["filters"])
        search_layout.addLayout(primary_search)

        self.filter_row = QWidget()
        filter_layout = QHBoxLayout(self.filter_row)
        filter_layout.setContentsMargins(0, 0, 0, 0)
        filter_layout.setSpacing(7)
        self.level_label = QLabel("Show")
        filter_layout.addWidget(self.level_label)
        self.level_filter = QComboBox()
        self.level_filter.addItem("All entries", "all")
        self.level_filter.addItem("Anticheat", "anticheat")
        self.level_filter.addItem("Errors", "errors")
        self.level_filter.addItem("Warnings and errors", "warnings")
        self.level_filter.addItem("Info", "info")
        self.level_filter.currentIndexChanged.connect(
            self.update_search_placeholder
        )
        filter_layout.addWidget(self.level_filter)
        self.regex_search = QCheckBox("Regex")
        self.regex_search.setToolTip("Interpret the search text as a regular expression")
        self.regex_search.toggled.connect(self.update_search_placeholder)
        filter_layout.addWidget(self.regex_search)
        self.case_sensitive = QCheckBox("Case sensitive")
        filter_layout.addWidget(self.case_sensitive)
        filter_layout.addStretch(1)
        search_layout.addWidget(self.filter_row)
        workspace_layout.addWidget(self.search_panel)

        vertical_splitter = QSplitter(Qt.Orientation.Vertical)
        vertical_splitter.setChildrenCollapsible(False)
        vertical_splitter.setHandleWidth(7)

        context_panel = QFrame()
        context_panel.setObjectName("panel")
        context_layout = QVBoxLayout(context_panel)
        context_layout.setContentsMargins(0, 0, 0, 0)
        context_layout.setSpacing(0)
        context_header = QFrame()
        context_header.setObjectName("panelHeader")
        context_header_layout = QHBoxLayout(context_header)
        context_header_layout.setContentsMargins(12, 8, 9, 8)
        self.context_title = QLabel("Log context")
        self.context_title.setObjectName("sectionTitle")
        self.context_title.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        context_header_layout.addWidget(self.context_title)
        self.context_radius_label = QLabel("Context")
        self.context_radius_label.setObjectName("mutedText")
        context_header_layout.addWidget(self.context_radius_label)
        self.context_lines = QSpinBox()
        self.context_lines.setObjectName("contextSpin")
        self.context_lines.setRange(0, 50)
        self.context_lines.setValue(5)
        self.context_lines.setSuffix(" lines")
        self.context_lines.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.PlusMinus)
        self.context_lines.setMinimumWidth(112)
        self.context_lines.setToolTip("Lines shown before and after each match")
        self.context_lines.valueChanged.connect(self.context_radius_changed)
        context_header_layout.addWidget(self.context_lines)
        self.view_log_btn = QPushButton("View full log")
        self.view_log_btn.setObjectName("secondaryButton")
        self.view_log_btn.setToolTip("Open the full source log at this line")
        self.view_log_btn.clicked.connect(self.view_current_log)
        self.view_log_btn.hide()
        context_header_layout.addWidget(self.view_log_btn)
        self._register_icon(self.view_log_btn, ICONS["page"])

        self.previous_log_btn = QToolButton()
        self.previous_log_btn.setObjectName("iconButton")
        self.previous_log_btn.setToolTip("Previous log page")
        self.previous_log_btn.setFixedSize(32, 32)
        self.previous_log_btn.clicked.connect(self.previous_log_page)
        self.previous_log_btn.hide()
        context_header_layout.addWidget(self.previous_log_btn)
        self._register_icon(self.previous_log_btn, ICONS["previous"])
        self.log_page_label = QLabel()
        self.log_page_label.setObjectName("mutedText")
        self.log_page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.log_page_label.setMinimumWidth(118)
        self.log_page_label.hide()
        context_header_layout.addWidget(self.log_page_label)
        self.next_log_btn = QToolButton()
        self.next_log_btn.setObjectName("iconButton")
        self.next_log_btn.setToolTip("Next log page")
        self.next_log_btn.setFixedSize(32, 32)
        self.next_log_btn.clicked.connect(self.next_log_page)
        self.next_log_btn.hide()
        context_header_layout.addWidget(self.next_log_btn)
        self._register_icon(self.next_log_btn, ICONS["next"])

        self.bookmark_line_btn = QPushButton("Bookmark line")
        self.bookmark_line_btn.setObjectName("secondaryButton")
        self.bookmark_line_btn.setToolTip("Bookmark the selected line in this log")
        self.bookmark_line_btn.clicked.connect(self.bookmark_current_line)
        self.bookmark_line_btn.hide()
        context_header_layout.addWidget(self.bookmark_line_btn)
        self._register_icon(self.bookmark_line_btn, ICONS["bookmark"])

        self.expand_context_btn = QPushButton("Expand context")
        self.expand_context_btn.setObjectName("secondaryButton")
        self.expand_context_btn.setEnabled(False)
        self.expand_context_btn.clicked.connect(self.expand_context)
        context_header_layout.addWidget(self.expand_context_btn)
        self._register_icon(self.expand_context_btn, ICONS["expand"])
        context_layout.addWidget(context_header)

        self.context_output = QPlainTextEdit()
        self.context_output.setObjectName("contextOutput")
        self.context_output.setReadOnly(True)
        self.context_output.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        context_font = QFont("Consolas", 10)
        self.context_output.setFont(context_font)
        self.context_output.cursorPositionChanged.connect(self.context_line_changed)
        self.context_highlighter = ContextHighlighter(self.context_output.document())
        context_layout.addWidget(self.context_output, 1)
        vertical_splitter.addWidget(context_panel)

        results_panel = QFrame()
        results_panel.setObjectName("panel")
        results_layout = QVBoxLayout(results_panel)
        results_layout.setContentsMargins(0, 0, 0, 0)
        results_layout.setSpacing(0)
        results_header = QFrame()
        results_header.setObjectName("panelHeader")
        results_header_layout = QVBoxLayout(results_header)
        results_header_layout.setContentsMargins(12, 5, 9, 7)
        results_header_layout.setSpacing(4)
        results_tab_layout = QHBoxLayout()
        results_tab_layout.setSpacing(7)
        self.results_tabs = QTabBar()
        self.results_tabs.setObjectName("resultsTabs")
        self.results_tabs.setDrawBase(False)
        self.results_tabs.setExpanding(False)
        self.results_tabs.setUsesScrollButtons(False)
        self.results_tabs.addTab("Search results")
        self.results_tabs.addTab("Bookmarks (0)")
        self.results_tabs.currentChanged.connect(self.change_results_tab)
        results_tab_layout.addWidget(self.results_tabs)
        self.result_count = QLabel("0 results")
        self.result_count.setObjectName("mutedText")
        results_tab_layout.addWidget(self.result_count)
        results_tab_layout.addStretch(1)
        results_header_layout.addLayout(results_tab_layout)

        results_action_layout = QHBoxLayout()
        results_action_layout.setSpacing(7)
        self.include_context = QCheckBox("Context")
        self.include_context.setChecked(True)
        self.include_context.setToolTip(
            "Include surrounding lines when copying or exporting"
        )
        results_action_layout.addWidget(self.include_context)
        results_action_layout.addStretch(1)
        self.bookmark_btn = QPushButton("Bookmark")
        self.bookmark_btn.setObjectName("secondaryButton")
        self.bookmark_btn.clicked.connect(self.save_selected_results)
        results_action_layout.addWidget(self.bookmark_btn)
        self._register_icon(self.bookmark_btn, ICONS["bookmark"])
        self.remove_bookmark_btn = QPushButton("Remove")
        self.remove_bookmark_btn.setObjectName("secondaryButton")
        self.remove_bookmark_btn.setToolTip("Remove selected bookmarks")
        self.remove_bookmark_btn.clicked.connect(self.remove_selected_bookmarks)
        self.remove_bookmark_btn.hide()
        results_action_layout.addWidget(self.remove_bookmark_btn)
        self._register_icon(self.remove_bookmark_btn, ICONS["delete"])
        self.copy_btn = QPushButton("Copy")
        self.copy_btn.setObjectName("secondaryButton")
        self.copy_btn.setToolTip("Copy selected results or bookmarks (Ctrl+Shift+C)")
        self.copy_btn.clicked.connect(self.copy_bookmarks)
        results_action_layout.addWidget(self.copy_btn)
        self._register_icon(self.copy_btn, ICONS["copy"])
        self.export_btn = QPushButton("Export")
        self.export_btn.setObjectName("primaryButton")
        self.export_btn.clicked.connect(self.export_bookmarks)
        results_action_layout.addWidget(self.export_btn)
        self._register_icon(self.export_btn, ICONS["export"])
        results_header_layout.addLayout(results_action_layout)
        results_layout.addWidget(results_header)

        self.result_pages = QStackedWidget()
        self.result_list = self.make_result_list()
        self.result_list.itemClicked.connect(self.open_result_item)
        self.result_list.itemDoubleClicked.connect(self.open_result_item)
        self.player_list = self.make_result_list()
        self.player_list.itemClicked.connect(self.open_result_item)
        self.player_list.itemDoubleClicked.connect(self.open_result_item)
        self.error_list = self.make_result_list()
        self.error_list.itemClicked.connect(self.open_result_item)
        self.error_list.itemDoubleClicked.connect(self.open_result_item)
        self.bookmark_list = self.make_result_list()
        self.bookmark_list.itemClicked.connect(self.open_result_item)
        self.result_pages.addWidget(self.result_list)
        self.result_pages.addWidget(self.player_list)
        self.result_pages.addWidget(self.error_list)
        self.result_pages.addWidget(self.bookmark_list)
        results_layout.addWidget(self.result_pages, 1)
        vertical_splitter.addWidget(results_panel)
        vertical_splitter.setSizes([390, 330])
        workspace_layout.addWidget(vertical_splitter, 1)
        content_splitter.addWidget(workspace)
        content_splitter.setStretchFactor(0, 0)
        content_splitter.setStretchFactor(1, 1)
        content_splitter.setSizes([245, 1100])
        body_layout.addWidget(content_splitter, 1)
        root_layout.addWidget(body, 1)

        footer = QFrame()
        footer.setObjectName("footer")
        footer.setFixedHeight(38)
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(14, 5, 14, 5)
        self.spinner = LoadingSpinner()
        footer_layout.addWidget(self.spinner)
        self.status = QLabel("Ready")
        self.status.setObjectName("mutedText")
        footer_layout.addWidget(self.status)
        footer_layout.addStretch(1)
        self.footer_source = QLabel("No logs loaded")
        self.footer_source.setObjectName("mutedText")
        footer_layout.addWidget(self.footer_source)
        footer_layout.addSpacing(16)
        self.brand_wordmark = QToolButton()
        self.brand_wordmark.setObjectName("footerImageLink")
        self.brand_wordmark.setFixedSize(102, 20)
        self.brand_wordmark.setIconSize(self.brand_wordmark.size())
        self.brand_wordmark.setCursor(Qt.CursorShape.PointingHandCursor)
        self.brand_wordmark.setToolTip("Open VeyraLens on GitHub")
        self.brand_wordmark.setAccessibleName("Open VeyraLens on GitHub")
        self.brand_wordmark.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(PROJECT_URL))
        )
        self.brand_wordmarks = {
            "dark": QPixmap(str(asset_path("veyra-lens-wordmark.png"))),
            "light": QPixmap(str(asset_path("veyra-lens-wordmark-light.png"))),
        }
        footer_layout.addWidget(self.brand_wordmark)
        footer_layout.addSpacing(12)
        self.version_link = QToolButton()
        self.version_link.setObjectName("footerLink")
        self.version_link.setText(f"v{APP_VERSION}")
        self.version_link.setCursor(Qt.CursorShape.PointingHandCursor)
        self.version_link.setToolTip("Open the latest VeyraLens release")
        self.version_link.setAccessibleName("Open the latest VeyraLens release")
        self.version_link.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(RELEASES_URL))
        )
        footer_layout.addWidget(self.version_link)
        root_layout.addWidget(footer)

    def nav_button(self, glyph, text, callback, checkable=False):
        button = QToolButton()
        button.setObjectName("navButton")
        button.setText(text)
        button.setToolTip(text)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        button.setIconSize(QSize(22, 22))
        button.setFixedSize(72, 50)
        button.setCheckable(checkable)
        button.clicked.connect(callback)
        self._register_icon(button, glyph, 24)
        if checkable:
            button.toggled.connect(self.refresh_icons)
        return button

    @staticmethod
    def nav_separator():
        separator = QFrame()
        separator.setObjectName("navSeparator")
        separator.setFrameShape(QFrame.Shape.HLine)
        return separator

    @staticmethod
    def make_result_list():
        widget = QListWidget()
        widget.setObjectName("resultList")
        widget.setSpacing(3)
        widget.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        widget.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        return widget

    def _register_icon(self, button, glyph, size=20):
        self.icon_targets.append((button, glyph, size))

    def refresh_icons(self, *_):
        normal = "#596178" if self.theme == "light" else "#aeb6cc"
        accent = "#6756df" if self.theme == "light" else "#9a86ff"
        for button, glyph, size in self.icon_targets:
            if button.objectName() == "primaryButton":
                color = "#ffffff"
            elif button.objectName() == "headerOpenButton":
                color = accent
            elif button.isCheckable() and button.isChecked():
                color = accent
            else:
                color = normal
            button.setIcon(fluent_icon(glyph, color, size))

    def install_shortcuts(self):
        shortcuts = (
            (QKeySequence.StandardKey.Open, self.open_path_dialog),
            (QKeySequence("Ctrl+Shift+O"), self.open_folder_dialog),
            (QKeySequence.StandardKey.Find, self.focus_search),
            (QKeySequence("Ctrl+Shift+C"), self.copy_bookmarks),
            (QKeySequence("Ctrl+E"), self.export_bookmarks),
            (QKeySequence("F3"), lambda: self.move_result_selection(1)),
            (QKeySequence("Shift+F3"), lambda: self.move_result_selection(-1)),
        )
        for sequence, callback in shortcuts:
            action = QAction(self)
            action.setShortcut(sequence)
            action.triggered.connect(callback)
            self.addAction(action)
        delete_action = QAction(self.bookmark_list)
        delete_action.setShortcut(QKeySequence.StandardKey.Delete)
        delete_action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        delete_action.triggered.connect(self.remove_selected_bookmarks)
        self.bookmark_list.addAction(delete_action)

    def apply_theme(self, theme):
        self.theme = "light" if theme == "light" else "dark"
        self.settings.setValue("theme", self.theme)
        self.setStyleSheet(build_stylesheet(self.theme))
        self.logo.set_theme(self.theme)
        self.brand_wordmark.setIcon(
            QIcon(
                self.brand_wordmarks[self.theme].scaled(
                    self.brand_wordmark.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        )
        self.spinner.set_theme(self.theme)
        self.context_highlighter.set_theme(self.theme)
        icon_color = "#596178" if self.theme == "light" else "#aeb6cc"
        self.refresh_icons()
        if self.theme == "dark":
            self.theme_btn.setText("Light mode")
            self.theme_btn.setIcon(fluent_icon("\uE706", icon_color))
            scheme = Qt.ColorScheme.Dark
        else:
            self.theme_btn.setText("Dark mode")
            self.theme_btn.setIcon(fluent_icon("\uE708", icon_color))
            scheme = Qt.ColorScheme.Light
        style_hints = QApplication.styleHints()
        if hasattr(style_hints, "setColorScheme"):
            style_hints.setColorScheme(scheme)

    def toggle_theme(self):
        self.apply_theme("light" if self.theme == "dark" else "dark")

    def open_path_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open server logs",
            "",
            "Supported logs (*.zip *.log *.txt *.gz);;"
            "ZIP archives (*.zip);;"
            "Log files (*.log *.txt *.gz)",
        )
        if path:
            self.load_path(path)

    def open_folder_dialog(self):
        path = QFileDialog.getExistingDirectory(self, "Open logs folder")
        if path:
            self.load_path(path)

    def load_path(self, path):
        self.cancel_all_tasks()
        task = DiscoverTask(path)
        self.run_task("discover", task, self.on_sources_discovered, "Inspecting logs…")

    def on_sources_discovered(self, payload):
        label, sources = payload
        self.archive_label = label
        self.sources = sources
        self.results.clear()
        self.player_results.clear()
        self.error_results.clear()
        self.bookmarks.clear()
        self.log_line_counts.clear()
        self.source_list.clear()
        for source in sources:
            item = QListWidgetItem(
                f"{source.display_name}\n{format_size(source.size)}"
            )
            item.setToolTip(source.display_name)
            item.setData(Qt.ItemDataRole.UserRole, source)
            item.setSizeHint(QSize(0, 48))
            self.source_list.addItem(item)
        self.archive_name.setText(label)
        self.archive_name.setToolTip(label)
        self.archive_meta.setText(
            f"{len(sources):,} supported file{'s' if len(sources) != 1 else ''}"
        )
        self.source_count.setText(
            f"{len(sources):,} supported file{'s' if len(sources) != 1 else ''}"
        )
        self.footer_source.setText(f"{label} · {len(sources):,} files")
        self.status.setText("Logs are ready to search.")
        self.populate_result_list(
            self.result_list,
            [],
            SearchQuery(),
            "Enter a keyword or choose a filter to search this log.",
        )
        self.populate_result_list(
            self.player_list,
            [],
            SearchQuery(mode="player"),
            "Enter a player name to list their activity in this log.",
        )
        self.populate_result_list(
            self.error_list,
            [],
            SearchQuery(mode="errors"),
            "Open Errors to list errors and Java stack traces in this log.",
        )
        self.populate_bookmarks()
        self.show_search()
        if sources:
            self.source_list.setCurrentRow(0)

    def open_source_item(self, item, _previous=None):
        if item is None:
            return
        source = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(source, LogSource):
            return
        self.cancel_task("search")
        self.current_result = None
        self.current_query = SearchQuery()
        self.results.clear()
        self.player_results.clear()
        self.error_results.clear()
        self.populate_result_list(
            self.result_list,
            [],
            SearchQuery(),
            "Enter a keyword or choose a filter to search this log.",
        )
        self.populate_result_list(
            self.player_list,
            [],
            SearchQuery(mode="player"),
            "Enter a player name to list their activity in this log.",
        )
        self.populate_result_list(
            self.error_list,
            [],
            SearchQuery(mode="errors"),
            "Open Errors to list errors and Java stack traces in this log.",
        )
        if self.results_tabs.currentIndex() == 0 and self.active_view in {
            "search",
            "players",
        }:
            self.result_count.setText("0 results")
        if self.active_view == "errors":
            self.show_errors()
        else:
            self.show_log_page(source)

    def show_log_page(self, source, start=0, target_line=-1):
        start = max(0, start)
        task = LogPageTask(
            source,
            start,
            LOG_PAGE_SIZE,
            target_line,
            self.log_line_counts.get(source.key),
        )
        self.run_task(
            "context",
            task,
            self.on_log_page_loaded,
            f"Loading {source.display_name}…",
        )
        self.context_title.setText(source.display_name)
        self.set_context_error_state(False)
        self.context_output.setPlainText("Loading log lines…")
        self.set_context_mode("loading")

    def on_log_page_loaded(self, payload):
        source, start, context, has_more, target_line, total_lines = payload
        self.current_log_source = source
        self.current_log_start = start
        self.current_log_has_more = has_more
        self.log_line_counts[source.key] = total_lines

        self.context_title.setText(source.display_name)
        self.log_page_label.setText(f"Lines: {total_lines:,}")
        query = self.current_query if target_line >= 0 else SearchQuery()
        self.display_context(context, target_line, query, source)
        self.set_context_mode("log")
        self.status.setText(
            "Viewing the complete log in scrollable 2,000-line pages."
        )

    def previous_log_page(self):
        if self.current_log_source is None or self.current_log_start <= 0:
            return
        self.show_log_page(
            self.current_log_source,
            max(0, self.current_log_start - LOG_PAGE_SIZE),
        )

    def next_log_page(self):
        if self.current_log_source is None or not self.current_log_has_more:
            return
        self.show_log_page(
            self.current_log_source, self.current_log_start + LOG_PAGE_SIZE
        )

    def view_current_log(self):
        target_line = -1
        if self.current_result is not None:
            source = self.current_result.source
            target_line = self.current_result.line_number
        elif self.current_log_source is not None:
            source = self.current_log_source
        else:
            item = self.source_list.currentItem()
            source = item.data(Qt.ItemDataRole.UserRole) if item else None
        if not isinstance(source, LogSource):
            self.status.setText("Select a log file first.")
            return
        start = 0 if target_line < 0 else target_line // LOG_PAGE_SIZE * LOG_PAGE_SIZE
        self.show_log_page(source, start, target_line)

    def set_context_mode(self, mode):
        self.context_mode = mode
        log_page = mode == "log"
        result_context = mode == "result"
        self.view_log_btn.setVisible(result_context)
        self.bookmark_line_btn.setVisible(log_page)
        self.expand_context_btn.setVisible(result_context)
        self.expand_context_btn.setEnabled(result_context)
        for widget in (self.previous_log_btn, self.log_page_label, self.next_log_btn):
            widget.setVisible(log_page)
        self.previous_log_btn.setEnabled(log_page and self.current_log_start > 0)
        self.next_log_btn.setEnabled(log_page and self.current_log_has_more)

    def set_context_error_state(self, enabled):
        if self.context_output.property("errorState") == enabled:
            return
        self.context_output.setProperty("errorState", enabled)
        self.context_output.style().unpolish(self.context_output)
        self.context_output.style().polish(self.context_output)

    def on_context_loaded(self, payload):
        source, line_number, context = payload
        query = self.current_result.query if self.current_result else self.current_query
        if self.current_result is not None:
            self.current_result = SearchResult(
                source=self.current_result.source,
                line_number=self.current_result.line_number,
                line=self.current_result.line,
                timestamp=self.current_result.timestamp,
                level=self.current_result.level,
                event_type=self.current_result.event_type,
                context=tuple(context),
                query=self.current_result.query,
                context_radius=self.current_context_radius,
            )
        self.context_title.setText(f"{source.display_name} · line {line_number + 1}")
        self.display_context(context, line_number, query, source)
        self.current_log_source = source
        self.set_context_mode("result")

    def show_primary_results(self, widget, title):
        self.results_tabs.blockSignals(True)
        self.results_tabs.setTabText(0, title)
        self.results_tabs.setCurrentIndex(0)
        self.results_tabs.blockSignals(False)
        self.result_pages.setCurrentWidget(widget)
        self.bookmark_btn.show()
        self.remove_bookmark_btn.hide()

    def change_results_tab(self, index):
        if index == 1:
            self.result_pages.setCurrentWidget(self.bookmark_list)
            rendering = self.populate_bookmarks()
            if not rendering:
                self.result_count.setText(
                    f"{len(self.bookmarks):,} bookmark"
                    f"{'s' if len(self.bookmarks) != 1 else ''}"
                )
            self.bookmark_btn.hide()
            self.remove_bookmark_btn.show()
            return

        if self.active_view == "players":
            widget = self.player_list
            count = len(self.player_results)
            noun = "result"
        elif self.active_view == "errors":
            widget = self.error_list
            count = len(self.error_results)
            noun = "error"
        else:
            widget = self.result_list
            count = len(self.results)
            noun = "result"
        self.result_pages.setCurrentWidget(widget)
        if "search" in self.tasks:
            self.result_count.setText("Searching…")
        elif widget in self.result_render_jobs:
            job = self.result_render_jobs[widget]
            self.result_count.setText(
                f"{job['index']:,} / {len(job['results']):,} {noun}s"
            )
        else:
            self.result_count.setText(
                f"{count:,} {noun}{'s' if count != 1 else ''}"
            )
        self.bookmark_btn.show()
        self.remove_bookmark_btn.hide()

    def show_search(self):
        self.cancel_task("search")
        self.active_view = "search"
        self.search_nav.setChecked(True)
        self.search_panel.show()
        self.show_primary_results(self.result_list, "Search results")
        self.filters_btn.show()
        self.level_label.show()
        self.level_filter.show()
        self.regex_search.show()
        self.case_sensitive.show()
        self.filter_row.setVisible(self.filters_visible)
        self.search_btn.setText("Search")
        self.result_count.setText(
            f"{len(self.results):,} result{'s' if len(self.results) != 1 else ''}"
        )
        self.update_search_placeholder()
        source = self.selected_source()
        if source is not None:
            self.show_log_page(source)
        self.focus_search()

    def show_players(self):
        self.cancel_task("search")
        self.active_view = "players"
        self.players_nav.setChecked(True)
        self.search_panel.show()
        self.show_primary_results(self.player_list, "Player activity")
        self.filters_btn.hide()
        self.regex_search.hide()
        self.case_sensitive.hide()
        self.filter_row.hide()
        self.search_btn.setText("Find player activity")
        self.result_count.setText(
            f"{len(self.player_results):,} result"
            f"{'s' if len(self.player_results) != 1 else ''}"
        )
        self.update_search_placeholder()
        source = self.selected_source()
        if source is not None:
            self.show_log_page(source)
        self.focus_search()

    def show_errors(self):
        self.cancel_task("search")
        self.active_view = "errors"
        self.errors_nav.setChecked(True)
        self.search_panel.hide()
        self.show_primary_results(self.error_list, "Errors")
        self.result_count.setText(
            f"{len(self.error_results):,} error"
            f"{'s' if len(self.error_results) != 1 else ''}"
        )
        self.start_search(SearchQuery(mode="errors"), "Errors")

    def update_search_placeholder(self, *_):
        if self.active_view == "players":
            placeholder = "Enter the exact player name"
        elif self.regex_search.isChecked():
            placeholder = "Enter a regular expression"
        elif self.level_filter.currentData() != "all":
            placeholder = "Optional text filter"
        else:
            placeholder = "Enter text, a command, or a keyword"
        self.search_box.setPlaceholderText(placeholder)

    def focus_search(self):
        if self.active_view not in {"search", "players"}:
            self.show_search()
            return
        self.search_box.setFocus()
        self.search_box.selectAll()

    def toggle_filters(self, visible):
        self.filters_visible = visible
        self.filter_row.setVisible(visible and self.active_view == "search")

    def make_query(self, mode=None):
        if mode is None:
            if self.active_view == "players":
                mode = "player"
            elif self.active_view == "errors":
                mode = "errors"
            else:
                mode = "regex" if self.regex_search.isChecked() else "text"
        if mode in {"player", "errors"}:
            levels = ALL_LEVELS
        else:
            levels = {
                "all": ALL_LEVELS,
                "anticheat": frozenset({"AC"}),
                "errors": frozenset({"ERROR"}),
                "warnings": frozenset({"WARN", "ERROR"}),
                "info": frozenset({"INFO"}),
            }[self.level_filter.currentData()]
        return SearchQuery(
            text=self.search_box.text().strip(),
            mode=mode,
            case_sensitive=self.case_sensitive.isChecked(),
            levels=levels,
        )

    def run_search(self):
        query = self.make_query()
        if query.mode == "errors":
            self.show_errors()
            return
        text_required = query.mode in {"player", "regex"} or (
            query.mode == "text" and query.levels == ALL_LEVELS
        )
        if not query.text and text_required:
            self.status.setText("Enter a player name or search term first.")
            self.focus_search()
            return
        if query.mode == "regex":
            try:
                re.compile(query.text, 0 if query.case_sensitive else re.IGNORECASE)
            except re.error as error:
                self.show_error(f"The regular expression is not valid: {error}")
                return
        title = "Player activity" if query.mode == "player" else "Search results"
        self.start_search(query, title)

    def start_search(self, query, title):
        search_all = query.mode != "errors" and self.all_logs_search.isChecked()
        if search_all:
            if not self.sources:
                self.status.setText("Open a ZIP archive, log file, or folder first.")
                return
            sources = list(self.sources)
            source = None
            scope_name = f"{len(sources):,} loaded logs"
        else:
            source = self.require_selected_source()
            if source is None:
                return
            sources = [source]
            scope_name = source.display_name
        self.current_query = query
        self.current_result = None
        context = self.context_lines.value()
        if query.mode == "player":
            self.player_results = []
            target = self.player_list
        elif query.mode == "errors":
            self.error_results = []
            target = self.error_list
            self.cancel_task("context")
            self.context_title.setText("Errors")
            self.set_context_error_state(False)
            self.context_output.setPlainText(
                f"Finding errors and Java stack traces in {source.display_name}…"
            )
            self.set_context_mode("error-loading")
        else:
            self.results = []
            target = self.result_list
        self.show_primary_results(target, title)
        self.result_count.setText("Searching…")
        self.result_render_jobs.pop(target, None)
        target.clear()
        task = SearchTask(sources, query, context, context)
        self.run_task(
            "search", task, self.on_search_finished, f"Searching {scope_name}…"
        )

    def on_search_finished(self, payload):
        results, limited, errors = payload
        query = self.current_query
        if query.mode == "player":
            self.player_results = results
            target = self.player_list
            empty_message = "No activity for that player was found in this log."
            noun = "result"
        elif query.mode == "errors":
            self.error_results = results
            target = self.error_list
            empty_message = "No errors or Java stack traces were found in this log."
            noun = "error"
        else:
            self.results = results
            target = self.result_list
            empty_message = "No matching lines were found in this log."
            noun = "result"
        rendering = self.populate_result_list(
            target, results, query, empty_message, noun=noun
        )
        if self.results_tabs.currentIndex() == 0 and not rendering:
            self.result_count.setText(
                f"{len(results):,} {noun}{'s' if len(results) != 1 else ''}"
            )
        if (
            query.mode == "errors"
            and self.active_view == "errors"
            and self.results_tabs.currentIndex() == 0
        ):
            if results:
                first_item = self.error_list.item(0)
                self.error_list.setCurrentItem(first_item)
                self.open_result_item(first_item)
            elif self.context_mode == "error-loading":
                self.context_title.setText("Errors")
                self.context_output.setPlainText(empty_message)
                self.set_context_mode("empty")
        message = f"Found {len(results):,} {noun}{'s' if len(results) != 1 else ''}."
        if limited:
            message += " Some additional matches were omitted to keep results responsive."
        if errors:
            message += f" {len(errors)} file{'s were' if len(errors) != 1 else ' was'} unreadable."
        self.status.setText(message)

    def populate_result_list(
        self, widget, results, query, empty_message, noun="result"
    ):
        self.result_render_jobs.pop(widget, None)
        widget.clear()
        if not results:
            self.add_empty_item(widget, empty_message)
            self.finish_loading_if_idle()
            return False

        self.result_render_generation += 1
        generation = self.result_render_generation
        self.result_render_jobs[widget] = {
            "generation": generation,
            "results": results,
            "query": query,
            "index": 0,
            "noun": noun,
        }
        self.spinner.start()
        self.render_result_batch(widget, generation)
        return widget in self.result_render_jobs

    def render_result_batch(self, widget, generation):
        job = self.result_render_jobs.get(widget)
        if job is None or job["generation"] != generation:
            return

        results = job["results"]
        start = job["index"]
        end = min(start + RESULT_BATCH_SIZE, len(results))
        widget.setUpdatesEnabled(False)
        for result in results[start:end]:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, result)
            card = ResultCard(result, job["query"] or self.query_for_result(result))
            item.setSizeHint(card.sizeHint())
            widget.addItem(item)
            widget.setItemWidget(item, card)
        widget.setUpdatesEnabled(True)

        job["index"] = end
        if self.result_pages.currentWidget() is widget:
            noun = job["noun"]
            if end < len(results):
                self.result_count.setText(
                    f"{end:,} / {len(results):,} {noun}s"
                )
            else:
                self.result_count.setText(
                    f"{len(results):,} {noun}"
                    f"{'s' if len(results) != 1 else ''}"
                )

        if end < len(results):
            QTimer.singleShot(
                0,
                lambda widget=widget, generation=generation: self.render_result_batch(
                    widget, generation
                ),
            )
            return

        self.result_render_jobs.pop(widget, None)
        self.finish_loading_if_idle()

    def open_result_item(self, item):
        result = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(result, SearchResult):
            return
        self.cancel_task("context")
        self.current_result = result
        self.current_query = result.query
        self.context_title.setText(
            f"{result.source.display_name} · line {result.line_number + 1}"
        )
        self.show_result_context(result)
        self.select_source(result.source)

    def show_result_context(self, result):
        radius = self.context_lines.value()
        self.current_context_radius = radius
        start = max(0, result.line_number - radius)
        end = result.line_number + radius
        context = tuple(
            line for line in result.context if start <= line.line_number <= end
        )
        self.display_context(context, result.line_number, result.query, result.source)
        self.current_log_source = result.source
        self.set_context_mode("result")

        if result.context_radius == radius:
            return
        task = ContextTask(result.source, result.line_number, radius, radius)
        self.run_task(
            "context",
            task,
            self.on_context_loaded,
            f"Loading {radius} lines of context…",
        )

    def context_radius_changed(self, radius):
        self.current_context_radius = radius
        if self.current_result is not None and self.context_mode == "result":
            self.show_result_context(self.current_result)

    def query_for_result(self, result):
        return result.query

    def expand_context(self):
        if self.current_result is None:
            return
        self.current_context_radius = min(200, self.current_context_radius + 10)
        task = ContextTask(
            self.current_result.source,
            self.current_result.line_number,
            self.current_context_radius,
            self.current_context_radius,
        )
        self.run_task(
            "context",
            task,
            self.on_context_loaded,
            f"Loading {self.current_context_radius} lines of context…",
        )

    def display_context(self, context, target_line, query, source):
        self.set_context_error_state(False)
        self.displayed_context = tuple(context)
        self.displayed_source = source
        self.displayed_query = query
        lines = [formatted_log_line(entry.text) for entry in self.displayed_context]
        self.context_output.setPlainText("\n".join(lines))
        target_block = -1
        for index, entry in enumerate(self.displayed_context):
            if entry.line_number == target_line:
                target_block = index
                break
        self.context_highlighter.set_state(query, target_block)
        if target_block >= 0:
            cursor = QTextCursor(
                self.context_output.document().findBlockByNumber(target_block)
            )
            self.context_output.setTextCursor(cursor)
            self.context_output.centerCursor()

    def context_line_changed(self):
        if self.context_mode != "log":
            return
        block = self.context_output.textCursor().blockNumber()
        if (
            self.displayed_source is None
            or not 0 <= block < len(self.displayed_context)
        ):
            return
        self.context_highlighter.set_state(self.displayed_query, block)
        entry = self.displayed_context[block]
        self.status.setText(
            f"{self.displayed_source.display_name} · line {entry.line_number + 1} selected"
        )

    def show_bookmarks(self):
        if self.results_tabs.currentIndex() == 1:
            self.change_results_tab(1)
        else:
            self.results_tabs.setCurrentIndex(1)

    def save_selected_results(self):
        results = self._results_for_action()
        if not results:
            self.status.setText("Select a result or a line in the open log first.")
            return
        added = self.add_bookmarks(results)
        if added:
            self.status.setText(f"Added {added} bookmark{'s' if added != 1 else ''}.")
        else:
            self.status.setText("The selected line is already bookmarked.")

    def bookmark_current_line(self):
        result = self.current_log_line_result()
        if result is None:
            self.status.setText("Select a line in the open log first.")
            return
        if self.add_bookmarks([result]):
            self.status.setText(
                f"Bookmarked {result.source.display_name}:{result.line_number + 1}."
            )
        else:
            self.status.setText("That line is already bookmarked.")

    def current_log_line_result(self):
        if self.context_mode != "log" or self.displayed_source is None:
            return None
        block = self.context_output.textCursor().blockNumber()
        if not 0 <= block < len(self.displayed_context):
            return None
        entry = self.displayed_context[block]
        radius = self.context_lines.value()
        context_start = max(0, block - radius)
        context_end = min(len(self.displayed_context), block + radius + 1)
        context = self.displayed_context[context_start:context_end]
        return SearchResult(
            source=self.displayed_source,
            line_number=entry.line_number,
            line=entry.text,
            timestamp=timestamp_text(entry.text),
            level=level_for_line(entry.text),
            event_type="",
            context=tuple(context),
            query=SearchQuery(),
            context_radius=radius,
        )

    def add_bookmarks(self, results):
        existing = {(result.source.key, result.line_number) for result in self.bookmarks}
        added = 0
        for result in results:
            key = (result.source.key, result.line_number)
            if key not in existing:
                self.bookmarks.append(result)
                existing.add(key)
                added += 1
        rendering = self.populate_bookmarks()
        if self.results_tabs.currentIndex() == 1 and not rendering:
            self.result_count.setText(
                f"{len(self.bookmarks):,} bookmark{'s' if len(self.bookmarks) != 1 else ''}"
            )
        return added

    def remove_selected_bookmarks(self):
        if self.result_pages.currentWidget() is not self.bookmark_list:
            return
        selected = set()
        for item in self.bookmark_list.selectedItems():
            result = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(result, SearchResult):
                selected.add((result.source.key, result.line_number))
        if not selected:
            self.status.setText("Select one or more bookmarks to remove.")
            return
        self.bookmarks = [
            result
            for result in self.bookmarks
            if (result.source.key, result.line_number) not in selected
        ]
        if self.current_result and (
            self.current_result.source.key,
            self.current_result.line_number,
        ) in selected:
            self.current_result = None
        rendering = self.populate_bookmarks()
        if not rendering:
            self.result_count.setText(
                f"{len(self.bookmarks):,} bookmark{'s' if len(self.bookmarks) != 1 else ''}"
            )
        self.status.setText(
            f"Removed {len(selected)} bookmark{'s' if len(selected) != 1 else ''}."
        )

    def populate_bookmarks(self):
        self.results_tabs.setTabText(1, f"Bookmarks ({len(self.bookmarks):,})")
        return self.populate_result_list(
            self.bookmark_list,
            self.bookmarks,
            None,
            "Bookmarked lines appear here until the application is closed.",
            noun="bookmark",
        )

    def selected_results(self):
        active = self.result_pages.currentWidget()
        if active not in {
            self.result_list,
            self.player_list,
            self.error_list,
            self.bookmark_list,
        }:
            return []

        results = []
        for item in active.selectedItems():
            result = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(result, SearchResult):
                results.append(result)
        return results

    def _results_for_action(self):
        results = self.selected_results()
        if results:
            return results
        if self.context_mode == "log":
            result = self.current_log_line_result()
            return [result] if result else []
        return [self.current_result] if self.current_result else []

    def copy_bookmarks(self):
        results = self._results_for_action()
        if results:
            text = format_results(results, self.include_context.isChecked())
        else:
            text = self.context_output.textCursor().selectedText().replace("\u2029", "\n")
            if not text:
                text = self.context_output.toPlainText()
        if not text.strip():
            self.status.setText("There are no results or bookmarks to copy yet.")
            return
        QApplication.clipboard().setText(text)
        self.status.setText("Copied to the clipboard.")

    def export_bookmarks(self):
        results = self._results_for_action()
        if results:
            text = format_results(results, self.include_context.isChecked())
        else:
            text = self.context_output.toPlainText()
        if not text.strip():
            self.status.setText("There are no results or bookmarks to export yet.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export bookmarks", "log-bookmarks.txt", "Text files (*.txt)"
        )
        if not path:
            return
        try:
            Path(path).write_text(text, encoding="utf-8")
        except OSError as error:
            self.show_error(f"The bookmarks file could not be saved: {error}")
            return
        self.status.setText(f"Bookmarks exported to {Path(path).name}.")

    def move_result_selection(self, direction):
        active = self.result_pages.currentWidget()
        if active not in {
            self.result_list,
            self.player_list,
            self.error_list,
            self.bookmark_list,
        }:
            return
        if not active.count():
            return
        row = active.currentRow()
        row = 0 if row < 0 else max(0, min(active.count() - 1, row + direction))
        active.setCurrentRow(row)
        item = active.item(row)
        if item:
            self.open_result_item(item)

    def select_source(self, source):
        for index in range(self.source_list.count()):
            item = self.source_list.item(index)
            candidate = item.data(Qt.ItemDataRole.UserRole)
            if candidate and candidate.key == source.key:
                self.source_list.blockSignals(True)
                self.source_list.setCurrentRow(index)
                self.source_list.blockSignals(False)
                break

    def selected_source(self):
        item = self.source_list.currentItem()
        source = item.data(Qt.ItemDataRole.UserRole) if item else None
        return source if isinstance(source, LogSource) else None

    def require_selected_source(self):
        if not self.sources:
            self.status.setText("Open a ZIP archive, log file, or folder first.")
            return None
        source = self.selected_source()
        if source is None:
            self.status.setText("Select a log file first.")
        return source

    @staticmethod
    def add_empty_item(widget, message):
        item = QListWidgetItem(message)
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        item.setSizeHint(QSize(0, 72))
        widget.addItem(item)

    def show_empty_workspace(self):
        self.context_title.setText("Log context")
        self.set_context_error_state(False)
        self.context_output.setPlainText(
            "Open a ZIP archive, a log file, or a folder of logs.\n\n"
            "Then select a log and search for a player name, command, chat message, or keyword."
        )
        self.set_context_mode("empty")
        self.populate_result_list(
            self.result_list,
            [],
            SearchQuery(),
            "Enter a keyword or choose a filter to search this log.",
        )
        self.populate_result_list(
            self.player_list,
            [],
            SearchQuery(mode="player"),
            "Enter a player name to list their activity in this log.",
        )
        self.populate_result_list(
            self.error_list,
            [],
            SearchQuery(mode="errors"),
            "Open Errors to list errors and Java stack traces in this log.",
        )
        self.populate_bookmarks()
        self.active_view = "search"
        self.search_nav.setChecked(True)
        self.show_primary_results(self.result_list, "Search results")

    def run_task(self, key, task, finished, message):
        previous = self.tasks.pop(key, None)
        if previous:
            previous.cancel()
        self.tasks[key] = task
        task.signals.progress.connect(
            lambda value, message, task=task, key=key: self.task_progress(
                key, task, value, message
            )
        )
        task.signals.finished.connect(
            lambda payload, task=task, key=key: self.task_finished(
                key, task, finished, payload
            )
        )
        task.signals.failed.connect(
            lambda error, task=task, key=key: self.task_failed(key, task, error)
        )
        self.status.setText(message)
        self.spinner.start()
        self.thread_pool.start(task)

    def cancel_task(self, key):
        task = self.tasks.pop(key, None)
        if task:
            task.cancel()
            self.finish_loading_if_idle()

    def task_progress(self, key, task, _value, message):
        if self.tasks.get(key) is not task:
            return
        self.status.setText(message)

    def task_finished(self, key, task, callback, payload):
        if self.tasks.get(key) is not task:
            return
        self.tasks.pop(key, None)
        self.finish_loading_if_idle()
        callback(payload)

    def task_failed(self, key, task, error):
        if self.tasks.get(key) is not task:
            return
        self.tasks.pop(key, None)
        self.finish_loading_if_idle()
        if key == "context":
            self.set_context_error_state(True)
            self.displayed_context = ()
            self.displayed_source = None
            self.context_highlighter.set_state(SearchQuery(), -1)
            self.context_output.setPlainText(error)
            self.set_context_mode("empty")
        elif key == "search" and self.context_mode == "error-loading":
            self.set_context_error_state(True)
            self.context_output.setPlainText("Error analysis could not be completed.")
            self.set_context_mode("empty")
        self.show_error(error)

    def finish_loading_if_idle(self):
        if not self.tasks and not self.result_render_jobs:
            self.spinner.stop()

    def cancel_all_tasks(self):
        for task in self.tasks.values():
            task.cancel()
        self.tasks.clear()
        self.result_render_jobs.clear()
        self.thread_pool.clear()
        self.spinner.stop()

    def show_error(self, message):
        self.status.setText(message)
        QMessageBox.warning(self, APP_NAME, message)

    def dragEnterEvent(self, event):
        urls = event.mimeData().urls()
        if not urls:
            return
        path = Path(urls[0].toLocalFile())
        if path.is_dir() or path.suffix.lower() in {".zip", ".log", ".txt", ".gz"}:
            event.acceptProposedAction()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if urls:
            self.load_path(urls[0].toLocalFile())
            event.acceptProposedAction()

    def closeEvent(self, event):
        self.cancel_all_tasks()
        event.accept()


def build_stylesheet(theme: str) -> str:
    if theme == "light":
        colors = {
            "bg": "#eef1f6",
            "surface": "#ffffff",
            "surface2": "#f6f7fa",
            "border": "#d5dae5",
            "text": "#242837",
            "muted": "#687188",
            "accent": "#6756df",
            "accent2": "#5545ca",
            "selected": "#e7e3ff",
            "hover": "#eceff5",
            "input": "#ffffff",
            "scroll": "#b8bfce",
            "logo": "transparent",
            "error": "#c42b36",
        }
    else:
        colors = {
            "bg": "#090d16",
            "surface": "#101522",
            "surface2": "#151a29",
            "border": "#242b3d",
            "text": "#d9deeb",
            "muted": "#8e98ad",
            "accent": "#725cf4",
            "accent2": "#5d49d7",
            "selected": "#25213e",
            "hover": "#1a2030",
            "input": "#0d121d",
            "scroll": "#343d52",
            "logo": "transparent",
            "error": "#ff5964",
        }
    return f"""
        QMainWindow, QWidget#appRoot {{ background: {colors['bg']}; color: {colors['text']}; }}
        QWidget {{ color: {colors['text']}; font-size: 10pt; }}
        QFrame#header {{ background: {colors['surface']}; border-bottom: 1px solid {colors['border']}; }}
        QFrame#logoShell {{ background: {colors['logo']}; border: none; border-radius: 5px; }}
        QFrame#fileCard {{ background: {colors['surface2']}; border: 1px solid {colors['border']}; border-radius: 6px; }}
        QLabel#fileCardIcon {{ color: {colors['accent']}; }}
        QLabel#fileCardTitle {{ color: {colors['text']}; font-weight: 600; }}
        QLabel#fileCardMeta {{ color: {colors['muted']}; font-size: 9pt; }}
        QLabel#appTitle {{ font-size: 12pt; font-weight: 600; }}
        QLabel#appSubtitle, QLabel#mutedText {{ color: {colors['muted']}; }}
        QFrame#body {{ background: {colors['bg']}; }}
        QFrame#panel, QFrame#workspace {{ background: {colors['surface']}; border: 1px solid {colors['border']}; border-radius: 6px; }}
        QFrame#workspace {{ border: none; background: transparent; }}
        QFrame#panelHeader {{ background: {colors['surface2']}; border-bottom: 1px solid {colors['border']}; }}
        QLabel#sectionEyebrow {{ color: {colors['muted']}; font-size: 9pt; font-weight: 600; }}
        QLabel#sectionTitle {{ font-size: 10pt; font-weight: 600; }}
        QTabBar#resultsTabs {{ background: transparent; }}
        QTabBar#resultsTabs::tab {{ background: transparent; color: {colors['muted']}; border: none; border-bottom: 2px solid transparent; padding: 7px 12px; }}
        QTabBar#resultsTabs::tab:hover {{ color: {colors['text']}; background: {colors['hover']}; }}
        QTabBar#resultsTabs::tab:selected {{ color: {colors['text']}; border-bottom-color: {colors['accent']}; font-weight: 600; }}
        QFrame#navRail {{ background: {colors['surface']}; border: 1px solid {colors['border']}; border-radius: 6px; }}
        QFrame#navSeparator {{ color: {colors['border']}; margin: 2px 7px; }}
        QToolButton#navButton {{ background: transparent; border: none; border-radius: 5px; color: {colors['muted']}; font-size: 8pt; padding: 3px; }}
        QToolButton#navButton:hover {{ background: {colors['hover']}; color: {colors['text']}; }}
        QToolButton#navButton:checked {{ background: {colors['selected']}; color: {colors['accent']}; border-left: 2px solid {colors['accent']}; }}
        QPushButton {{ min-height: 30px; padding: 0 10px; border-radius: 5px; }}
        QPushButton#primaryButton {{ background: {colors['accent']}; color: white; border: 1px solid {colors['accent']}; font-weight: 600; }}
        QPushButton#primaryButton:hover {{ background: {colors['accent2']}; }}
        QPushButton#headerOpenButton {{ background: {colors['selected']}; color: {colors['accent']}; border: 1px solid {colors['border']}; font-weight: 600; }}
        QPushButton#headerOpenButton:hover {{ background: {colors['hover']}; border-color: {colors['accent']}; }}
        QPushButton#headerOpenButton::menu-indicator {{ subcontrol-origin: padding; subcontrol-position: right center; right: 8px; }}
        QPushButton#secondaryButton {{ background: {colors['surface2']}; color: {colors['text']}; border: 1px solid {colors['border']}; }}
        QPushButton#secondaryButton:hover {{ background: {colors['hover']}; border-color: {colors['muted']}; }}
        QPushButton#secondaryButton:checked {{ background: {colors['selected']}; border-color: {colors['accent']}; }}
        QToolButton#iconButton {{ background: {colors['surface2']}; border: 1px solid {colors['border']}; border-radius: 5px; }}
        QToolButton#iconButton:hover {{ background: {colors['hover']}; border-color: {colors['muted']}; }}
        QToolButton#iconButton:disabled {{ background: {colors['surface2']}; border-color: {colors['border']}; }}
        QPushButton:disabled {{ color: {colors['muted']}; background: {colors['surface2']}; }}
        QLineEdit, QComboBox, QSpinBox {{ background: {colors['input']}; color: {colors['text']}; border: 1px solid {colors['border']}; border-radius: 5px; min-height: 32px; padding: 0 8px; selection-background-color: {colors['accent']}; }}
        QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{ border-color: {colors['accent']}; }}
        QSpinBox#contextSpin {{ padding-right: 27px; }}
        QSpinBox#contextSpin::up-button, QSpinBox#contextSpin::down-button {{ width: 24px; background: {colors['selected']}; border-left: 1px solid {colors['accent']}; color: {colors['text']}; }}
        QSpinBox#contextSpin::up-button {{ subcontrol-origin: border; subcontrol-position: top right; border-bottom: 1px solid {colors['border']}; border-top-right-radius: 4px; }}
        QSpinBox#contextSpin::down-button {{ subcontrol-origin: border; subcontrol-position: bottom right; border-bottom-right-radius: 4px; }}
        QSpinBox#contextSpin::up-button:hover, QSpinBox#contextSpin::down-button:hover {{ background: {colors['accent']}; color: white; }}
        QComboBox::drop-down {{ border: none; width: 24px; }}
        QComboBox QAbstractItemView {{ background: {colors['surface']}; color: {colors['text']}; border: 1px solid {colors['border']}; selection-background-color: {colors['selected']}; }}
        QMenu {{ background: {colors['surface']}; color: {colors['text']}; border: 1px solid {colors['border']}; padding: 5px; }}
        QMenu::item {{ min-width: 170px; padding: 7px 24px 7px 8px; border-radius: 4px; }}
        QMenu::item:selected {{ background: {colors['selected']}; color: {colors['text']}; }}
        QCheckBox {{ spacing: 6px; color: {colors['muted']}; }}
        QCheckBox::indicator {{ width: 15px; height: 15px; }}
        QListWidget {{ background: {colors['surface']}; color: {colors['text']}; border: none; outline: none; }}
        QListWidget#sourceList::item {{ padding: 5px 7px; border-radius: 4px; color: {colors['muted']}; }}
        QListWidget#sourceList::item:hover {{ background: {colors['hover']}; }}
        QListWidget#sourceList::item:selected {{ background: {colors['selected']}; color: {colors['text']}; }}
        QListWidget#resultList::item {{ background: {colors['surface']}; border-bottom: 1px solid {colors['border']}; }}
        QListWidget#resultList::item:hover {{ background: {colors['hover']}; }}
        QListWidget#resultList::item:selected {{ background: {colors['selected']}; border-left: 3px solid {colors['accent']}; }}
        QLabel#resultMeta {{ color: {colors['muted']}; font-size: 9pt; }}
        QLabel#resultLine {{ color: {colors['text']}; font-family: monospace; font-size: 10pt; }}
        QLabel#resultContext {{ color: {colors['muted']}; font-family: monospace; font-size: 9pt; }}
        QPlainTextEdit#contextOutput {{ background: {colors['input']}; color: {colors['text']}; border: none; padding: 9px; selection-background-color: {colors['accent']}; }}
        QPlainTextEdit#contextOutput[errorState="true"] {{ color: {colors['error']}; }}
        QSplitter::handle {{ background: {colors['bg']}; }}
        QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
        QScrollBar::handle:vertical {{ background: {colors['scroll']}; min-height: 28px; border-radius: 4px; }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
        QFrame#footer {{ background: {colors['surface']}; border-top: 1px solid {colors['border']}; }}
        QToolButton#footerImageLink, QToolButton#footerLink {{ background: transparent; color: {colors['muted']}; border: 1px solid transparent; border-radius: 3px; padding: 0; }}
        QToolButton#footerImageLink:hover, QToolButton#footerLink:hover {{ background: {colors['hover']}; color: {colors['accent']}; }}
        QToolButton#footerImageLink:focus, QToolButton#footerLink:focus {{ border-color: {colors['accent']}; }}
        QToolTip {{ background: {colors['surface2']}; color: {colors['text']}; border: 1px solid {colors['border']}; padding: 4px; }}
    """


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_NAME)
    app.setStyle("Fusion")
    window = LogViewer()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

import sys
import gzip
import re
from collections import Counter
from pathlib import Path

from PySide6.QtCore import Qt, QObject, QThread, Signal, QTimer
from PySide6.QtGui import QTextCharFormat, QColor, QFont, QTextCursor, QSyntaxHighlighter, QIcon
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QFileDialog,
    QVBoxLayout, QHBoxLayout, QPushButton, QLineEdit,
    QListWidget, QPlainTextEdit, QTextEdit, QLabel, QComboBox, QSplitter, QProgressBar,
    QCheckBox, QListWidgetItem, QTabWidget
)


def read_log(path):
    path = Path(path)
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
            return f.readlines()
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.readlines()


def matches(line, text, mode, case_sensitive, pattern=None):
    if not text:
        return True
    if mode == "Regular expression":
        return bool(pattern.search(line))

    haystack = line if case_sensitive else line.lower()
    needle = text if case_sensitive else text.lower()
    if mode == "Exact line":
        return haystack.rstrip("\r\n") == needle
    return needle in haystack


class LogLoader(QObject):
    finished = Signal(list)
    failed = Signal(str)

    def __init__(self, path):
        super().__init__()
        self.path = Path(path)

    def run(self):
        try:
            self.finished.emit(read_log(self.path))

        except Exception as e:
            self.failed.emit(str(e))


def asset_path(filename):
    return Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / filename


class LogHighlighter(QSyntaxHighlighter):
    def __init__(self, document):
        super().__init__(document)
        self.marker_format = self.make_format("#55ff55", QFont.Weight.Bold)
        self.error_format = self.make_format("#ff5555")
        self.warning_format = self.make_format("#ffaa00")
        self.info_format = self.make_format("#dddddd")
        self.debug_format = self.make_format("#8888ff")
        self.success_format = self.make_format("#55ff55")
        self.default_format = self.make_format("#cccccc")
        self.search_format = self.make_format("#151515")
        self.search_format.setBackground(QColor("#ffff00"))
        self.search = ""
        self.search_mode = "Contains"
        self.case_sensitive = False
        self.search_pattern = None

    @staticmethod
    def make_format(color, weight=None):
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        if weight is not None:
            fmt.setFontWeight(weight)
        return fmt

    def highlightBlock(self, line):
        if (
            line in {"<=========", "==========>", "START OF LOG", "END OF LOG", "END OF SUMMARY"}
            or line.startswith("ERROR SUMMARY")
        ):
            fmt = self.marker_format
        elif "ERROR" in line or "SEVERE" in line or "Exception" in line:
            fmt = self.error_format
        elif "WARN" in line:
            fmt = self.warning_format
        elif "INFO" in line:
            fmt = self.info_format
        elif "DEBUG" in line:
            fmt = self.debug_format
        elif "Done (" in line:
            fmt = self.success_format
        else:
            fmt = self.default_format
        self.setFormat(0, len(line), fmt)

        if not self.search:
            return

        if self.search_mode == "Regular expression":
            for match in self.search_pattern.finditer(line):
                self.setFormat(match.start(), match.end() - match.start(), self.search_format)
        elif self.search_mode == "Exact line":
            comparable_line = line if self.case_sensitive else line.lower()
            comparable_search = self.search if self.case_sensitive else self.search.lower()
            if comparable_line == comparable_search:
                self.setFormat(0, len(line), self.search_format)
        else:
            comparable_line = line if self.case_sensitive else line.lower()
            comparable_search = self.search if self.case_sensitive else self.search.lower()
            start = 0
            while True:
                start = comparable_line.find(comparable_search, start)
                if start < 0:
                    break
                self.setFormat(start, len(comparable_search), self.search_format)
                start += len(comparable_search)

    def set_search(self, search, mode, case_sensitive, pattern=None):
        self.search = search
        self.search_mode = mode
        self.case_sensitive = case_sensitive
        self.search_pattern = pattern
        self.rehighlight()


class LogOutput(QPlainTextEdit):
    stack_arrow_clicked = Signal(int)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            cursor = self.cursorForPosition(event.position().toPoint())
            text = cursor.block().text()
            if text.endswith((" ▼", " ▲")) and cursor.positionInBlock() >= len(text) - 2:
                self.stack_arrow_clicked.emit(cursor.blockNumber())
                event.accept()
                return
        super().mouseReleaseEvent(event)


class LogViewer(QMainWindow):
    FIRST_LOG_BLOCK = 4

    def __init__(self):
        super().__init__()

        self.setWindowTitle("Log Viewer")
        self.setWindowIcon(QIcon(str(asset_path("log-viewer.ico"))))
        self.resize(1200, 750)

        self.current_lines = []
        self.log_files = []
        self.log_cache = {}
        self.loading_threads = []
        self.current_log_path = None
        self.displayed_lines = []
        self.stack_blocks = {}
        self.expanded_stacks = set()
        self.pending_bookmark = None
        self.flash_generation = 0
        self.line_selection = None
        self.bookmark_flash = None

        root = QWidget()
        self.setCentralWidget(root)

        layout = QVBoxLayout(root)
        top_bar = QHBoxLayout()

        self.open_file_btn = QPushButton("Open Log")
        self.open_folder_btn = QPushButton("Open Folder")

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search logs...")

        self.search_mode = QComboBox()
        self.search_mode.addItems(["Contains", "Exact line", "Regular expression"])

        self.case_sensitive = QCheckBox("Case sensitive")
        self.error_summary_toggle = QCheckBox("Error summary")
        self.fold_stacks = QCheckBox("Fold stacks")
        self.fold_stacks.setChecked(True)
        self.search_all_btn = QPushButton("Search all")

        self.level_filter = QComboBox()
        self.level_filter.addItems(["All", "INFO", "WARN", "ERROR", "DEBUG"])

        top_bar.addWidget(self.open_file_btn)
        top_bar.addWidget(self.open_folder_btn)
        top_bar.addWidget(QLabel("Filter:"))
        top_bar.addWidget(self.level_filter)
        top_bar.addWidget(self.search_box)
        top_bar.addWidget(self.search_mode)
        top_bar.addWidget(self.case_sensitive)
        top_bar.addWidget(self.error_summary_toggle)
        top_bar.addWidget(self.fold_stacks)
        top_bar.addWidget(self.search_all_btn)

        layout.addLayout(top_bar)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(8)

        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)

        self.file_list = QListWidget()
        left_layout.addWidget(QLabel("Log files"))
        left_layout.addWidget(self.file_list, 1)

        self.side_tabs = QTabWidget()

        bookmarks = QWidget()
        bookmark_layout = QVBoxLayout(bookmarks)
        bookmark_layout.setContentsMargins(4, 4, 4, 4)
        bookmark_bar = QHBoxLayout()
        self.add_bookmark_btn = QPushButton("Bookmark selected")
        self.remove_bookmark_btn = QPushButton("Remove")
        bookmark_bar.addWidget(self.add_bookmark_btn)
        bookmark_bar.addWidget(self.remove_bookmark_btn)
        bookmark_layout.addLayout(bookmark_bar)

        self.bookmark_list = QListWidget()
        self.bookmark_list.setWordWrap(True)
        self.bookmark_list.setTextElideMode(Qt.TextElideMode.ElideNone)
        bookmark_layout.addWidget(self.bookmark_list)

        timeline = QWidget()
        timeline_layout = QVBoxLayout(timeline)
        timeline_layout.setContentsMargins(4, 4, 4, 4)
        self.timeline_list = QListWidget()
        timeline_layout.addWidget(self.timeline_list)

        results = QWidget()
        results_layout = QVBoxLayout(results)
        results_layout.setContentsMargins(4, 4, 4, 4)
        self.search_results = QListWidget()
        results_layout.addWidget(self.search_results)

        self.side_tabs.addTab(bookmarks, "Bookmarks")
        self.side_tabs.addTab(timeline, "Timeline")
        self.side_tabs.addTab(results, "Results")
        left_layout.addWidget(self.side_tabs, 2)

        self.log_output = LogOutput()
        self.log_output.setReadOnly(True)
        log_font = QFont("Consolas")
        log_font.setPointSize(10)
        self.log_output.setFont(log_font)
        self.log_highlighter = LogHighlighter(self.log_output.document())
        self.log_output.cursorPositionChanged.connect(self.update_selection)
        self.log_output.stack_arrow_clicked.connect(self.toggle_stack_at)

        self.splitter.addWidget(left_panel)
        self.splitter.addWidget(self.log_output)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 3)
        self.splitter.setSizes([300, 900])

        layout.addWidget(self.splitter, 1)

        bottom_bar = QHBoxLayout()
        self.status = QLabel("Open a log file or folder.")
        self.loading_indicator = QProgressBar()
        self.loading_indicator.setFixedWidth(180)
        self.loading_indicator.setTextVisible(False)
        self.loading_indicator.hide()
        bottom_bar.addWidget(self.status)
        bottom_bar.addStretch()
        bottom_bar.addWidget(self.loading_indicator)
        layout.addLayout(bottom_bar)

        self.open_file_btn.clicked.connect(self.open_file)
        self.open_folder_btn.clicked.connect(self.open_folder)
        self.file_list.currentRowChanged.connect(self.load_selected_file)
        self.search_box.textChanged.connect(self.render_log)
        self.search_mode.currentTextChanged.connect(self.render_log)
        self.case_sensitive.toggled.connect(self.render_log)
        self.level_filter.currentTextChanged.connect(self.render_log)
        self.error_summary_toggle.toggled.connect(self.render_log)
        self.fold_stacks.toggled.connect(self.render_log)
        self.search_all_btn.clicked.connect(self.search_all_logs)
        self.add_bookmark_btn.clicked.connect(self.add_bookmark)
        self.remove_bookmark_btn.clicked.connect(self.remove_selected_bookmark)
        self.bookmark_list.itemClicked.connect(self.go_to_bookmark)
        self.timeline_list.itemClicked.connect(self.go_to_timeline)
        self.search_results.itemClicked.connect(self.go_to_search_result)

    def open_file(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Open Log File",
            "",
            "Log Files (*.log *.txt *.gz);;All Files (*)"
        )

        if file_path:
            self.log_files = [Path(file_path)]
            self.refresh_file_list()
            self.file_list.setCurrentRow(0)

    def open_folder(self):
        folder_path = QFileDialog.getExistingDirectory(self, "Open Logs Folder")

        if not folder_path:
            return

        folder = Path(folder_path)

        self.log_files = sorted(
            list(folder.glob("*.log")) +
            list(folder.glob("*.txt")) +
            list(folder.glob("*.gz")),
            key=lambda p: p.stat().st_mtime,
            reverse=True
        )

        self.refresh_file_list()

        if self.log_files:
            self.file_list.setCurrentRow(0)
        else:
            self.status.setText("No log files found.")

    def refresh_file_list(self):
        self.file_list.clear()

        for file in self.log_files:
            self.file_list.addItem(file.name)

        self.status.setText(f"Loaded {len(self.log_files)} log file(s).")

    def load_selected_file(self, index):
        if index < 0 or index >= len(self.log_files):
            return

        path = self.log_files[index]
        self.current_log_path = path
        self.expanded_stacks = set()

        if path in self.log_cache:
            self.current_lines = self.log_cache[path]
            self.find_stack_blocks()
            self.update_timeline()
            self.render_log()
            return

        self.status.setText(f"Reading {path.name}...")
        self.log_output.setPlainText("Loading log file...")
        self.current_lines = []
        self.loading_indicator.setRange(0, 0)
        self.loading_indicator.show()

        thread = QThread(self)
        worker = LogLoader(path)
        self.loading_threads.append((thread, worker))

        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.finished.connect(self.on_log_loaded)
        worker.failed.connect(self.on_log_failed)

        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)

        worker.finished.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)

        thread.finished.connect(lambda: self.release_loader(thread))
        thread.finished.connect(thread.deleteLater)

        self.file_list.setEnabled(False)
        thread.start()

    def release_loader(self, thread):
        self.loading_threads = [
            (active_thread, worker)
            for active_thread, worker in self.loading_threads
            if active_thread is not thread
        ]
        self.file_list.setEnabled(not self.loading_threads)

    def closeEvent(self, event):
        for thread, _ in self.loading_threads:
            thread.quit()
            thread.wait()
        event.accept()

    def on_log_loaded(self, lines):
        self.current_lines = lines
        self.log_cache[self.current_log_path] = lines
        self.find_stack_blocks()
        self.update_timeline()
        self.status.setText(f"Rendering {len(lines)} lines...")
        self.render_log()

    def on_log_failed(self, error):
        self.current_lines = []
        self.log_output.setPlainText(f"Failed to read file:\n{error}")
        self.status.setText("Failed to load log file.")
        self.loading_indicator.hide()

    def find_stack_blocks(self):
        self.stack_blocks = {}
        index = 0
        while index < len(self.current_lines) - 1:
            line = self.current_lines[index]
            lower = line.lower()
            if not any(word in lower for word in ("error", "severe", "exception", "throwable")):
                index += 1
                continue

            probe_end = min(index + 9, len(self.current_lines))
            has_stack_frame = any(
                self.current_lines[probe].lstrip().startswith("at ")
                for probe in range(index + 1, probe_end)
            )
            if has_stack_frame:
                end = index + 1
                while end < len(self.current_lines):
                    if re.match(r"^\[\d{2}:\d{2}:\d{2}\] \[", self.current_lines[end]):
                        break
                    end += 1
                self.stack_blocks[index] = end
                index = end
            else:
                index += 1

    def update_timeline(self):
        self.timeline_list.clear()
        if self.current_log_path is None:
            return

        stack_lines = {
            index
            for start, end in self.stack_blocks.items()
            for index in range(start + 1, end)
        }
        for index, line in enumerate(self.current_lines):
            if index in stack_lines:
                continue
            lower = line.lower()
            if "starting minecraft server" in lower or "starting net.minecraft.server" in lower:
                event = "Server starting"
            elif "done (" in lower:
                event = "Server ready"
            elif "stopping server" in lower or "stopping the server" in lower:
                event = "Server stopping"
            elif "watchdog" in lower:
                event = "Watchdog"
            elif "could not pass event" in lower:
                event = "Plugin event failure"
            elif "plugin" in lower and ("failed to load" in lower or "could not load" in lower):
                event = "Plugin load failure"
            elif any(word in lower for word in ("crash", "crashed", "fatal error", "outofmemoryerror")):
                event = "Crash"
            elif "severe" in lower:
                event = "Severe"
            elif "error" in lower or "exception" in lower:
                event = "Error"
            elif "warn" in lower:
                event = "Warning"
            else:
                continue

            item = QListWidgetItem(f"{index + 1}: {event} — {line.strip()}")
            item.setData(Qt.ItemDataRole.UserRole, {
                "path": str(self.current_log_path),
                "line_index": index,
            })
            self.timeline_list.addItem(item)

    def add_bookmark(self):
        cursor = self.log_output.textCursor()
        displayed_index = cursor.blockNumber() - self.FIRST_LOG_BLOCK
        if (
            self.error_summary_toggle.isChecked()
            or displayed_index < 0
            or displayed_index >= len(self.displayed_lines)
            or self.current_log_path is None
        ):
            self.status.setText("Select a log line before adding a bookmark.")
            return

        block = self.displayed_lines[displayed_index]
        if block["fold"] is not None:
            self.status.setText("Expand the stack trace before bookmarking a line in it.")
            return

        source_index = block["source"]
        line = self.current_lines[source_index].rstrip("\r\n")
        item = QListWidgetItem(line)
        item.setToolTip(line)
        item.setData(Qt.ItemDataRole.UserRole, {
            "path": str(self.current_log_path),
            "line_index": source_index,
        })
        self.bookmark_list.addItem(item)
        self.status.setText("Bookmark added.")

    def remove_selected_bookmark(self):
        row = self.bookmark_list.currentRow()
        if row >= 0:
            self.bookmark_list.takeItem(row)

    def toggle_stack_at(self, block_number):
        displayed_index = block_number - self.FIRST_LOG_BLOCK
        if displayed_index < 0 or displayed_index >= len(self.displayed_lines):
            return

        start = self.displayed_lines[displayed_index]["fold"]
        if start is None:
            return
        if start in self.expanded_stacks:
            self.expanded_stacks.remove(start)
        else:
            self.expanded_stacks.add(start)
        self.render_log()

    def go_to_bookmark(self, item):
        self.open_line(item.data(Qt.ItemDataRole.UserRole))

    def go_to_timeline(self, item):
        self.open_line(item.data(Qt.ItemDataRole.UserRole))

    def go_to_search_result(self, item):
        self.open_line(item.data(Qt.ItemDataRole.UserRole))

    def open_line(self, line_info):
        self.pending_bookmark = line_info

        self.search_box.blockSignals(True)
        self.level_filter.blockSignals(True)
        self.error_summary_toggle.blockSignals(True)
        self.search_box.clear()
        self.level_filter.setCurrentText("All")
        self.error_summary_toggle.setChecked(False)
        self.search_box.blockSignals(False)
        self.level_filter.blockSignals(False)
        self.error_summary_toggle.blockSignals(False)

        log_path = Path(self.pending_bookmark["path"])
        if log_path != self.current_log_path:
            try:
                self.file_list.setCurrentRow(self.log_files.index(log_path))
            except ValueError:
                self.status.setText("The log file is no longer available.")
                self.pending_bookmark = None
        else:
            self.render_log()

    def jump_to_bookmark(self):
        if self.pending_bookmark is None:
            return
        if Path(self.pending_bookmark["path"]) != self.current_log_path:
            return

        source_index = self.pending_bookmark["line_index"]
        for start, end in self.stack_blocks.items():
            if start <= source_index < end:
                self.expanded_stacks.add(start)
                break

        if any(block["source"] == source_index for block in self.displayed_lines) is False:
            self.render_log()
            return

        try:
            displayed_index = next(
                index for index, block in enumerate(self.displayed_lines)
                if block["source"] == source_index
            )
        except ValueError:
            self.status.setText("The bookmarked line is not in the current view.")
            self.pending_bookmark = None
            return

        cursor = QTextCursor(
            self.log_output.document().findBlockByNumber(displayed_index + self.FIRST_LOG_BLOCK)
        )
        cursor.select(QTextCursor.SelectionType.LineUnderCursor)
        self.log_output.setTextCursor(cursor)
        self.log_output.centerCursor()

        selection = QTextEdit.ExtraSelection()
        selection.cursor = cursor
        selection.format.setBackground(QColor("#55aaff"))
        self.bookmark_flash = selection
        self.refreshSelections()
        self.flash_generation += 1
        flash_generation = self.flash_generation
        QTimer.singleShot(1200, lambda: self.clear_flash(flash_generation))
        self.pending_bookmark = None

    def clear_flash(self, flash_generation):
        if flash_generation == self.flash_generation:
            self.bookmark_flash = None
            self.refreshSelections()

    def update_selection(self):
        cursor = self.log_output.textCursor()
        displayed_index = cursor.blockNumber() - self.FIRST_LOG_BLOCK
        if displayed_index < 0 or displayed_index >= len(self.displayed_lines):
            self.line_selection = None
        else:
            selection = QTextEdit.ExtraSelection()
            line_cursor = QTextCursor(cursor)
            line_cursor.select(QTextCursor.SelectionType.LineUnderCursor)
            selection.cursor = line_cursor
            selection.format.setBackground(QColor("#2b3d55"))
            self.line_selection = selection
        self.refreshSelections()

    def refreshSelections(self):
        selections = [
            selection for selection in (
                self.line_selection,
                self.bookmark_flash,
            ) if selection is not None
        ]
        self.log_output.setExtraSelections(selections)

    def search_all_logs(self):
        search = self.search_box.text()
        if not search:
            self.status.setText("Enter search text before searching all logs.")
            return

        mode = self.search_mode.currentText()
        case_sensitive = self.case_sensitive.isChecked()
        try:
            pattern = re.compile(search, 0 if case_sensitive else re.IGNORECASE) \
                if mode == "Regular expression" else None
        except re.error as error:
            self.status.setText(f"Invalid regular expression: {error}")
            return

        self.search_results.clear()
        count = 0
        limit = 2000
        for path in self.log_files:
            try:
                lines = self.log_cache.get(path)
                if lines is None:
                    lines = read_log(path)
                    self.log_cache[path] = lines
            except OSError as error:
                self.status.setText(f"Could not read {path.name}: {error}")
                continue

            for line_number, line in enumerate(lines):
                if not matches(line, search, mode, case_sensitive, pattern):
                    continue
                item = QListWidgetItem(f"{path.name}:{line_number + 1}  {line.strip()}")
                item.setToolTip(line.rstrip("\r\n"))
                item.setData(Qt.ItemDataRole.UserRole, {
                    "path": str(path),
                    "line_index": line_number,
                })
                self.search_results.addItem(item)
                count += 1
                if count == limit:
                    self.status.setText(f"Showing the first {limit} matches.")
                    self.side_tabs.setCurrentWidget(self.search_results.parentWidget())
                    return

        self.status.setText(f"Found {count} match(es) in {len(self.log_files)} log file(s).")
        self.side_tabs.setCurrentWidget(self.search_results.parentWidget())

    def render_log(self):
        old_source = None
        old_block = self.log_output.textCursor().blockNumber() - self.FIRST_LOG_BLOCK
        if 0 <= old_block < len(self.displayed_lines):
            old_source = self.displayed_lines[old_block]["source"]
        scroll_position = self.log_output.verticalScrollBar().value()

        search = self.search_box.text()
        selected_level = self.level_filter.currentText()
        search_mode = self.search_mode.currentText()
        case_sensitive = self.case_sensitive.isChecked()

        try:
            search_pattern = (
                re.compile(search, 0 if case_sensitive else re.IGNORECASE)
                if search and search_mode == "Regular expression"
                else None
            )
        except re.error as error:
            self.log_highlighter.set_search("", "Contains", False)
            self.log_output.setPlainText(f"Invalid regular expression:\n{error}")
            self.status.setText("Invalid regular expression.")
            return

        self.log_highlighter.set_search(
            search, search_mode, case_sensitive, search_pattern
        )

        entries = [
            (index, line) for index, line in enumerate(self.current_lines)
            if matches(line, search, search_mode, case_sensitive, search_pattern)
            and (selected_level == "All" or selected_level in line)
        ]
        if self.error_summary_toggle.isChecked():
            errors = [
                line.rstrip("\r\n") for _, line in entries
                if "ERROR" in line or "SEVERE" in line or "Exception" in line
            ]
            counts = Counter(errors)
            log_text = "".join(
                f"{count}x  {line}\n"
                for line, count in counts.most_common()
            ) or "No error lines match the current filters.\n"
            header = "ERROR SUMMARY (turn off Error summary to view full log)"
            self.displayed_lines = []
        else:
            header = "START OF LOG"
            shown_lines = []
            self.displayed_lines = []
            fold_stacks = self.fold_stacks.isChecked() and not search and selected_level == "All"
            entry = 0
            while entry < len(entries):
                source_index, line = entries[entry]
                end = self.stack_blocks.get(source_index)
                if fold_stacks and end and source_index not in self.expanded_stacks:
                    title = line.rstrip("\r\n")
                    shown_lines.append(f"{title} ({end - source_index} lines) ▼\n")
                    self.displayed_lines.append({"source": source_index, "fold": source_index})
                    entry += end - source_index
                    continue

                if fold_stacks and end:
                    title = line.rstrip("\r\n")
                    shown_lines.append(f"{title} ▲\n")
                    self.displayed_lines.append({"source": source_index, "fold": source_index})
                    entry += 1
                    continue

                shown_lines.append(line)
                fold_start = source_index if end else None
                self.displayed_lines.append({"source": source_index, "fold": fold_start})
                entry += 1
            log_text = "".join(shown_lines)
        if log_text and not log_text.endswith("\n"):
            log_text += "\n"
        footer = "END OF SUMMARY" if self.error_summary_toggle.isChecked() else "END OF LOG"
        self.log_output.setPlainText(
            f"<=========\n{header}\n==========>\n\n"
            f"{log_text}"
            f"\n<=========\n{footer}\n==========>"
        )
        if old_source is not None:
            for index, block in enumerate(self.displayed_lines):
                if block["source"] == old_source:
                    cursor = QTextCursor(
                        self.log_output.document().findBlockByNumber(index + self.FIRST_LOG_BLOCK)
                    )
                    self.log_output.setTextCursor(cursor)
                    break
        self.log_output.verticalScrollBar().setValue(scroll_position)
        self.status.setText(f"Loaded {len(self.current_lines)} lines.")
        self.loading_indicator.hide()
        self.jump_to_bookmark()

def main():
    app = QApplication(sys.argv)
    app.setFont(QFont("Segoe UI", 10))
    app.setWindowIcon(QIcon(str(asset_path("log-viewer.ico"))))

    app.setStyleSheet("""
        QWidget {
            background-color: #151515;
            color: #eeeeee;
        }

        QPlainTextEdit {
            background-color: #0f0f0f;
            color: #dddddd;
            border: 1px solid #333333;
        }

        QListWidget {
            background-color: #111111;
            border: 1px solid #333333;
        }

        QLineEdit {
            background-color: #111111;
            color: #eeeeee;
            border: 1px solid #444444;
            padding: 6px;
        }

        QPushButton {
            background-color: #252525;
            border: 1px solid #444444;
            padding: 6px 10px;
        }

        QPushButton:hover {
            background-color: #333333;
        }

        QComboBox {
            background-color: #111111;
            border: 1px solid #444444;
            padding: 5px;
        }

        QSplitter::handle {
            background-color: #444444;
        }

        QSplitter::handle:hover {
            background-color: #666666;
        }
    """)

    window = LogViewer()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()

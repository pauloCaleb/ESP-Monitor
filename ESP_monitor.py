#!/usr/bin/env python3
"""ESP Monitor — monitor serial com interface gráfica para os logs do ESP-IDF.

Uso:
    python ESP_monitor.py [--port COM5] [--baud 115200] [arquivo.log]
"""

# @package ESP_monitor
#  PySide6 GUI of the ESP Monitor (entry point main()).
#
#  The module docstring above is also the argparse --help text, so it stays in
#  Portuguese like the rest of the user interface; this block documents the code.
#
#  Threading model: the GUI thread owns MainWindow, LogModel and LogTable. Each
#  connection starts one serial_reader.SerialReader (a QThread) that reads the
#  port, parses lines with log_parser.LineParser, appends them to the continuous
#  .log and emits them in batches through Qt signals (queued into the GUI thread).
#  Filters (level, TAG, text/regex search) are applied in LogModel; the memory
#  limit (LogModel.max_lines, up to 5 000 000 lines) trims only the screen, never
#  the recording. Ports are listed with pyserial (COMx on Windows, /dev/tty* on
#  Linux, where the user needs the dialout group).

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSettings, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QFontDatabase, QKeySequence, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableView,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QGroupBox,
)
from serial.tools import list_ports

from log_parser import ALL_LEVELS, LEVEL_NAMES, OTHER, LineParser, format_pc_time, parse_saved_line
from serial_reader import SerialReader

## Window and dialog title.
APP_NAME = "ESP Monitor"
## Default baud rate: CONFIG_ESP_CONSOLE_UART_BAUDRATE of the ESP sdkconfig.
DEFAULT_BAUD = 115200  # CONFIG_ESP_CONSOLE_UART_BAUDRATE of ESP
## Choices of the baud combo (it is editable, any integer is accepted).
BAUD_RATES = ["9600", "57600", "115200", "230400", "460800", "921600", "1500000", "2000000"]
## Default in-memory (on-screen) line limit; the GUI allows 10 000 to 5 000 000 and remembers it.
DEFAULT_MAX_LINES = 200_000
## TAG value of lines without a TAG.
NO_TAG = ""
## Label shown in the TAG list for NO_TAG.
NO_TAG_LABEL = "(sem TAG)"

# Dark terminal palette; E/W/I follow the esp_log ANSI colours.
## Table background colour.
BG = QColor("#1e1e1e")
## Default text colour (D, V and uncoloured lines).
FG = QColor("#d4d4d4")
## Text colour of each level.
LEVEL_COLORS = {
    "E": QColor("#f14c4c"),  # ANSI 31 (red)
    "W": QColor("#e5c07b"),  # ANSI 33 (yellow)
    "I": QColor("#4ec94e"),  # ANSI 32 (green)
    "D": FG,  # no colour in ESP-IDF
    "V": FG,  # no colour in ESP-IDF
    OTHER: FG,
}
## Text colour of OTHER lines that carry their own ANSI colour (30-37).
ANSI_COLORS = {
    30: QColor("#7f7f7f"),
    31: LEVEL_COLORS["E"],
    32: LEVEL_COLORS["I"],
    33: LEVEL_COLORS["W"],
    34: QColor("#3b8eea"),
    35: QColor("#d670d6"),
    36: QColor("#29b8db"),
    37: QColor("#e5e5e5"),
}
## Background of rows matching the search (highlight mode).
HIGHLIGHT_BG = QColor("#4b3d00")

## Table column titles (shown in Portuguese).
COLUMNS = ("Hora PC", "Nível", "Tempo ESP", "TAG", "Mensagem")
## Column indexes of COLUMNS (COL_PC, COL_LEVEL, COL_ESP, COL_TAG, COL_MSG).
COL_PC, COL_LEVEL, COL_ESP, COL_TAG, COL_MSG = range(len(COLUMNS))


class LogModel(QAbstractTableModel):
    """Table model that keeps every received entry in memory and exposes only those accepted by the filters.

    `entries` holds all lines still in memory (oldest first); `visible` is the
    filtered subset shown by the view, in the same order. New batches are
    appended incrementally (beginInsertRows); a filter change rebuilds `visible`
    with a model reset (refilter()).

    On-screen limit: when `entries` exceeds `max_lines` (DEFAULT_MAX_LINES,
    adjustable in the GUI from 10 000 to 5 000 000 lines) the oldest lines are
    dropped from memory and from the view. This limit does not affect the
    continuous recording: SerialReader writes every line to the .log before the
    batch reaches this model.

    All methods run in the GUI thread only.
    """

    def __init__(self, parent=None) -> None:
        """Create an empty model with every level and TAG enabled and no search.

        @param parent  Qt parent (the MainWindow).
        """
        super().__init__(parent)
        ## Every entry still in memory, oldest first (LogEntry).
        self.entries: list = []
        ## Entries accepted by the filters, in the same order (the table rows).
        self.visible: list = []
        ## Memory limit in lines (see _trim()).
        self.max_lines = DEFAULT_MAX_LINES
        ## Number of entries dropped by the memory limit since the last clear().
        self.trimmed = 0
        ## Entries in memory per level.
        self.level_counts = dict.fromkeys(ALL_LEVELS, 0)
        ## Entries in memory per TAG; keys are every TAG seen since start.
        self.tag_counts: dict[str, int] = {}
        # Filters
        ## Levels shown (level checkboxes).
        self.enabled_levels = set(ALL_LEVELS)
        ## TAGs hidden (unchecked in the TAG list).
        self.hidden_tags: set[str] = set()
        ## Compiled search pattern, None without search.
        self.search: re.Pattern | None = None
        ## True: the search filters rows ("Mostrar só resultados"); False: it only highlights.
        self.search_only_matches = False

    # -- Qt -----------------------------------------------------------------
    def rowCount(self, parent=QModelIndex()) -> int:
        """Qt override: number of visible (filtered) rows.

        @param parent  Parent index; the model is flat, so a valid parent has 0 rows.
        @return len(visible), or 0 for a valid parent.
        """
        return 0 if parent.isValid() else len(self.visible)

    def columnCount(self, parent=QModelIndex()) -> int:
        """Qt override: number of columns (COLUMNS).

        @param parent  Parent index; a valid parent has 0 columns.
        @return len(COLUMNS), or 0 for a valid parent.
        """
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        """Qt override: horizontal header titles from COLUMNS.

        @param section      Column number.
        @param orientation  Only Qt.Horizontal has titles.
        @param role         Only Qt.DisplayRole is answered.
        @return The column title, or None.
        """
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return COLUMNS[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        """Qt override: cell contents and styling for a visible entry.

        - DisplayRole: PC time ("hh:mm:ss.mmm"), level, ESP time, TAG or message.
        - ForegroundRole: colour of the level (LEVEL_COLORS); OTHER lines keep their
          own ANSI colour (ANSI_COLORS) when they had one.
        - BackgroundRole: HIGHLIGHT_BG on lines matching the search when
          "Mostrar só resultados" is off (the regex runs at paint time).
        - ToolTipRole on the message column: the whole raw line.
        - TextAlignmentRole: level and ESP time centred.

        @param index  Cell index (row in `visible`).
        @param role   Qt item data role.
        @return The value for the role, or None.
        """
        if not index.isValid():
            return None
        e = self.visible[index.row()]
        col = index.column()
        if role == Qt.DisplayRole:
            if col == COL_PC:
                return format_pc_time(e.pc_time)[11:]
            if col == COL_LEVEL:
                return e.level
            if col == COL_ESP:
                return e.esp_time
            if col == COL_TAG:
                return e.tag
            return e.msg
        if role == Qt.ForegroundRole:
            if e.level == OTHER and e.color is not None:
                return ANSI_COLORS.get(e.color, FG)
            return LEVEL_COLORS[e.level]
        if role == Qt.BackgroundRole:
            if self.search and not self.search_only_matches and self.search.search(e.raw):
                return HIGHLIGHT_BG
            return None
        if role == Qt.ToolTipRole and col == COL_MSG:
            return e.raw
        if role == Qt.TextAlignmentRole and col in (COL_LEVEL, COL_ESP):
            return int(Qt.AlignCenter)
        return None

    # -- filter -------------------------------------------------------------
    def accepts(self, e) -> bool:
        """Return whether an entry passes the current filters.

        An entry is shown when its level is enabled, its TAG is not hidden and, if
        "Mostrar só resultados" is on, the search pattern matches its raw text.

        @param e  LogEntry to test.
        @return True if the entry must be visible.
        """
        if e.level not in self.enabled_levels or e.tag in self.hidden_tags:
            return False
        if self.search and self.search_only_matches:
            return self.search.search(e.raw) is not None
        return True

    def refilter(self) -> None:
        """Rebuild `visible` from all entries after a filter change (model reset).

        @note O(number of entries in memory); the reset also clears the view's
              selection and current row.
        @return None.
        """
        self.beginResetModel()
        accepts = self.accepts
        self.visible = [e for e in self.entries if accepts(e)]
        self.endResetModel()

    def refresh_highlight(self) -> None:
        """Repaint every visible row after the search pattern changed without changing the filter (highlight only).

        @return None.
        """
        if self.visible:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.visible) - 1, len(COLUMNS) - 1))

    # -- data ----------------------------------------------------------------
    def append(self, batch: list) -> list[str]:
        """Append a batch of entries, update the counters, show the accepted ones and apply the memory limit.

        @param batch  LogEntry list (from SerialReader.entries_ready or from a file being loaded).
        @return TAGs seen for the first time (the GUI adds them to the TAG list).
        """
        new_tags = []
        for e in batch:
            self.level_counts[e.level] += 1
            if e.tag not in self.tag_counts:
                self.tag_counts[e.tag] = 0
                new_tags.append(e.tag)
            self.tag_counts[e.tag] += 1
        self.entries.extend(batch)
        accepted = [e for e in batch if self.accepts(e)]
        if accepted:
            first = len(self.visible)
            self.beginInsertRows(QModelIndex(), first, first + len(accepted) - 1)
            self.visible.extend(accepted)
            self.endInsertRows()
        self._trim()
        return new_tags

    def _trim(self) -> None:
        """Drop the oldest entries when `entries` exceeds `max_lines`.

        Removes the excess plus 10 % of `max_lines` in one go, so the cost is not
        paid on every batch; updates the level/TAG counters and `trimmed`, and
        removes the matching rows from `visible` by comparing LogEntry.seq.

        @note A lower `max_lines` set in the GUI only takes effect on the next
              append(), not immediately.
        @return None.
        """
        excess = len(self.entries) - self.max_lines
        if excess <= 0:
            return
        # Remove in blocks (10 %) so the cost is not paid on every batch
        n = excess + self.max_lines // 10
        removed, self.entries = self.entries[:n], self.entries[n:]
        self.trimmed += len(removed)
        for e in removed:
            self.level_counts[e.level] -= 1
            self.tag_counts[e.tag] -= 1
        cutoff = removed[-1].seq
        k = 0
        while k < len(self.visible) and self.visible[k].seq <= cutoff:
            k += 1
        if k:
            self.beginRemoveRows(QModelIndex(), 0, k - 1)
            del self.visible[:k]
            self.endRemoveRows()

    def clear(self) -> None:
        """Remove every entry (the "Limpar tela" action).

        Counters are zeroed but the known TAGs are kept (their list items stay in the
        GUI with count 0); filters are not changed.

        @return None.
        """
        self.beginResetModel()
        self.entries.clear()
        self.visible.clear()
        self.trimmed = 0
        self.level_counts = dict.fromkeys(ALL_LEVELS, 0)
        self.tag_counts = {t: 0 for t in self.tag_counts}
        self.endResetModel()


class LogTable(QTableView):
    """Table view of the log: monospace font, dark palette, row selection and a "Copiar linhas" action (Ctrl+C)."""
    def __init__(self, model: LogModel, parent=None) -> None:
        """Configure the view for a LogModel.

        Fixed row height, no grid, no word wrap, column widths sized for typical
        contents, last column (message) stretched, copy action in the context menu.

        @param model   The LogModel to show.
        @param parent  Qt parent.
        """
        super().__init__(parent)
        self.setModel(model)
        font = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        font.setPointSize(max(font.pointSize(), 10))
        self.setFont(font)
        fm = self.fontMetrics()
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.setTextElideMode(Qt.ElideRight)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        vh = self.verticalHeader()
        vh.hide()
        vh.setSectionResizeMode(QHeaderView.Fixed)
        vh.setDefaultSectionSize(fm.height() + 4)
        hh = self.horizontalHeader()
        hh.setStretchLastSection(True)
        hh.setSectionResizeMode(QHeaderView.Interactive)
        widths = {COL_PC: "00:00:00.000", COL_LEVEL: "Nível", COL_ESP: "0000000000", COL_TAG: "TAG_GRANDE_XX"}
        for col, sample in widths.items():
            self.setColumnWidth(col, fm.horizontalAdvance(sample) + 20)
        pal = self.palette()
        pal.setColor(QPalette.Base, BG)
        pal.setColor(QPalette.Text, FG)
        self.setPalette(pal)

        copy = QAction("Copiar linhas", self)
        copy.setShortcut(QKeySequence.Copy)
        copy.setShortcutContext(Qt.WidgetShortcut)
        # Ctrl+C (when the table has focus) and the context menu copy the selected rows.
        copy.triggered.connect(self.copy_selection)
        self.addAction(copy)
        self.setContextMenuPolicy(Qt.ActionsContextMenu)

    def copy_selection(self) -> None:
        """Copy the raw text of the selected rows (without PC time) to the clipboard, one per line, in row order.

        @return None.
        """
        model: LogModel = self.model()
        rows = sorted({i.row() for i in self.selectionModel().selectedRows()})
        if rows:
            QApplication.clipboard().setText("\n".join(model.visible[r].raw for r in rows))


class MainWindow(QMainWindow):
    """Main window of the ESP Monitor: connection and recording toolbars, level/TAG filter panel, search bar and log table.

    Owns at most one SerialReader at a time (`reader`, None when disconnected).
    The reader's signals are connected to on_entries(), on_status(),
    on_record_file() and on_reader_finished(), which Qt runs in the GUI thread.
    A 500 ms QTimer refreshes the level/TAG counters and the status bar.
    Preferences (port, baud, reconnect, recording folder and options, memory
    limit, last save folder) are kept with QSettings("ESP", "ESP_monitor")
    (registry on Windows, ~/.config/ESP/ESP_monitor.conf on Linux).
    """
    def __init__(self, args) -> None:
        """Build the window and apply the command line options.

        @param args  argparse namespace from main(): `port` and `baud` preselect the
                     connection (they do not connect automatically); `file` is opened
                     right after the event loop starts.
        """
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1300, 800)
        ## Persistent preferences.
        self.settings = QSettings("ESP", "ESP_monitor")
        ## Running serial reader thread, None when disconnected.
        self.reader: SerialReader | None = None
        ## Current recording file ("" when not recording).
        self.record_path = ""

        ## Log model (all entries and filters).
        self.model = LogModel(self)
        self.model.max_lines = int(self.settings.value("max_lines", DEFAULT_MAX_LINES))
        ## Log table view.
        self.table = LogTable(self.model)

        self._build_toolbars()
        self._build_filter_panel()
        self._build_search_bar()
        self._build_menu()

        center = QWidget()
        lay = QVBoxLayout(center)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.addWidget(self.search_bar)
        lay.addWidget(self.table)
        splitter = QSplitter()
        splitter.addWidget(self.filter_panel)
        splitter.addWidget(center)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([240, 1060])
        self.setCentralWidget(splitter)

        ## Status bar: connection status (on_status()).
        self.status_conn = QLabel("Desconectado")
        ## Status bar: visible / total / dropped lines.
        self.status_counts = QLabel()
        ## Status bar: recording file.
        self.status_record = QLabel()
        sb = self.statusBar()
        sb.addWidget(self.status_conn, 1)
        sb.addPermanentWidget(self.status_record)
        sb.addPermanentWidget(self.status_counts)

        ## Refreshes the counters every 500 ms.
        self.counts_timer = QTimer(self)
        self.counts_timer.timeout.connect(self._update_counts)
        self.counts_timer.start(500)

        self.refresh_ports()
        if args.port:
            self.port_combo.setCurrentText(args.port)
        if args.baud:
            self.baud_combo.setCurrentText(str(args.baud))
        if args.file:
            QTimer.singleShot(0, lambda: self.load_file(Path(args.file)))

    # -- UI construction ---------------------------------------------------
    def _build_toolbars(self) -> None:
        """Create the "Conexão" toolbar (port, refresh, baud, connect, auto-reconnect, clear, auto-scroll) and the "Gravação" toolbar (recording folder, PC time option, memory limit 10 000 to 5 000 000 lines).

        The default recording folder is ~/ESP_logs.

        @return None.
        """
        tb = QToolBar("Conexão")
        tb.setMovable(False)
        self.addToolBar(tb)

        tb.addWidget(QLabel(" Porta: "))
        ## Port selector (editable; item data = device name).
        self.port_combo = QComboBox()
        self.port_combo.setEditable(True)
        self.port_combo.setMinimumWidth(260)
        tb.addWidget(self.port_combo)
        refresh = QToolButton()
        refresh.setText("⟳")
        refresh.setToolTip("Atualizar lista de portas")
        refresh.clicked.connect(self.refresh_ports)
        tb.addWidget(refresh)

        tb.addWidget(QLabel("  Baud: "))
        ## Baud rate selector (editable).
        self.baud_combo = QComboBox()
        self.baud_combo.setEditable(True)
        self.baud_combo.addItems(BAUD_RATES)
        self.baud_combo.setCurrentText(str(self.settings.value("baud", DEFAULT_BAUD)))
        tb.addWidget(self.baud_combo)

        ## Connect / disconnect button.
        self.connect_btn = QPushButton("Conectar")
        self.connect_btn.setMinimumWidth(110)
        self.connect_btn.clicked.connect(self.toggle_connection)
        tb.addSeparator()
        tb.addWidget(self.connect_btn)

        ## "Reconectar automaticamente" option.
        self.reconnect_chk = QCheckBox("Reconectar automaticamente")
        self.reconnect_chk.setToolTip("Reabre a porta se o dongle reiniciar ou a USB cair")
        self.reconnect_chk.setChecked(self.settings.value("auto_reconnect", True, type=bool))
        tb.addSeparator()
        tb.addWidget(self.reconnect_chk)

        tb.addSeparator()
        clear_btn = QPushButton("Limpar tela")
        clear_btn.clicked.connect(self.clear)
        tb.addWidget(clear_btn)
        ## "Auto-scroll" option.
        self.autoscroll_chk = QCheckBox("Auto-scroll")
        self.autoscroll_chk.setToolTip("Acompanha o fim do log enquanto a barra de rolagem estiver no final")
        self.autoscroll_chk.setChecked(True)
        tb.addWidget(self.autoscroll_chk)

        self.addToolBarBreak()
        rec = QToolBar("Gravação")
        rec.setMovable(False)
        self.addToolBar(rec)
        ## "Gravação contínua" option.
        self.record_chk = QCheckBox("Gravação contínua em:")
        self.record_chk.setToolTip("Grava cada linha recebida em um .log novo a cada conexão")
        self.record_chk.setChecked(self.settings.value("record", True, type=bool))
        rec.addWidget(self.record_chk)
        ## Recording folder (default ~/ESP_logs).
        self.record_dir_edit = QLineEdit(
            self.settings.value("record_dir", str(Path.home() / "ESP_logs"))
        )
        self.record_dir_edit.setMinimumWidth(350)
        rec.addWidget(self.record_dir_edit)
        browse = QPushButton("…")
        browse.setToolTip("Escolher pasta")
        browse.clicked.connect(self.choose_record_dir)
        rec.addWidget(browse)
        ## "Incluir hora do PC no arquivo" option (recording and save).
        self.pc_time_chk = QCheckBox("Incluir hora do PC no arquivo")
        self.pc_time_chk.setChecked(self.settings.value("record_pc_time", True, type=bool))
        rec.addSeparator()
        rec.addWidget(self.pc_time_chk)

        rec.addSeparator()
        rec.addWidget(QLabel(" Limite em memória: "))
        ## Memory limit spin box (10 000 to 5 000 000 lines).
        self.max_lines_spin = QSpinBox()
        self.max_lines_spin.setRange(10_000, 5_000_000)
        self.max_lines_spin.setSingleStep(50_000)
        self.max_lines_spin.setGroupSeparatorShown(True)
        self.max_lines_spin.setSuffix(" linhas")
        self.max_lines_spin.setValue(self.model.max_lines)
        self.max_lines_spin.valueChanged.connect(self._set_max_lines)
        rec.addWidget(self.max_lines_spin)

    def _build_filter_panel(self) -> None:
        """Create the left panel: one checkbox per level (with counter) and the TAG list.

        The TAG list has a text filter and the "Todas", "Nenhuma" and "Só seleção"
        buttons; each TAG is a checkable item, unchecked = hidden.

        @return None.
        """
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(4, 4, 4, 4)

        lv_box = QGroupBox("Níveis")
        lv_lay = QVBoxLayout(lv_box)
        ## Level checkbox by level letter.
        self.level_checks: dict[str, QCheckBox] = {}
        for lv in ALL_LEVELS:
            chk = QCheckBox()
            chk.setChecked(True)
            color = LEVEL_COLORS[lv] if lv in "EWI" else self.palette().color(QPalette.WindowText)
            chk.setStyleSheet(f"QCheckBox {{ color: {color.name()}; font-weight: bold; }}")
            chk.toggled.connect(self._levels_changed)
            lv_lay.addWidget(chk)
            self.level_checks[lv] = chk
        lay.addWidget(lv_box)

        tag_box = QGroupBox("TAGs")
        tag_lay = QVBoxLayout(tag_box)
        ## Text filter of the TAG list.
        self.tag_search = QLineEdit()
        self.tag_search.setPlaceholderText("Procurar TAG…")
        self.tag_search.setClearButtonEnabled(True)
        self.tag_search.textChanged.connect(self._filter_tag_list)
        tag_lay.addWidget(self.tag_search)
        btns = QHBoxLayout()
        all_btn = QPushButton("Todas")
        all_btn.clicked.connect(lambda: self._set_all_tags(True))
        none_btn = QPushButton("Nenhuma")
        none_btn.clicked.connect(lambda: self._set_all_tags(False))
        only_btn = QPushButton("Só seleção")
        only_btn.setToolTip("Mostra apenas as TAGs selecionadas na lista")
        only_btn.clicked.connect(self._only_selected_tags)
        btns.addWidget(all_btn)
        btns.addWidget(none_btn)
        btns.addWidget(only_btn)
        tag_lay.addLayout(btns)
        ## TAG list (checkable items, sorted).
        self.tag_list = QListWidget()
        self.tag_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tag_list.setSortingEnabled(True)
        # Check state changes show/hide TAGs (also fired by counter text updates, see _update_counts()).
        self.tag_list.itemChanged.connect(self._tag_item_changed)
        tag_lay.addWidget(self.tag_list)
        ## TAG list item by TAG.
        self.tag_items: dict[str, QListWidgetItem] = {}
        lay.addWidget(tag_box, 1)
        ## Left panel widget (levels and TAGs).
        self.filter_panel = panel

    def _build_search_bar(self) -> None:
        """Create the search bar: text/regex field, "Regex", "Diferenciar maiúsc.", "Mostrar só resultados", previous/next buttons and hit counter.

        Typing is debounced by a 250 ms single-shot timer before _apply_search();
        toggling an option applies the search at once; Enter goes to the next hit.

        @return None.
        """
        bar = QWidget()
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(QLabel("Buscar:"))
        ## Search text field.
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Texto ou expressão regular (Ctrl+F)")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.returnPressed.connect(lambda: self.find(+1))
        lay.addWidget(self.search_edit, 1)
        ## "Regex" option.
        self.regex_chk = QCheckBox("Regex")
        ## "Diferenciar maiúsc." option.
        self.case_chk = QCheckBox("Diferenciar maiúsc.")
        ## "Mostrar só resultados" option.
        self.only_matches_chk = QCheckBox("Mostrar só resultados")
        self.only_matches_chk.setToolTip("Desmarcado: destaca as linhas encontradas sem esconder as demais")
        for w in (self.regex_chk, self.case_chk, self.only_matches_chk):
            lay.addWidget(w)
        prev_btn = QToolButton()
        prev_btn.setText("▲")
        prev_btn.setToolTip("Anterior (Shift+F3)")
        prev_btn.clicked.connect(lambda: self.find(-1))
        next_btn = QToolButton()
        next_btn.setText("▼")
        next_btn.setToolTip("Próximo (F3 / Enter)")
        next_btn.clicked.connect(lambda: self.find(+1))
        lay.addWidget(prev_btn)
        lay.addWidget(next_btn)
        ## Hit counter label.
        self.search_info = QLabel()
        self.search_info.setMinimumWidth(120)
        lay.addWidget(self.search_info)

        ## 250 ms debounce of the search field.
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(250)
        self.search_timer.timeout.connect(self._apply_search)
        self.search_edit.textChanged.connect(self.search_timer.start)
        for chk in (self.regex_chk, self.case_chk, self.only_matches_chk):
            chk.toggled.connect(self._apply_search)
        ## Search bar widget.
        self.search_bar = bar

    def _build_menu(self) -> None:
        """Create the menus: Arquivo (open Ctrl+O, save all Ctrl+S, save visible Ctrl+Shift+S, quit), Exibir (search Ctrl+F, F3 / Shift+F3, clear Ctrl+L, end Ctrl+End) and Ajuda.

        @return None.
        """
        m = self.menuBar().addMenu("&Arquivo")
        act = m.addAction("Abrir log…", self.open_file)
        act.setShortcut(QKeySequence.Open)
        act = m.addAction("Salvar tudo como .log…", lambda: self.save_file(only_visible=False))
        act.setShortcut(QKeySequence.Save)
        act = m.addAction("Salvar linhas visíveis (filtradas)…", lambda: self.save_file(only_visible=True))
        act.setShortcut(QKeySequence("Ctrl+Shift+S"))
        m.addSeparator()
        m.addAction("Sair", self.close).setShortcut(QKeySequence.Quit)

        m = self.menuBar().addMenu("&Exibir")
        act = m.addAction("Buscar", lambda: (self.search_edit.setFocus(), self.search_edit.selectAll()))
        act.setShortcut(QKeySequence.Find)
        act = m.addAction("Próximo resultado", lambda: self.find(+1))
        act.setShortcut(QKeySequence("F3"))
        act = m.addAction("Resultado anterior", lambda: self.find(-1))
        act.setShortcut(QKeySequence("Shift+F3"))
        m.addSeparator()
        m.addAction("Limpar tela", self.clear).setShortcut(QKeySequence("Ctrl+L"))
        m.addAction("Ir para o fim", self.table.scrollToBottom).setShortcut(QKeySequence("Ctrl+End"))

        m = self.menuBar().addMenu("A&juda")
        m.addAction("Sobre", self.about)

    # -- connection ------------------------------------------------------------
    def refresh_ports(self) -> None:
        """Fill the port combo with the serial ports found by pyserial (list_ports.comports()).

        Items are shown as "{device} — {description}" ("COM5 — USB-SERIAL CH340" on
        Windows, "/dev/ttyUSB0 — CP2102 ..." on Linux) and keep the device name as
        item data. The previous selection (or the saved port) is reselected, or kept
        as free text when the port is not present now.

        @return None.
        """
        current = self.port_combo.currentData() or self.port_combo.currentText() or self.settings.value("port", "")
        self.port_combo.clear()
        for p in sorted(list_ports.comports(), key=lambda p: p.device):
            label = p.device if not p.description or p.description == "n/a" else f"{p.device} — {p.description}"
            self.port_combo.addItem(label, p.device)
        idx = self.port_combo.findData(current)
        if idx >= 0:
            self.port_combo.setCurrentIndex(idx)
        elif current:
            self.port_combo.setEditText(current)

    def _selected_port(self) -> str:
        """Return the device name of the port to open.

        @return The item data when a listed item is selected, otherwise the typed
                text up to " — " (so a device name typed by hand also works).
        """
        idx = self.port_combo.currentIndex()
        text = self.port_combo.currentText().strip()
        if idx >= 0 and self.port_combo.itemText(idx) == text:
            return self.port_combo.itemData(idx)
        return text.split(" — ")[0].strip()

    def toggle_connection(self) -> None:
        """Slot of the "Conectar"/"Desconectar" button.

        @return None.
        """
        if self.reader:
            self.disconnect_serial()
        else:
            self.connect_serial()

    def connect_serial(self) -> None:
        """Start a SerialReader for the selected port and baud rate.

        The recording folder is used only when "Gravação contínua" is checked.
        Connection and recording widgets are disabled while the reader runs, and
        the settings are saved. Opening the port happens in the worker thread; errors
        arrive later through on_status().

        @return None.
        """
        port = self._selected_port()
        if not port:
            QMessageBox.warning(self, APP_NAME, "Selecione uma porta serial.")
            return
        try:
            baud = int(self.baud_combo.currentText())
        except ValueError:
            QMessageBox.warning(self, APP_NAME, "Baud rate inválido.")
            return
        record_dir = Path(self.record_dir_edit.text()).expanduser() if self.record_chk.isChecked() else None
        ## New reader thread for this connection; its signals are queued to the GUI thread.
        self.reader = SerialReader(
            port, baud, self.reconnect_chk.isChecked(), record_dir, self.pc_time_chk.isChecked(), self
        )
        self.reader.entries_ready.connect(self.on_entries)
        self.reader.status_changed.connect(self.on_status)
        self.reader.record_file_changed.connect(self.on_record_file)
        self.reader.finished.connect(self.on_reader_finished)
        self.reader.start()
        self._set_connected_ui(True)
        self._save_settings()

    def disconnect_serial(self) -> None:
        """Ask the reader to stop and wait for it (up to 3 s, blocking the GUI).

        The UI is reset by on_reader_finished() when QThread.finished arrives.

        @warning If the thread does not finish within 3 s (e.g. a driver blocking
                 in close()), closeEvent() continues and Qt may destroy a running
                 QThread.
        @return None.
        """
        if self.reader:
            self.reader.stop()
            self.reader.wait(3000)

    def on_reader_finished(self) -> None:
        """Slot of QThread.finished: forget the reader and re-enable the connection widgets.

        Also runs when the reader ends by itself (open error or lost connection
        without auto-reconnect).

        @return None.
        """
        self.reader = None
        self._set_connected_ui(False)

    def _set_connected_ui(self, running: bool) -> None:
        """Switch the connect button text and lock the connection/recording widgets while connected.

        @param running  True while a reader is running.
        @return None.
        """
        self.connect_btn.setText("Desconectar" if running else "Conectar")
        for w in (self.port_combo, self.baud_combo, self.reconnect_chk, self.record_chk,
                  self.record_dir_edit, self.pc_time_chk):
            w.setEnabled(not running)

    def on_status(self, msg: str, connected: bool) -> None:
        """Slot of SerialReader.status_changed: show the status text with a green (open) or yellow (not open) dot.

        @param msg        Status text.
        @param connected  Whether the port is open.
        @return None.
        """
        color = "#4ec94e" if connected else "#e5c07b"
        self.status_conn.setText(f"<span style='color:{color}'>●</span> {msg}")

    def on_record_file(self, path: str) -> None:
        """Slot of SerialReader.record_file_changed: show the recording file in the status bar.

        @param path  Path of the file being recorded, or "" when recording stopped.
        @return None.
        """
        self.record_path = path
        self.status_record.setText(f"Gravando: {path}" if path else "")
        self.status_record.setToolTip(path)

    # -- data ----------------------------------------------------------------
    def on_entries(self, batch: list) -> None:
        """Slot of SerialReader.entries_ready (also used by load_file()): add a batch to the model.

        New TAGs are added to the TAG list. Auto-scroll follows the end of the log
        only when the option is on and the scroll bar was already at the bottom
        before the batch, so scrolling up pauses it.

        @param batch  LogEntry list.
        @return None.
        """
        sb = self.table.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 2
        new_tags = self.model.append(batch)
        for tag in new_tags:
            self._add_tag_item(tag)
        if self.autoscroll_chk.isChecked() and at_bottom:
            self.table.scrollToBottom()

    def _update_counts(self) -> None:
        """Timer slot (every 500 ms): refresh the level and TAG counters and the "visíveis / linhas / descartadas" status.

        @bug (novo) QListWidgetItem.setText() on a TAG item whose counter changed
             emits QListWidget.itemChanged, which is connected to
             _tag_item_changed(), so every tick runs one LogModel.refilter() (full
             model reset over all entries in memory) per TAG that received lines.
             With hundreds of thousands of lines this freezes the GUI periodically
             and the reset also clears the selected row. It does not lose data.

        @return None.
        """
        m = self.model
        for lv, chk in self.level_checks.items():
            chk.setText(f"{lv}  {LEVEL_NAMES[lv]} ({m.level_counts[lv]})")
        for tag, item in self.tag_items.items():
            item.setText(f"{tag or NO_TAG_LABEL} ({m.tag_counts.get(tag, 0)})")
        trimmed = f" · {m.trimmed} descartadas da memória" if m.trimmed else ""
        self.status_counts.setText(f"{len(m.visible)} visíveis / {len(m.entries)} linhas{trimmed}")

    def clear(self) -> None:
        """Clear the screen (model) and the counters; the continuous recording is not affected.

        @return None.
        """
        self.model.clear()
        self._update_counts()

    def _set_max_lines(self, value: int) -> None:
        """Slot of the memory-limit spin box: set LogModel.max_lines and save it.

        @param value  New limit in lines (10 000 to 5 000 000).
        @return None.
        """
        self.model.max_lines = value
        self.settings.setValue("max_lines", value)

    # -- filters ------------------------------------------------------------
    def _levels_changed(self) -> None:
        """Slot of the level checkboxes: apply the enabled levels, refilter and scroll to the end.

        @return None.
        """
        self.model.enabled_levels = {lv for lv, c in self.level_checks.items() if c.isChecked()}
        self.model.refilter()
        self.table.scrollToBottom()

    def _add_tag_item(self, tag: str) -> None:
        """Add a checkable item for a TAG seen for the first time.

        The item is unchecked if the TAG is already hidden, and hidden in the list if
        it does not match the TAG search text. Signals are blocked while the check
        state is set so no refilter happens.

        @param tag  TAG text ("" is shown as NO_TAG_LABEL).
        @return None.
        """
        item = QListWidgetItem(tag or NO_TAG_LABEL)
        item.setData(Qt.UserRole, tag)
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
        self.tag_list.blockSignals(True)
        item.setCheckState(Qt.Unchecked if tag in self.model.hidden_tags else Qt.Checked)
        self.tag_list.addItem(item)
        self.tag_list.blockSignals(False)
        self.tag_items[tag] = item
        needle = self.tag_search.text().lower()
        item.setHidden(bool(needle) and needle not in (tag or NO_TAG_LABEL).lower())

    def _tag_item_changed(self, item: QListWidgetItem) -> None:
        """Slot of QListWidget.itemChanged: show or hide a TAG according to its check state and refilter.

        @param item  Changed item (its UserRole data is the TAG).
        @note Also triggered by the counter text updates of _update_counts() (see the @bug there).
        @return None.
        """
        tag = item.data(Qt.UserRole)
        if item.checkState() == Qt.Checked:
            self.model.hidden_tags.discard(tag)
        else:
            self.model.hidden_tags.add(tag)
        self.model.refilter()

    def _set_tags_checked(self, predicate) -> None:
        """Check or uncheck every TAG item at once and refilter a single time.

        @param predicate  Callable (tag, item) -> bool; True keeps the TAG visible.
        @return None.
        """
        self.tag_list.blockSignals(True)
        hidden = set()
        for tag, item in self.tag_items.items():
            checked = predicate(tag, item)
            item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
            if not checked:
                hidden.add(tag)
        self.tag_list.blockSignals(False)
        self.model.hidden_tags = hidden
        self.model.refilter()

    def _set_all_tags(self, checked: bool) -> None:
        """Slot of "Todas" / "Nenhuma": show or hide every TAG.

        @param checked  True to show all TAGs, False to hide all.
        @return None.
        """
        self._set_tags_checked(lambda tag, item: checked)

    def _only_selected_tags(self) -> None:
        """Slot of "Só seleção": show only the TAGs selected (highlighted) in the list; does nothing when none is selected.

        @return None.
        """
        selected = {i.data(Qt.UserRole) for i in self.tag_list.selectedItems()}
        if selected:
            self._set_tags_checked(lambda tag, item: tag in selected)

    def _filter_tag_list(self, text: str) -> None:
        """Slot of the TAG search field: hide list items whose TAG does not contain the text (case-insensitive); does not filter the log.

        @param text  Text typed in the field.
        @return None.
        """
        needle = text.lower()
        for tag, item in self.tag_items.items():
            item.setHidden(bool(needle) and needle not in (tag or NO_TAG_LABEL).lower())

    # -- search ---------------------------------------------------------------
    def _apply_search(self) -> None:
        """Compile the search text and apply it as highlight or as filter.

        The text is a regular expression when "Regex" is checked, otherwise a literal
        (re.escape); case-insensitive unless "Diferenciar maiúsc." is checked. An
        invalid regex paints the field red and clears the search. The model is
        refiltered only when "Mostrar só resultados" is on (or was just toggled);
        otherwise the rows are just repainted. The hit counter counts the visible
        rows whose raw text matches.

        @return None.
        """
        text = self.search_edit.text()
        pattern = None
        error = False
        if text:
            flags = 0 if self.case_chk.isChecked() else re.IGNORECASE
            try:
                pattern = re.compile(text if self.regex_chk.isChecked() else re.escape(text), flags)
            except re.error:
                error = True
        self.search_edit.setStyleSheet("QLineEdit { border: 1px solid #f14c4c; }" if error else "")
        self.search_edit.setToolTip("Expressão regular inválida" if error else "")
        m = self.model
        filter_changed = m.search_only_matches != self.only_matches_chk.isChecked() or (
            self.only_matches_chk.isChecked() and m.search != pattern
        )
        m.search = pattern
        m.search_only_matches = self.only_matches_chk.isChecked()
        if filter_changed:
            m.refilter()
            self.table.scrollToBottom()
        else:
            m.refresh_highlight()
        if pattern:
            hits = sum(1 for e in m.visible if pattern.search(e.raw))
            self.search_info.setText(f"{hits} resultado(s)")
        else:
            self.search_info.setText("")

    def find(self, direction: int) -> None:
        """Select and centre the next or previous visible row that matches the search, wrapping around.

        @param direction  +1 for next (F3, Enter), -1 for previous (Shift+F3).
        @return None.
        """
        m = self.model
        if not m.search or not m.visible:
            return
        cur = self.table.currentIndex().row()
        n = len(m.visible)
        start = cur if cur >= 0 else (-1 if direction > 0 else n)
        for step in range(1, n + 1):
            row = (start + direction * step) % n
            if m.search.search(m.visible[row].raw):
                idx = m.index(row, COL_MSG)
                self.table.setCurrentIndex(idx)
                self.table.scrollTo(idx, QAbstractItemView.PositionAtCenter)
                return

    # -- files --------------------------------------------------------------
    def choose_record_dir(self) -> None:
        """Slot of the "…" button: pick the continuous recording folder.

        @return None.
        """
        d = QFileDialog.getExistingDirectory(self, "Pasta de gravação", self.record_dir_edit.text())
        if d:
            self.record_dir_edit.setText(d)

    def save_file(self, only_visible: bool) -> None:
        """Save the lines in memory (all, or only the visible ones) to a .log chosen by the user.

        The PC time prefix follows the "Incluir hora do PC no arquivo" option; the
        file is UTF-8 with LF endings, the same format as the continuous recording.
        When lines were already dropped by the memory limit the user is told that the
        complete log is in the continuous recording file.

        @param only_visible  True for "Salvar linhas visíveis (filtradas)", False for "Salvar tudo".
        @return None.
        """
        entries = self.model.visible if only_visible else self.model.entries
        if not entries:
            QMessageBox.information(self, APP_NAME, "Não há linhas para salvar.")
            return
        default = Path(self.settings.value("save_dir", str(Path.home()))) / (
            f"ESP_{entries[0].pc_time:%Y%m%d_%H%M%S}{'_filtrado' if only_visible else ''}.log"
        )
        path, _ = QFileDialog.getSaveFileName(self, "Salvar log", str(default), "Log (*.log *.txt);;Todos (*)")
        if not path:
            return
        with_pc = self.pc_time_chk.isChecked()
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as fp:
                for e in entries:
                    fp.write(e.to_text(with_pc) + "\n")
        except OSError as exc:
            QMessageBox.critical(self, APP_NAME, f"Erro ao salvar:\n{exc}")
            return
        self.settings.setValue("save_dir", str(Path(path).parent))
        msg = f"{len(entries)} linhas salvas em {path}"
        if not only_visible and self.model.trimmed:
            msg += (f"\n\nAtenção: {self.model.trimmed} linhas mais antigas já tinham sido descartadas da "
                    "memória. O log completo está no arquivo da gravação contínua.")
        QMessageBox.information(self, APP_NAME, msg)

    def open_file(self) -> None:
        """Slot of "Abrir log…": choose a .log/.txt file and load it.

        @return None.
        """
        path, _ = QFileDialog.getOpenFileName(
            self, "Abrir log", self.settings.value("save_dir", str(Path.home())), "Log (*.log *.txt);;Todos (*)"
        )
        if path:
            self.load_file(Path(path))

    def load_file(self, path: Path) -> None:
        """Replace the screen contents with the lines of a saved .log file.

        Refused while a serial reader is running. Lines are parsed with
        parse_saved_line() in the GUI thread and added in batches of 5000, calling
        QApplication.processEvents() between batches to keep the window responsive.
        Current level/TAG/search filters apply to the loaded lines and the memory
        limit still applies (a file bigger than it keeps only its end).

        @param path  File to open (UTF-8; invalid bytes are replaced).
        @return None.
        """
        if self.reader:
            QMessageBox.warning(self, APP_NAME, "Desconecte a serial antes de abrir um arquivo.")
            return
        self.clear()
        parser = LineParser()
        batch = []
        try:
            with open(path, encoding="utf-8", errors="replace") as fp:
                for line in fp:
                    batch.append(parse_saved_line(parser, line))
                    if len(batch) >= 5000:
                        self.on_entries(batch)
                        batch = []
                        QApplication.processEvents()
        except OSError as exc:
            QMessageBox.critical(self, APP_NAME, f"Erro ao abrir:\n{exc}")
            return
        if batch:
            self.on_entries(batch)
        self.setWindowTitle(f"{APP_NAME} — {path.name}")
        self.on_status(f"Arquivo: {path}", False)
        self._update_counts()

    # -- misc ---------------------------------------------------------------
    def about(self) -> None:
        """Show the "Sobre" dialog with the colour legend.

        @return None.
        """
        QMessageBox.about(
            self,
            APP_NAME,
            "<b>ESP Monitor</b><br>Monitor serial para logs do ESP-IDF (esp_log).<br><br>"
            "Cores: <span style='color:#f14c4c'>E erro</span>, <span style='color:#e5c07b'>W aviso</span>, "
            "<span style='color:#4ec94e'>I info</span>, D/V sem cor (como no ESP-IDF).",
        )

    def _save_settings(self) -> None:
        """Store the connection and recording options in QSettings (called on connect and on close).

        @return None.
        """
        s = self.settings
        s.setValue("port", self._selected_port())
        s.setValue("baud", self.baud_combo.currentText())
        s.setValue("auto_reconnect", self.reconnect_chk.isChecked())
        s.setValue("record", self.record_chk.isChecked())
        s.setValue("record_dir", self.record_dir_edit.text())
        s.setValue("record_pc_time", self.pc_time_chk.isChecked())

    def closeEvent(self, event) -> None:
        """Qt override: save the settings and stop the serial reader before the window closes.

        @param event  Close event (always accepted).
        @return None.
        """
        self._save_settings()
        self.disconnect_serial()
        super().closeEvent(event)


def apply_dark_theme(app: QApplication) -> None:
    """Apply the Fusion style with a dark palette matching the terminal colours (BG/FG).

    @param app  The application instance.
    @return None.
    """
    app.setStyle("Fusion")
    pal = QPalette()
    pal.setColor(QPalette.Window, QColor("#2b2b2b"))
    pal.setColor(QPalette.WindowText, FG)
    pal.setColor(QPalette.Base, BG)
    pal.setColor(QPalette.AlternateBase, QColor("#262626"))
    pal.setColor(QPalette.Text, FG)
    pal.setColor(QPalette.Button, QColor("#3a3a3a"))
    pal.setColor(QPalette.ButtonText, FG)
    pal.setColor(QPalette.ToolTipBase, QColor("#3a3a3a"))
    pal.setColor(QPalette.ToolTipText, FG)
    pal.setColor(QPalette.Highlight, QColor("#264f78"))
    pal.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    pal.setColor(QPalette.PlaceholderText, QColor("#808080"))
    pal.setColor(QPalette.Disabled, QPalette.Text, QColor("#7a7a7a"))
    pal.setColor(QPalette.Disabled, QPalette.ButtonText, QColor("#7a7a7a"))
    pal.setColor(QPalette.Disabled, QPalette.WindowText, QColor("#7a7a7a"))
    app.setPalette(pal)


def main() -> int:
    """Command line entry point: parse --port, --baud and an optional .log file, then run the Qt event loop.

    @return The exit code of QApplication.exec().
    """
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="porta serial (ex.: COM5, /dev/ttyUSB0)")
    ap.add_argument("--baud", type=int, help=f"baud rate (padrão {DEFAULT_BAUD})")
    ap.add_argument("file", nargs="?", help="arquivo .log para abrir")
    args = ap.parse_args()

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    apply_dark_theme(app)
    win = MainWindow(args)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

"""Parser for the ESP-IDF (esp_log) log lines printed by an ESP32 running ESP-IDF.

Pure Python (no Qt): it is shared by the serial reader thread, by the GUI when
a saved .log is reopened, and by the unit tests in tests/test_log_parser.py.

ESP-IDF line format with CONFIG_LOG_COLORS=y (the ESP-IDF default):

    \\033[0;32mI (1234) TAG: message\\033[0m

- Level letter: E (error), W (warning), I (info), D (debug), V (verbose).
- Timestamp between parentheses: milliseconds since boot
  (CONFIG_LOG_TIMESTAMP_SOURCE_RTOS, the ESP-IDF default) or system time
  "hh:mm:ss.mmm" (CONFIG_LOG_TIMESTAMP_SOURCE_SYSTEM); both are accepted.
- TAG: everything up to the first ':' (a space after ':' is optional, so
  "wifi:wifi driver task: ..." gives TAG "wifi").
- ANSI colours: E = 31 (red), W = 33 (yellow), I = 32 (green); D and V are
  printed without colour. The colour stays active until the reset sequence,
  so the continuation lines of a multi-line message carry it.

Lines outside this format (ROM/bootloader output, printf, panic backtraces,
continuation lines of multi-line messages) get level OTHER ("-"), unless the
ANSI colour active at the start of the line maps to E/W/I, or the line
contains one of PANIC_MARKERS (then level "E" and TAG "PANIC").
All ANSI escape sequences are stripped from the stored text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from itertools import count

## ESP-IDF log level letters, most to least severe.
LEVELS = ("E", "W", "I", "D", "V")
## Pseudo-level of lines that are not in the esp_log format (ROM, printf, ...).
OTHER = "-"
## Every level shown by the GUI filter (LEVELS + OTHER).
ALL_LEVELS = LEVELS + (OTHER,)

## Display name (Portuguese, shown in the GUI) of each level.
LEVEL_NAMES = {
    "E": "Erro",
    "W": "Aviso",
    "I": "Info",
    "D": "Debug",
    "V": "Verbose",
    OTHER: "Outros",
}

## ANSI foreground colour used by esp_log for each coloured level (see esp_log_color.h in ESP-IDF).
ANSI_COLOR_BY_LEVEL = {"E": 31, "W": 33, "I": 32}
## Reverse map: ANSI colour code -> level letter (used for lines without a level prefix).
LEVEL_BY_ANSI_COLOR = {v: k for k, v in ANSI_COLOR_BY_LEVEL.items()}

## SGR (colour) escape sequence; group 1 holds the ';'-separated parameters.
ANSI_RE = re.compile(r"\x1b\[([0-9;]*)m")
## Other CSI escape sequences (cursor movement etc., any final letter except 'm') that are only removed.
ANSI_OTHER_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-ln-z]")

## esp_log line without ANSI codes: "L (timestamp) TAG: message" -> groups level, ts, tag, msg.
LOG_RE = re.compile(
    r"^(?P<level>[EWIDV]) "
    r"\((?P<ts>[0-9:.\- ]+)\) "
    r"(?P<tag>[^:\s][^:]*?):\s?"
    r"(?P<msg>.*)$"
)

## strftime/strptime format of the PC time prefix (truncated to milliseconds when written).
PC_TIME_FMT = "%Y-%m-%d %H:%M:%S.%f"
## Optional "[YYYY-MM-DD hh:mm:ss.mmm] " prefix of the .log files written by this monitor.
PC_PREFIX_RE = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3})\] ?(.*)$")

## Substrings that mark a line (outside the esp_log format) as a panic/abort report: level "E", TAG "PANIC".
PANIC_MARKERS = (
    "Guru Meditation Error",
    "abort() was called",
    "Backtrace:",
    "panic'ed",
    "assert failed",
    "Stack smashing protect failure",
    "CORRUPT HEAP",
)

## Global sequence counter: gives every LogEntry a unique, increasing seq (used by LogModel._trim()).
_seq = count()


@dataclass(slots=True)
class LogEntry:
    """One parsed log line, as stored by the GUI model and written to .log files.

    Instances are created by LineParser.parse() in the serial reader thread and
    handed to the GUI thread through a Qt signal; they are never mutated after
    creation.
    """

    ## PC time when the line was received (or the time read from the .log prefix).
    pc_time: datetime
    ## Level letter from LEVELS, or OTHER.
    level: str
    ## Timestamp text between parentheses ("" when the line is not in the esp_log format).
    esp_time: str
    ## esp_log TAG, "PANIC" for panic lines, "" otherwise.
    tag: str
    ## Message after "TAG: " (the whole line text for lines outside the esp_log format).
    msg: str
    ## Whole line without ANSI codes (what is searched, copied and saved).
    raw: str
    ## ANSI colour (30-37) of an OTHER line, so the GUI can keep it; None for levelled lines.
    color: int | None = None
    ## Unique increasing sequence number (from _seq), used to trim the GUI model in order.
    seq: int = field(default_factory=lambda: next(_seq))

    def to_text(self, with_pc_time: bool = True) -> str:
        """Format the entry as one line of a .log file (without the trailing newline).

        @param with_pc_time  Prefix the line with "[YYYY-MM-DD hh:mm:ss.mmm] " (PC time).
        @return The line text without ANSI codes.
        """
        if with_pc_time:
            return f"[{format_pc_time(self.pc_time)}] {self.raw}"
        return self.raw


def format_pc_time(t: datetime) -> str:
    """Format a PC timestamp as "YYYY-MM-DD hh:mm:ss.mmm" (PC_TIME_FMT truncated to milliseconds).

    @param t  Time to format.
    @return The formatted text; the GUI shows only the "hh:mm:ss.mmm" part (characters 11 onwards).
    """
    return t.strftime(PC_TIME_FMT)[:-3]


def strip_ansi(text: str) -> str:
    """Remove every ANSI escape sequence (colours and other CSI sequences) from a text.

    @param text  Line as received from the serial port.
    @return The text without escape sequences.
    """
    return ANSI_OTHER_RE.sub("", ANSI_RE.sub("", text))


def _last_color(text: str, current: int | None) -> int | None:
    """Return the ANSI foreground colour active at the end of a text.

    Walks every SGR sequence of the text: parameter 0 (reset) or 39 (default
    colour) clears the colour, 30-37 set it and the bright variants 90-97 are
    folded to 30-37. Other parameters (bold etc.) are ignored. An empty
    sequence ("ESC[m") counts as a reset.

    @param text     Text that may contain ANSI sequences.
    @param current  Colour active before the text (None = default colour).
    @return The colour code 30-37 active after the text, or None.
    """
    for m in ANSI_RE.finditer(text):
        params = [p for p in m.group(1).split(";") if p] or ["0"]
        for p in params:
            n = int(p)
            if n == 0 or n == 39:
                current = None
            elif 30 <= n <= 37 or 90 <= n <= 97:
                current = n if n < 90 else n - 60
    return current


class LineParser:
    """Stateful line parser that tracks the ANSI colour carried across lines.

    ESP-IDF multi-line messages keep their colour until the reset sequence at
    the end of the message, so continuation lines without a level prefix
    inherit the level from the colour active when they start.

    One instance per input stream: the serial reader owns one (reset on every
    (re)connection) and the GUI creates a new one for each .log it opens. Not
    thread-safe; it is only used by the thread that owns it.
    """

    def __init__(self) -> None:
        """Create a parser with no active colour."""
        ## ANSI colour active at the end of the previous line (None = default colour).
        self._color: int | None = None

    def reset(self) -> None:
        """Forget the colour carried from previous lines (called after a reconnection).

        @return None.
        """
        ## Forget the carried colour.
        self._color = None

    def parse(self, line: str, pc_time: datetime | None = None) -> LogEntry:
        """Parse one line into a LogEntry and update the carried colour.

        Lines in the esp_log format (LOG_RE, after stripping ANSI codes) get
        level, timestamp, TAG and message from the text. Other lines get the
        level of the colour active at the start of the line (the carried colour,
        or the first SGR sequence when the line starts with one), OTHER when
        that colour is not E/W/I, and are forced to level "E" / TAG "PANIC"
        when they contain one of PANIC_MARKERS. Trailing CR/LF are removed.

        @param line     Decoded line, possibly with ANSI codes, without the '\\n'.
        @param pc_time  PC timestamp to store; None uses datetime.now().
        @return The new LogEntry (with a fresh seq number).
        """
        pc_time = pc_time or datetime.now()
        start_color = self._color
        # The colour of the first ANSI code at the start of the line (if any) defines the line
        first = ANSI_RE.match(line)
        line_color = _last_color(first.group(0), start_color) if first else start_color
        self._color = _last_color(line, start_color)

        text = strip_ansi(line).rstrip("\r\n")
        m = LOG_RE.match(text)
        if m:
            return LogEntry(
                pc_time=pc_time,
                level=m.group("level"),
                esp_time=m.group("ts"),
                tag=m.group("tag").strip(),
                msg=m.group("msg"),
                raw=text,
            )

        level = LEVEL_BY_ANSI_COLOR.get(line_color, OTHER)
        tag = ""
        if any(marker in text for marker in PANIC_MARKERS):
            level, tag = "E", "PANIC"
        return LogEntry(
            pc_time=pc_time,
            level=level,
            esp_time="",
            tag=tag,
            msg=text,
            raw=text,
            color=line_color if level == OTHER else None,
        )


def parse_saved_line(parser: LineParser, line: str) -> LogEntry:
    """Parse one line of a saved .log file (with or without the PC time prefix).

    When the line starts with "[YYYY-MM-DD hh:mm:ss.mmm] " that time becomes
    LogEntry.pc_time; otherwise (or when the prefix is not a valid date, e.g.
    "[2026-13-45 ...]") the current time is used and the whole line is parsed. Saved files contain
    no ANSI codes, so continuation lines of multi-line messages come back as
    OTHER.

    @param parser  Parser of the file being loaded (keeps state between lines).
    @param line    Line read from the file (trailing CR/LF allowed).
    @return The parsed LogEntry.
    """
    line = line.rstrip("\r\n")
    m = PC_PREFIX_RE.match(line)
    if m:
        try:
            pc_time = datetime.strptime(m.group(1), PC_TIME_FMT)
        except ValueError:
            return parser.parse(line)
        return parser.parse(m.group(2), pc_time)
    return parser.parse(line)

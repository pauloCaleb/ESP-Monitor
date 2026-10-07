"""Unit tests of log_parser (pure Python, no Qt and no serial port needed).

Run from tools/ealive_monitor with: python -m pytest tests
"""

import sys
from datetime import datetime
from pathlib import Path

# Make log_parser importable without installing the tool (tests/ -> tools/ealive_monitor/).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from log_parser import OTHER, LineParser, parse_saved_line  # noqa: E402

## ANSI SGR sequences as printed by esp_log (green = I, red = E, yellow = W, reset).
GREEN, RED, YELLOW, RESET = "\x1b[0;32m", "\x1b[0;31m", "\x1b[0;33m", "\x1b[0m"


def test_colored_levels():
    """Coloured E/W/I lines give level, ESP time, TAG and message, and `raw` has no ANSI codes."""
    p = LineParser()
    e = p.parse(f"{GREEN}I (1234) TUF_CLIENT: Root carregado{RESET}")
    assert (e.level, e.esp_time, e.tag, e.msg) == ("I", "1234", "TUF_CLIENT", "Root carregado")
    assert e.raw == "I (1234) TUF_CLIENT: Root carregado"
    assert p.parse(f"{RED}E (5) CAN: bus off{RESET}").level == "E"
    assert p.parse(f"{YELLOW}W (6) UDS: timeout{RESET}").level == "W"


def test_uncolored_debug_and_verbose():
    """D and V lines (printed without colour by ESP-IDF) are recognised by the level letter."""
    p = LineParser()
    e = p.parse("D (99) UPDATE: Sending block 3 (4094 bytes)")
    assert (e.level, e.tag) == ("D", "UPDATE")
    assert p.parse("V (100) X: y").level == "V"


def test_tag_without_space_after_colon_and_colons_in_message():
    """The TAG ends at the first ':' even without a following space; later colons stay in the message."""
    p = LineParser()
    e = p.parse(f"{GREEN}I (588) wifi:wifi driver task: 3fca, prio:23{RESET}")
    assert (e.tag, e.msg) == ("wifi", "wifi driver task: 3fca, prio:23")


def test_system_time_timestamp():
    """The "hh:mm:ss.mmm" timestamp of CONFIG_LOG_TIMESTAMP_SOURCE_SYSTEM is accepted."""
    e = LineParser().parse("I (12:34:56.789) main: ok")
    assert (e.esp_time, e.tag) == ("12:34:56.789", "main")


def test_multiline_message_inherits_color():
    """Continuation lines inherit the level from the colour carried until the reset; after the reset lines are OTHER."""
    p = LineParser()
    first = p.parse(f"{RED}E (10) JSONRPC: falha:")
    cont = p.parse(f"  detalhe da falha{RESET}")
    after = p.parse("texto puro")
    assert first.level == "E"
    assert cont.level == "E" and cont.tag == ""
    assert after.level == OTHER


def test_rom_and_panic_lines():
    """ROM/bootloader lines are OTHER; panic markers force level E and TAG PANIC."""
    p = LineParser()
    assert p.parse("ESP-ROM:esp32s3-20210327").level == OTHER
    e = p.parse("Guru Meditation Error: Core  1 panic'ed (LoadProhibited).")
    assert (e.level, e.tag) == ("E", "PANIC")


def test_other_ansi_color_kept():
    """An OTHER line keeps its own ANSI colour (cyan, 36) for display."""
    e = LineParser().parse("\x1b[0;36mprintf colorido\x1b[0m")
    assert e.level == OTHER and e.color == 36


def test_saved_line_roundtrip():
    """to_text() with PC time and parse_saved_line() round-trip; lines without the PC prefix are parsed too."""
    p = LineParser()
    e = p.parse(f"{GREEN}I (1) A: b{RESET}", datetime(2026, 10, 3, 12, 0, 0, 123000))
    text = e.to_text(with_pc_time=True)
    assert text == "[2026-10-03 12:00:00.123] I (1) A: b"
    back = parse_saved_line(LineParser(), text + "\n")
    assert (back.pc_time, back.level, back.tag, back.raw) == (e.pc_time, "I", "A", e.raw)
    assert parse_saved_line(LineParser(), "W (2) B: c").level == "W"

"""Serial reader thread: splits the byte stream into lines, parses them and records them to disk.

Threading model: SerialReader is a QThread created and owned by the GUI
(MainWindow). Its run() method executes in the worker thread and is the only
code that touches the serial port, the LineParser and the recording file.
Parsed lines are delivered to the GUI only through Qt signals; because the
signals are emitted from the worker thread and the receivers live in the GUI
thread, Qt queues them (AutoConnection), so the slots run in the GUI thread.
Lines are sent in batches (every BATCH_INTERVAL_S at most) to keep the event
loop responsive under heavy logging (the UDS flash prints tens of thousands
of lines). The GUI stops the thread by calling stop() (a flag polled by the
loops, at least every 50 ms) and waiting on QThread.wait().

Continuous recording happens here, not in the GUI, so no line is lost even if
the interface freezes or the on-screen limit (LogModel.max_lines) discards
old lines: every batch is written and flushed to the .log before it is
emitted to the GUI.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

import serial
from PySide6.QtCore import QThread, Signal

from log_parser import LineParser, format_pc_time

## Minimum interval (s) between two batches emitted to the GUI (and written to the .log).
BATCH_INTERVAL_S = 0.05
## A partial line (no '\\n' yet) is emitted as a line of its own after this idle time (s).
PARTIAL_FLUSH_S = 0.5
## Delay (s) between attempts to (re)open the port when auto-reconnect is on.
RECONNECT_INTERVAL_S = 1.0


class SerialReader(QThread):
    """Worker thread that reads one serial port, parses its lines and records them.

    One instance per "Conectar" click. With auto_reconnect the thread survives
    port errors (board reset, USB unplugged): it closes the port, waits
    RECONNECT_INTERVAL_S and reopens it, keeping the same recording file. The
    port is opened with DTR and RTS low, like idf.py monitor, so opening it does
    not reset the ESP32-S3 nor put it in download mode.
    """

    ## Batch of parsed lines (list[LogEntry]); emitted from the worker thread.
    entries_ready = Signal(list)  # list[LogEntry]
    ## Status text for the status bar and whether the port is currently open.
    status_changed = Signal(str, bool)  # message, connected
    ## Path of the recording file just opened, or "" when it was closed.
    record_file_changed = Signal(str)  # file path ("" = none)

    def __init__(
        self,
        port: str,
        baudrate: int,
        auto_reconnect: bool,
        record_dir: Path | None,
        record_pc_time: bool,
        parent=None,
    ) -> None:
        """Store the connection settings; nothing is opened until start() runs run().

        @param port            Port name: "COM5" on Windows, "/dev/ttyUSB0" or "/dev/ttyACM0" on Linux.
        @param baudrate        Baud rate (the ESP-IDF console default is 115200).
        @param auto_reconnect  Keep retrying to open the port and reopen it after errors.
        @param record_dir      Folder of the continuous recording, or None to not record.
        @param record_pc_time  Prefix each recorded line with the PC time.
        @param parent          Qt parent (the MainWindow).
        """
        super().__init__(parent)
        ## Port name to open (COMx or /dev/tty*).
        self.port = port
        ## Baud rate.
        self.baudrate = baudrate
        ## Retry opening / reopen the port after errors.
        self.auto_reconnect = auto_reconnect
        ## Recording folder, None = no recording.
        self.record_dir = record_dir
        ## Prefix recorded lines with the PC time.
        self.record_pc_time = record_pc_time
        ## Stop request flag, set by stop() from the GUI thread and polled by the worker.
        self._stop = False
        ## Parser owned by the worker thread (colour state).
        self._parser = LineParser()
        ## Open recording file, None when not recording.
        self._record_fp = None
        ## Last fatal error, shown in the final "Desconectado" status.
        self._error = ""

    def stop(self) -> None:
        """Ask the thread to finish; thread-safe, returns at once (use QThread.wait() to join).

        The read loop and the reconnect wait poll the flag at least every 50 ms
        (serial timeout / sleep step); pending lines are flushed before run() returns.

        @return None.
        """
        self._stop = True

    # -- continuous recording ----------------------------------------------
    def _open_record_file(self) -> None:
        """Create the recording file "ESP_{YYYYmmdd_HHMMSS}_{port}.log" in record_dir.

        Called once per run(), so reconnections keep appending to the same file.
        The port name is sanitised to [A-Za-z0-9_] ("/dev/ttyUSB0" -> "dev_ttyUSB0").
        The file is UTF-8 with LF line endings. On OSError, recording is disabled
        for this connection and the error is reported through status_changed.

        @return None.
        """
        if self.record_dir is None:
            return
        try:
            self.record_dir.mkdir(parents=True, exist_ok=True)
            safe_port = "".join(c if c.isalnum() else "_" for c in self.port).strip("_")
            name = f"ESP_{datetime.now():%Y%m%d_%H%M%S}_{safe_port}.log"
            path = self.record_dir / name
            self._record_fp = open(path, "a", encoding="utf-8", newline="\n")
            self.record_file_changed.emit(str(path))
        except OSError as exc:
            self._record_fp = None
            self.status_changed.emit(f"Falha ao criar arquivo de gravação: {exc}", False)

    def _close_record_file(self) -> None:
        """Close the recording file (if open) and emit record_file_changed("").

        @return None.
        """
        if self._record_fp:
            self._record_fp.close()
            self._record_fp = None
            self.record_file_changed.emit("")

    def _record(self, entries) -> None:
        """Append a batch to the recording file and flush it.

        @param entries  LogEntry list about to be emitted to the GUI.
        @note A write error closes the file (recording stops, reading goes on)
              and is reported through status_changed.
        @return None.
        """
        if not self._record_fp:
            return
        try:
            self._record_fp.write("".join(e.to_text(self.record_pc_time) + "\n" for e in entries))
            self._record_fp.flush()
        except OSError as exc:
            self.status_changed.emit(f"Erro gravando log: {exc}", True)
            self._close_record_file()

    # -- serial --------------------------------------------------------------
    def _open_serial(self) -> serial.Serial:
        """Open the port with a 50 ms read timeout and DTR/RTS low.

        DTR/RTS are set before open() so pyserial applies them when opening.
        On the ESP32-S3 auto-reset circuit (and on the USB-Serial-JTAG) these
        lines drive EN/GPIO0, so keeping them low avoids resetting the board.

        @return The open serial.Serial.
        @throws serial.SerialException  port missing, busy (another monitor open) or no permission
                                        (Linux: user not in the dialout group).
        """
        ser = serial.Serial()
        ser.port = self.port
        ser.baudrate = self.baudrate
        ser.timeout = 0.05
        # Same as idf.py monitor: do not trigger reset/bootloader when opening
        ser.dtr = False
        ser.rts = False
        ser.open()
        return ser

    def run(self) -> None:
        """Thread body: open the recording file, then open/read/reopen the port until stopped.

        Without auto_reconnect the thread ends on the first open error or lost
        connection (the reason is shown in the final "Desconectado — ..." status).
        With it, open errors show "Aguardando {port}…" and are retried every
        RECONNECT_INTERVAL_S. The parser colour state is reset on every
        (re)connection. Always ends by closing the recording file and emitting
        a "Desconectado" status; QThread.finished then reaches the GUI.

        @return None.
        """
        self._open_record_file()
        try:
            while not self._stop:
                try:
                    ser = self._open_serial()
                except (serial.SerialException, OSError) as exc:
                    if not self.auto_reconnect:
                        self._error = f"erro ao abrir {self.port}: {exc}"
                        return
                    self.status_changed.emit(f"Aguardando {self.port}…", False)
                    self._sleep(RECONNECT_INTERVAL_S)
                    continue

                self.status_changed.emit(f"Conectado em {self.port} @ {self.baudrate}", True)
                self._parser.reset()
                try:
                    self._read_loop(ser)
                except (serial.SerialException, OSError) as exc:
                    self._error = f"conexão perdida: {exc}"
                    self.status_changed.emit(f"Conexão perdida: {exc}", False)
                finally:
                    try:
                        ser.close()
                    except Exception:
                        pass
                if not self.auto_reconnect:
                    break
                if not self._stop:
                    self._sleep(RECONNECT_INTERVAL_S)
        finally:
            self._close_record_file()
            msg = f"Desconectado — {self._error}" if self._error and not self._stop else "Desconectado"
            self.status_changed.emit(msg, False)

    def _sleep(self, seconds: float) -> None:
        """Sleep in 50 ms steps, returning early when stop() is called.

        @param seconds  Maximum time to wait.
        @return None.
        """
        end = time.monotonic() + seconds
        while not self._stop and time.monotonic() < end:
            time.sleep(0.05)

    def _read_loop(self, ser: serial.Serial) -> None:
        """Read the open port until stop() or an I/O error, emitting parsed lines in batches.

        Bytes are accumulated and split on '\\n'; each line is decoded as UTF-8
        (invalid bytes replaced), stripped of '\\r' and parsed. A partial line
        idle for more than PARTIAL_FLUSH_S (e.g. a prompt without newline) is
        parsed as a line of its own, so a line interrupted by a pause longer than
        that appears split in two. Pending entries are recorded and emitted at
        most every BATCH_INTERVAL_S; what is left (pending entries and the partial
        line) is recorded and emitted when the loop ends, also when it ends with
        an I/O error, so the last lines before a USB drop or reset are kept.

        @param ser  Open port returned by _open_serial().
        @throws serial.SerialException, OSError  when the port disappears; run()
                handles it.

        @return None.
        """
        buf = bytearray()
        pending = []
        last_emit = time.monotonic()
        last_rx = last_emit
        try:
            while not self._stop:
                chunk = ser.read(ser.in_waiting or 1)
                now = time.monotonic()
                if chunk:
                    buf.extend(chunk)
                    last_rx = now
                    while True:
                        nl = buf.find(b"\n")
                        if nl < 0:
                            break
                        line = bytes(buf[:nl]).decode("utf-8", errors="replace").rstrip("\r")
                        del buf[: nl + 1]
                        pending.append(self._parser.parse(line))
                elif buf and now - last_rx > PARTIAL_FLUSH_S:
                    # Idle incomplete line (e.g. a prompt without '\n')
                    pending.append(self._parser.parse(bytes(buf).decode("utf-8", errors="replace")))
                    buf.clear()

                if pending and now - last_emit >= BATCH_INTERVAL_S:
                    self._record(pending)
                    self.entries_ready.emit(pending)
                    pending = []
                    last_emit = now
        finally:
            if buf:
                pending.append(self._parser.parse(bytes(buf).decode("utf-8", errors="replace")))
            if pending:
                self._record(pending)
                self.entries_ready.emit(pending)

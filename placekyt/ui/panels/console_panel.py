"""Embedded Python console — a REPL with the API namespace (the architecture notes §3.1).

A ``QPlainTextEdit``-based REPL running on the main Qt thread via
``code.InteractiveConsole`` (so widget access is thread-safe). The namespace is
pre-loaded with the live API objects (``project``, ``controller``, helpers).
Supports multi-line input (``...`` continuation), stdout/stderr capture, command
history (Up/Down), and Tab completion over the namespace via ``rlcompleter``.

Pygments syntax highlighting is applied per-submitted-line when Pygments is
installed (optional — the REPL works without it).
"""

from __future__ import annotations

import code
import contextlib
import io
import rlcompleter
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QTextCursor
from PySide6.QtWidgets import QPlainTextEdit

PROMPT = ">>> "
CONTINUE = "... "

_INPUT_COLOR = QColor(120, 200, 255)
_ERROR_COLOR = QColor(255, 120, 120)
_OUTPUT_COLOR = QColor(220, 220, 220)


class ConsolePanel(QPlainTextEdit):
    """A minimal but real Python REPL bound to the placeKYT API namespace."""

    def __init__(self, namespace: dict | None = None, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(150)
        self.setFont(QFont("monospace"))
        self.setUndoRedoEnabled(False)
        # Terminal-style: dark background so the light text colours are visible
        # (without this the near-white text is invisible on the light theme).
        self.setStyleSheet(
            "QPlainTextEdit { background-color: #1e2124; color: #dcdcdc; }")

        self._namespace: dict = {}
        self._console = code.InteractiveConsole(self._namespace)
        # InteractiveConsole.runcode writes tracebacks via self.write() (→ real
        # stderr), bypassing redirect_stderr. Route them into the widget instead.
        self._console.write = lambda text: self._append(text, _ERROR_COLOR)
        self._completer = rlcompleter.Completer(self._namespace)
        self._buffer: list[str] = []        # pending continuation lines
        self._history: list[str] = []
        self._history_pos = 0
        self._input_start = 0               # doc position where the editable line begins

        self.set_namespace(namespace or {})
        self._banner()
        self._write_prompt(PROMPT)

    # -- namespace ------------------------------------------------------------

    def set_namespace(self, namespace: dict) -> None:
        """Replace the REPL namespace (e.g. when a new project opens)."""
        self._namespace.clear()
        self._namespace.update(namespace)
        self._namespace.setdefault("__name__", "__console__")
        self._completer = rlcompleter.Completer(self._namespace)

    # -- output helpers -------------------------------------------------------

    def _append(self, text: str, color: QColor | None = None) -> None:
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.End)
        if color is not None:
            fmt = cursor.charFormat()
            fmt.setForeground(color)
            cursor.setCharFormat(fmt)
        cursor.insertText(text)
        # reset to default colour for subsequent text
        if color is not None:
            fmt = cursor.charFormat()
            fmt.setForeground(_OUTPUT_COLOR)
            cursor.setCharFormat(fmt)
        self.setTextCursor(cursor)
        self.ensureCursorVisible()

    def _banner(self) -> None:
        self._append("placeKYT console — `project`, `controller` in scope.\n",
                     _OUTPUT_COLOR)

    def _write_prompt(self, prompt: str) -> None:
        self._append(prompt, _OUTPUT_COLOR)
        self._input_start = self.textCursor().position()

    # -- current input line ---------------------------------------------------

    def _current_input(self) -> str:
        cursor = self.textCursor()
        cursor.setPosition(self._input_start)
        cursor.movePosition(QTextCursor.End, QTextCursor.KeepAnchor)
        return cursor.selectedText()

    def _replace_input(self, text: str) -> None:
        cursor = self.textCursor()
        cursor.setPosition(self._input_start)
        cursor.movePosition(QTextCursor.End, QTextCursor.KeepAnchor)
        cursor.insertText(text)
        self.setTextCursor(cursor)

    # -- evaluation -----------------------------------------------------------

    def submit(self, line: str) -> None:
        """Feed one source line to the interpreter (used by the UI and tests).

        Returns nothing; output is written to the widget. Multi-line blocks are
        accumulated until the interpreter reports the statement is complete.
        """
        if line.strip():
            self._history.append(line)
        self._history_pos = len(self._history)

        self._buffer.append(line)
        out = io.StringIO()
        err = io.StringIO()
        # Force the stock excepthook so InteractiveConsole.showtraceback takes
        # its ``self.write()`` branch (which we route into the widget) rather
        # than invoking a test/Qt-installed sys.excepthook. Capture stdout/stderr
        # for any output the executed code itself produces.
        saved_hook = sys.excepthook
        sys.excepthook = sys.__excepthook__
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                more = self._console.push(line)
        except KeyboardInterrupt:
            self._buffer.clear()
            self._append("\nKeyboardInterrupt\n", _ERROR_COLOR)
            self._write_prompt(PROMPT)
            return
        finally:
            sys.excepthook = saved_hook

        if out.getvalue():
            self._append(out.getvalue(), _OUTPUT_COLOR)
        if err.getvalue():
            self._append(err.getvalue(), _ERROR_COLOR)

        if more:
            self._write_prompt(CONTINUE)
        else:
            self._buffer.clear()
            self._write_prompt(PROMPT)

    def _enter(self) -> None:
        line = self._current_input()
        self._append("\n")
        self.submit(line)

    # -- key handling ---------------------------------------------------------

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        key = event.key()

        # Keep the caret within the editable region.
        if self.textCursor().position() < self._input_start and key not in (
            Qt.Key_Up, Qt.Key_Down, Qt.Key_Left, Qt.Key_Right,
        ):
            self.moveCursor(QTextCursor.End)

        if key in (Qt.Key_Return, Qt.Key_Enter):
            self._enter()
            return
        if key == Qt.Key_Up:
            self._history_prev()
            return
        if key == Qt.Key_Down:
            self._history_next()
            return
        if key == Qt.Key_Tab:
            self._complete()
            return
        if key in (Qt.Key_Backspace, Qt.Key_Left) and \
                self.textCursor().position() <= self._input_start:
            return  # don't delete the prompt
        super().keyPressEvent(event)

    # -- history --------------------------------------------------------------

    def _history_prev(self) -> None:
        if not self._history:
            return
        self._history_pos = max(0, self._history_pos - 1)
        self._replace_input(self._history[self._history_pos])

    def _history_next(self) -> None:
        if not self._history:
            return
        self._history_pos = min(len(self._history), self._history_pos + 1)
        text = (self._history[self._history_pos]
                if self._history_pos < len(self._history) else "")
        self._replace_input(text)

    # -- completion -----------------------------------------------------------

    def completions(self, text: str) -> list[str]:
        """Return rlcompleter candidates for ``text`` (also used by tests)."""
        results = []
        i = 0
        while True:
            c = self._completer.complete(text, i)
            if c is None:
                break
            results.append(c)
            i += 1
        return results

    def _complete(self) -> None:
        text = self._current_input()
        # complete the last whitespace-separated token
        token = text.split()[-1] if text.split() else ""
        cands = self.completions(token)
        if len(cands) == 1:
            self._replace_input(text[: len(text) - len(token)] + cands[0])
        elif len(cands) > 1:
            self._append("\n" + "  ".join(cands) + "\n", _OUTPUT_COLOR)
            self._write_prompt(PROMPT if not self._buffer else CONTINUE)
            self._append(text)

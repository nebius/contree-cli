"""Sphinx extension: render text or shell output as SVG terminal windows.

Directives::

    ```{terminal} Window Title
    Any text content here
    with ANSI colors supported
    ```

    ```{terminal} example.py
    :language: python

    def hello():
        print("world")
    ```

    ```{terminal-shell} contree run --help
    ```
"""

from __future__ import annotations

import os
import subprocess
from typing import Any, ClassVar

from ansi_svg import render_terminal
from docutils import nodes
from docutils.parsers.rst import directives
from sphinx.application import Sphinx
from sphinx.util.docutils import SphinxDirective

TERMINAL_COLUMNS = 100


def _highlight(text: str, language: str) -> str:
    """Highlight code with Pygments, return ANSI-colored text."""
    from pygments import highlight
    from pygments.formatters import Terminal256Formatter
    from pygments.lexers import get_lexer_by_name

    lexer = get_lexer_by_name(language)
    return highlight(text, lexer, Terminal256Formatter(style="monokai"))


class TerminalDirective(SphinxDirective):
    """Render literal text content as an SVG terminal window.

    The argument is the window title. Content is the text body.
    Use :language: to syntax-highlight via Pygments.
    """

    required_arguments = 1
    optional_arguments = 0
    final_argument_whitespace = True
    has_content = True
    option_spec: ClassVar[dict[str, Any]] = {
        "language": directives.unchanged,
    }

    def run(self) -> list[nodes.Node]:
        title = self.arguments[0].strip()
        text = "\n".join(self.content)
        if not text.strip():
            return []
        language = self.options.get("language")
        if language:
            text = _highlight(text, language)
        svg = render_terminal(title, text)
        return [nodes.raw("", svg, format="html")]


class TerminalShellDirective(SphinxDirective):
    """Run a shell command and render its output as an SVG terminal.

    The argument is the shell command to execute.
    """

    required_arguments = 1
    optional_arguments = 0
    final_argument_whitespace = True
    has_content = False
    option_spec: ClassVar[dict[str, Any]] = {}

    def run(self) -> list[nodes.Node]:
        command = self.arguments[0].strip()
        env = {**os.environ, "FORCE_COLOR": "1", "COLUMNS": str(TERMINAL_COLUMNS)}
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            env=env,
        )
        output = result.stdout or result.stderr
        if not output.strip():
            return []
        svg = render_terminal(f"$ {command}", output)
        return [nodes.raw("", svg, format="html")]


def setup(app: Sphinx) -> dict[str, Any]:
    app.add_directive("terminal", TerminalDirective)
    app.add_directive("terminal-shell", TerminalShellDirective)
    return {
        "version": "0.2",
        "parallel_read_safe": True,
        "parallel_write_safe": True,
    }

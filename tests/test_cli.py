"""The entry point's own behaviour, not the pipeline's.

One case only, and it is the one that cost a paid report: `eval report` renders
an arrow that the Windows console's code page cannot encode, so `print` raised
before the numbers reached disk.
"""

from __future__ import annotations

import io

from ainews.cli import _utf8_stdio


class _Plain:
    """A stream with no `reconfigure` - what a captured pipe can look like."""

    def write(self, text: str) -> int:  # pragma: no cover - never called here
        return len(text)


def test_utf8_stdio_survives_a_stream_that_cannot_reconfigure(monkeypatch):
    monkeypatch.setattr("sys.stdout", _Plain())
    monkeypatch.setattr("sys.stderr", _Plain())

    _utf8_stdio()  # the point is that this does not raise


def test_utf8_stdio_makes_an_unencodable_character_printable(monkeypatch):
    # cp1254 is the Turkish console page; the arrow is not in it.
    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1254")
    monkeypatch.setattr("sys.stdout", stream)
    monkeypatch.setattr("sys.stderr", stream)

    _utf8_stdio()
    stream.write("| ↳ unsupported claim |")  # would raise UnicodeEncodeError before
    stream.flush()

    assert stream.encoding == "utf-8"

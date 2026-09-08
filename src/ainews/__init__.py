"""AI news digest agent."""

from importlib.metadata import PackageNotFoundError, version

try:
    # One source of truth, and it is `pyproject.toml`. The string was typed out
    # in four places - here, the FastAPI app, `/health` and the project file -
    # so a release would have moved three of them and left one behind, and the
    # one left behind is whichever the reader happens to look at.
    __version__ = version("ainews")
except PackageNotFoundError:  # pragma: no cover - running from a source tree
    __version__ = "0.0.0+unknown"

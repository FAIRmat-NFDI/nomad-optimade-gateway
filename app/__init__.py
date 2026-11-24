"""Gateway translating between OPTIMADE and NOMAD APIs."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("nomad-optimade-gateway")
except PackageNotFoundError:  # pragma: no cover - running from a source tree without install
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]

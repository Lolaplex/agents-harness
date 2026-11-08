"""agents-harness runner module"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("agents-harness")
except PackageNotFoundError:  # a source checkout that was never installed
    __version__ = "0.0.0+source"

from .receipts import clear_final_check, register_final_check
from .retrieve import clear_content_backend, register_content_backend

__all__ = [
    "clear_content_backend",
    "clear_final_check",
    "register_content_backend",
    "register_final_check",
]

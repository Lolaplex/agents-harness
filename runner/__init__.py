"""agents-harness runner module"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("agents-harness")
except PackageNotFoundError:  # a source checkout that was never installed
    __version__ = "0.0.0+source"

from .approval_policy import clear_action_classifiers, register_action_classifier
from .recall import clear_recall_providers, register_recall_provider
from .receipts import clear_final_check, register_final_check
from .retrieve import clear_content_backend, register_content_backend

__all__ = [
    "clear_action_classifiers",
    "clear_content_backend",
    "clear_final_check",
    "clear_recall_providers",
    "register_action_classifier",
    "register_content_backend",
    "register_final_check",
    "register_recall_provider",
]

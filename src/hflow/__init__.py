"""HFlow package root. Version is a hand-maintained constant kept in sync with pyproject."""

from .contracts import SCHEMA_VERSION, ResultReceipt, TaskSpec

__all__ = ["SCHEMA_VERSION", "ResultReceipt", "TaskSpec", "__version__"]

__version__ = "0.0.1"

"""mosaic -- source-code similarity detection.

Multi-signal plagiarism detection for source code: winnowing fingerprints,
normalized structure-tree matching, consistent-rename detection,
compression distance and cohort statistics.
"""

from .engine import Cluster, Config, Document, Pair, Report, run  # noqa: F401
from .report import build_html, build_json, write_html  # noqa: F401

__version__ = "0.1.0"
__all__ = [
    "Config",
    "Document",
    "Pair",
    "Cluster",
    "Report",
    "run",
    "build_html",
    "build_json",
    "write_html",
]

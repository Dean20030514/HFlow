"""``python -m hflow ...``: the same entry point as the ``hflow`` console script.

The console script (``[project.scripts] hflow = "hflow.cli:main"``) calls ``hflow.cli.main()``
and exits with what it returns; this does exactly that, so the module form and the installed
command share one argument parser and one exit-code mapping.
"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())

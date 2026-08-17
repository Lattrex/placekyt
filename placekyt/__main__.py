# SPDX-License-Identifier: GPL-3.0-or-later
"""Entry point for ``python -m placekyt`` — delegates to the CLI."""

import sys

from cli import main

if __name__ == "__main__":
    sys.exit(main())

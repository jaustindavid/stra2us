# Copyright (c) 2026 Austin David — PolyForm Noncommercial 1.0.0
# See LICENSE in the repo root.
from .cli import main
import sys

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""Entry script for the PyInstaller build (the package itself uses relative imports)."""

import sys

from tokenpanel.__main__ import main

sys.exit(main())

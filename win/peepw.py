"""Launcher for the installed Windows-side package.

Run as a script (`python.exe C:\\Users\\<you>\\AppData\\Local\\peep\\app\\peepw.py rec`)
so the directory holding it — the app dir, which contains the `peep`
package — lands on sys.path without any environment variables (interop
does not forward them). Session B's startup entry launches this same
file with pythonw.exe.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from peep.cli import main  # noqa: E402

raise SystemExit(main())

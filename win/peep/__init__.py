"""peep — the Windows-side capture engine and storage core for peep-peep.

Session A of the build (see the project README): ffmpeg capture with a
microphone track and clapper flashes, the naming/catalog storage core,
and a terminal CLI that the WSL `peep` shim drives through interop.
Session B (resident agent) and session C (post-processing) import these
modules unchanged; nothing here assumes a UI.

Standard library only. Modules that touch Win32 (`winapi`, `flash`) do
so lazily, so the whole package imports and its pure parts test on
Linux with no ffmpeg, no display and no interop.
"""

import logging

__version__ = "0.1.0"

# Library etiquette: silent until the CLI (or session B) calls logsetup.setup_logging().
logging.getLogger("peep").addHandler(logging.NullHandler())

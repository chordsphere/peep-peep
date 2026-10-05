"""`python -m peep ...` — same as peepw.py; handy from a Windows console
when `win/` (or the installed `app/`) is the current directory."""

from .cli import main

raise SystemExit(main())

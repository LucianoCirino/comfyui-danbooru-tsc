"""Standalone scripts: DB builder + Danbooru wiki scrapers.

Each module can be run two ways:
  - As a package import: `from .. import scripts; scripts.build_db.build()`
  - Directly:           `python scripts/build_db.py`
The ImportError fallback in each script inserts the package root onto sys.path
so the second form still resolves `core.db` etc.
"""

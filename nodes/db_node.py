"""ComfyUI node that (re)builds the danbooru.db from the two CSVs.

Run this once after install — or any time you replace the CSVs with newer
copies. Outputs a status string with row counts.
"""

from __future__ import annotations

from ..core import db as dblayer
from ..scripts import build_db


class DanbooruDBBuild:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "character_csv": ("STRING", {
                    "default": str(dblayer.DEFAULT_CHARACTER_CSV),
                    "multiline": False,
                }),
                "artist_csv": ("STRING", {
                    "default": str(dblayer.DEFAULT_ARTIST_CSV),
                    "multiline": False,
                }),
                "rebuild": ("BOOLEAN", {
                    "default": False,
                    "tooltip": "Toggle to True and run to rebuild. Reset to False to avoid re-running on every queue.",
                }),
            }
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("status",)
    FUNCTION = "run"
    CATEGORY = "🎨 danbooru-tsc"
    OUTPUT_NODE = True

    def run(self, character_csv, artist_csv, rebuild):
        if not rebuild and dblayer.db_exists():
            s = dblayer.stats()
            return (
                f"DB already exists. Toggle rebuild=True to refresh.\n"
                f"  characters: {s['characters']:,}\n"
                f"  artists: {s['artists']:,}\n"
                f"  built_at: {s['built_at']}\n"
                f"  path: {s['path']}",
            )

        try:
            stats = build_db.build(
                character_csv=character_csv or None,
                artist_csv=artist_csv or None,
            )
        except Exception as e:
            return (f"Build failed: {type(e).__name__}: {e}",)

        return (
            f"DB built OK.\n"
            f"  characters: {stats['characters']:,}\n"
            f"  artists: {stats['artists']:,}\n"
            f"  character_tags: {stats['character_tags']:,}\n"
            f"  built_at: {stats['built_at']}\n"
            f"  path: {stats['path']}",
        )


class DanbooruDBStats:
    """Tiny info node — prints current DB stats. Useful for debugging."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("stats",)
    FUNCTION = "run"
    CATEGORY = "🎨 danbooru-tsc"

    @classmethod
    def IS_CHANGED(cls):
        # Always re-run so the displayed stats are fresh.
        import time
        return time.time()

    def run(self):
        s = dblayer.stats()
        if not s.get("exists"):
            return ("danbooru.db not found. Add a 'Danbooru DB Build' node.",)
        return (
            f"characters: {s['characters']:,}\n"
            f"artists: {s['artists']:,}\n"
            f"character_tags: {s['character_tags']:,}\n"
            f"built_at: {s['built_at']}\n"
            f"path: {s['path']}",
        )

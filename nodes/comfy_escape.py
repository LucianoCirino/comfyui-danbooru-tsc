"""ComfyTagEscape: rewrite Danbooru-style tags into ComfyUI-prompt-safe
form by escaping the literal parens used in disambiguation qualifiers.

ComfyUI's prompt parser treats unescaped parens as weight syntax
(``(tag:1.2)``). Danbooru tags routinely embed literal parens for
qualifiers: ``fate_(series)``, ``admiral_(kancolle)``,
``hammer_(sunset_beach)``. Feeding those through CLIPTextEncode raw
breaks parsing and silently mangles weights on neighbouring fragments.

The transformation rule (delegated to ``core.tagfmt.escape_for_comfy``):
each balanced ``(...)`` group becomes `` \\(...\\)``, with the joining
underscore dropped and underscores inside the qualifier converted to
spaces. Tag parts outside paren groups still respect the emoticon-aware
display rule, so ``looking_at_viewer`` → ``looking at viewer`` while
``o_o``, ``=_=``, ``:)`` pass through untouched.

Works on raw danbooru tags, gelbooru-swapped tags, character/artist
tags from the extractor — anything emitting comma- or newline-separated
tag strings. Newlines and commas are preserved so per-character outputs
flow cleanly. The underlying helper is idempotent, so chaining this
node after GelbooruTagSwap (which already escapes its output) is safe.
"""
from __future__ import annotations

from ..core.tagfmt import escape_for_comfy


class ComfyTagEscape:
    """Convert Danbooru-style paren qualifiers (``fate_(series)`` →
    ``fate \\(series\\)``) so ComfyUI's prompt parser treats them as
    literal text rather than weight syntax. Accepts comma/newline
    separated tags in either underscore or space form."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "tags": ("STRING", {"default": "", "multiline": True}),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("tags",)
    FUNCTION = "run"
    CATEGORY = "🎨 danbooru-tsc"

    def run(self, tags: str):
        if not tags:
            return ("",)
        out_lines: list[str] = []
        for line in tags.split("\n"):
            out_tokens: list[str] = []
            for raw in line.split(","):
                tok = raw.strip()
                if not tok:
                    continue
                out_tokens.append(escape_for_comfy(tok))
            out_lines.append(", ".join(out_tokens))
        return ("\n".join(out_lines),)

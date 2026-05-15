"""AnimaFastEnhancer: single-LLM-call replacement for AnimaSmartEnhancer.

Takes a user request + character/artist context + pre-rolled seed-tag blocks
(wire from RandomTagSampler nodes) and makes ONE LLM call to write the 2-5
sentence visual description. The LLM picks which seed tags fit and weaves them
in naturally — same quality trade-off as the tool-calling Smart Enhancer,
~5-7x faster (1 round-trip vs N+1).

Essentially AnimaEnhancerPrompt + an LlamaCpp Inference call collapsed into
one node, with no DB dependency.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from ..core import prompts as promptlib

DEFAULT_SYSTEM_PROMPT = promptlib.load("anima_enhancer")


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _chat(host: str, port: int, body: dict, timeout: int) -> dict:
    url = f"http://{host}:{port}/v1/chat/completions"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    resp = urllib.request.urlopen(req, timeout=timeout)
    return json.loads(resp.read().decode("utf-8"))


def _server_alive(host: str, port: int) -> bool:
    try:
        r = urllib.request.urlopen(f"http://{host}:{port}/health", timeout=3)
        return json.loads(r.read()).get("status") == "ok"
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------

def _build_user_message(user_request: str, character: str,
                        character_core_tags: str, artist: str,
                        seed_tags: str, extra_notes: str) -> str:
    parts: list[str] = [f"## USER REQUEST\n{user_request.strip()}\n"]

    if (character and character.strip()) or (character_core_tags and character_core_tags.strip()):
        char_lines = []
        if character and character.strip():
            char_lines.append(f"Name: {character.strip()}")
        if character_core_tags and character_core_tags.strip():
            char_lines.append(
                f"Canonical visuals (already encoded — DO NOT re-describe): "
                f"{character_core_tags.strip()}"
            )
        parts.append("## CHARACTER\n" + "\n".join(char_lines) + "\n")

    if artist and artist.strip():
        parts.append(
            f"## ARTIST\n{artist.strip()} — let this artist's style influence "
            f"mood and aesthetic.\n"
        )

    if seed_tags and seed_tags.strip():
        parts.append(
            "## SEED TAGS (creative inspiration — pick what fits, weave in "
            "naturally, ignore the rest)\n"
            + seed_tags.strip() + "\n"
        )

    if extra_notes and extra_notes.strip():
        parts.append(f"## ADDITIONAL NOTES\n{extra_notes.strip()}\n")

    parts.append(
        "Now write the description (2-5 sentences, prose only, no tags, no "
        "meta-commentary)."
    )
    return "\n".join(parts)


def _summarize_seed_tags(seed_tags: str) -> list[str]:
    """One-line-per-group preview of the seed tags, no definitions.

    Parses the YAML-ish format produced by RandomTagSampler:
        group_name:
          - tag_a: definition
          - tag_b
        other_group:
          - tag_c
    """
    if not seed_tags or not seed_tags.strip():
        return []
    out: list[str] = []
    current_group: str | None = None
    current_tags: list[str] = []
    for raw in seed_tags.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("- "):
            tag = stripped[2:].split(":", 1)[0].strip()
            if tag and current_group is not None:
                current_tags.append(tag)
        elif line.endswith(":") and not line.startswith(" "):
            if current_group is not None:
                out.append(f"  {current_group}: {', '.join(current_tags) if current_tags else '(empty)'}")
            current_group = line.rstrip(":")
            current_tags = []
    if current_group is not None:
        out.append(f"  {current_group}: {', '.join(current_tags) if current_tags else '(empty)'}")
    return out


# ---------------------------------------------------------------------------
# ComfyUI node
# ---------------------------------------------------------------------------

class AnimaFastEnhancer:
    """Single-call enhancer: takes pre-rolled seed blocks + context, returns prose."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": ("LLAMACPP_MODEL",),
                "user_request": ("STRING", {
                    "default": "a girl doing the splits",
                    "multiline": True,
                }),
            },
            "optional": {
                "character": ("STRING", {
                    "default": "", "multiline": False,
                    "tooltip": "Wire from Danbooru Agent's character_trigger.",
                }),
                "character_core_tags": ("STRING", {
                    "default": "", "multiline": True,
                    "tooltip": "Wire from Danbooru Agent's character_core_tags. "
                               "The enhancer is told NOT to re-describe these.",
                }),
                "artist": ("STRING", {
                    "default": "", "multiline": False,
                    "tooltip": "Wire from Danbooru Agent's artist_trigger.",
                }),
                "seed_tags": ("STRING", {
                    "default": "", "multiline": True,
                    "tooltip": "Wire from a Random Tag Sampler's tags_with_definitions output. "
                               "Concat multiple samplers upstream if you want more groups.",
                }),
                "extra_notes": ("STRING", {
                    "default": "", "multiline": True,
                    "tooltip": "Optional free-form note (e.g. 'avoid horror', 'cinematic mood').",
                }),
                "system_prompt": ("STRING", {
                    "default": DEFAULT_SYSTEM_PROMPT,
                    "multiline": True,
                    "tooltip": "Overrides prompts/anima_enhancer.md for this run.",
                }),
                "temperature": ("FLOAT", {
                    "default": 0.7, "min": 0.0, "max": 2.0, "step": 0.05,
                }),
                "max_tokens": ("INT", {"default": 512, "min": 64, "max": 32768}),
                "seed": ("INT", {"default": -1, "min": -1, "max": 0xffffffffffffffff}),
                "timeout": ("INT", {"default": 120, "min": 10, "max": 600}),
                "debug": ("BOOLEAN", {
                    "default": False,
                    "tooltip": "Include the full user message and prose in debug_info.",
                }),
            },
        }

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("enhanced_prose", "debug_info")
    FUNCTION = "run"
    CATEGORY = "🎨 danbooru-tsc"

    def run(self, model, user_request, character="", character_core_tags="",
            artist="", seed_tags="", extra_notes="",
            system_prompt=DEFAULT_SYSTEM_PROMPT, temperature=0.7,
            max_tokens=512, seed=-1, timeout=120, debug=False):

        if isinstance(model, dict):
            host = model.get("host", "localhost")
            port = model.get("port", 1234)
        else:
            host, port = "localhost", 1234

        if not _server_alive(host, port):
            return ("", f"llama.cpp server not reachable at {host}:{port}.")

        if not system_prompt or not system_prompt.strip():
            system_prompt = DEFAULT_SYSTEM_PROMPT
        user_message = _build_user_message(
            user_request, character, character_core_tags, artist,
            seed_tags, extra_notes,
        )

        body = {
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_message},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if seed is not None and seed >= 0:
            body["seed"] = seed

        start = time.time()
        try:
            resp = _chat(host, port, body, timeout=timeout)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")[:500]
            return ("", f"HTTP {e.code}: {err_body}")
        except Exception as e:
            return ("", f"{type(e).__name__}: {e}")
        elapsed = time.time() - start

        choice = (resp.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        prose = (msg.get("content") or "").strip()
        finish_reason = choice.get("finish_reason", "")

        seed_summary = _summarize_seed_tags(seed_tags)
        lines = [
            f"=== AnimaFastEnhancer ({elapsed:.1f}s, 1 LLM call) ===",
            f"server: {host}:{port}",
            f"seed groups: {len(seed_summary)}",
            f"finish_reason: {finish_reason}",
            f"prose length: {len(prose)} chars",
            "",
            "--- seed groups (one-line summary) ---",
        ]
        lines.extend(seed_summary or ["  (none)"])

        if debug:
            lines.append("")
            lines.append("--- full user message ---")
            lines.append(user_message)
            lines.append("")
            lines.append("--- prose ---")
            lines.append(prose)

        return (prose, "\n".join(lines))

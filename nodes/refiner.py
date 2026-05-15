"""DanbooruTagRefiner: take DanBot's raw tag candidates and refine them with
an LLM that has tag-search + definition-lookup tools.

Modes:
  trust              — no LLM call. Just normalize DanBot's tags and pass through.
  filter             — 1 LLM call, no tools. Drop tags not implied by the prose.
  filter+search      — LLM with search_tag. Drop bad ones + fill gaps.
  filter+search+lookup — LLM with search_tag + get_tag_definition. Verify
                       uncertain ones too. Most thorough.

Output is comma-separated tags in Anima-friendly form: lowercase, spaces
instead of underscores.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request

from ..core import db as dblayer
from ..core import search as searchmod
from ..core import prompts as promptlib
from ..core.tagfmt import to_display_tag


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TAG_SPLIT = re.compile(r"[,\n;]+")


def normalize_tag(raw: str) -> str:
    """Anima format: lowercased and trimmed, with underscore→space only on
    word-form tags. Emoticon tags like ``=_=`` keep their underscores."""
    return to_display_tag(raw.strip())


def parse_tag_list(raw: str) -> list[str]:
    if not raw:
        return []
    out = []
    seen = set()
    for chunk in _TAG_SPLIT.split(raw):
        n = normalize_tag(chunk)
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def join_tags(tags: list[str]) -> str:
    return ", ".join(tags)


# ---------------------------------------------------------------------------
# HTTP
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
# Submit-answer terminal tool (used by all LLM modes)
# ---------------------------------------------------------------------------

SUBMIT_SPEC = {
    "type": "function",
    "function": {
        "name": "submit_tags",
        "description": (
            "Call this exactly once when you have settled on the final list "
            "of danbooru tags. Pass them as a comma-separated string in "
            "lowercase with spaces (not underscores). Don't include the "
            "character or artist tags — those are handled separately."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tags": {
                    "type": "string",
                    "description": "Comma-separated tags, e.g. 'sitting on bench, looking at viewer, blue dress, sunset'.",
                },
                "reasoning": {
                    "type": "string",
                    "description": "One short sentence on what you kept/dropped/added. Optional.",
                },
            },
            "required": ["tags"],
        },
    },
}


# ---------------------------------------------------------------------------
# Mode -> (system prompt, tool list)
# ---------------------------------------------------------------------------

MODES = ["trust", "filter", "filter+search", "filter+search+lookup"]


_MODE_PROMPT_NAMES = {
    "filter":               "tag_refiner_filter",
    "filter+search":        "tag_refiner_filter_search",
    "filter+search+lookup": "tag_refiner_filter_search_lookup",
}


def _system_prompt(mode: str) -> str:
    return promptlib.load(_MODE_PROMPT_NAMES[mode])


def _tools(mode: str) -> list[dict]:
    if mode == "filter":
        return [SUBMIT_SPEC]
    if mode == "filter+search":
        # search_tag only, plus terminal
        search_only = next(s for s in searchmod.TAG_TOOL_SPECS if s["function"]["name"] == "search_tag")
        return [search_only, SUBMIT_SPEC]
    # filter+search+lookup
    return list(searchmod.TAG_TOOL_SPECS) + [SUBMIT_SPEC]


# ---------------------------------------------------------------------------
# Loop runner
# ---------------------------------------------------------------------------

def run_refiner(host, port, mode, prose, danbot_tags, character_core_tags,
                temperature, max_tokens, max_iterations, seed, timeout, debug,
                progress=print):
    """Returns (final_tag_list, transcript)."""
    user_msg = (
        f"Description of desired image:\n{prose.strip()}\n\n"
        f"DanBot's candidate tags:\n{danbot_tags.strip()}"
    )
    if character_core_tags and character_core_tags.strip():
        user_msg += (
            f"\n\nCharacter's core tags (already included separately — DO NOT repeat):\n"
            f"{character_core_tags.strip()}"
        )

    messages = [
        {"role": "system", "content": _system_prompt(mode)},
        {"role": "user",   "content": user_msg},
    ]
    tools = _tools(mode)
    transcript = []

    for it in range(max_iterations):
        body = {
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if seed is not None and seed >= 0:
            body["seed"] = seed + it

        if debug:
            progress(f"[refiner:{mode}] iter {it+1}/{max_iterations}")

        try:
            resp = _chat(host, port, body, timeout=timeout)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")[:500]
            transcript.append({"error": f"HTTP {e.code}: {err_body}"})
            return None, transcript
        except Exception as e:
            transcript.append({"error": f"{type(e).__name__}: {e}"})
            return None, transcript

        choice = (resp.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        tool_calls = msg.get("tool_calls") or []
        content = msg.get("content") or ""

        assistant_turn = {"role": "assistant", "content": content}
        if tool_calls:
            assistant_turn["tool_calls"] = tool_calls
        messages.append(assistant_turn)
        transcript.append({"assistant": content, "tool_calls": tool_calls})

        if not tool_calls:
            return None, transcript

        submitted = None
        for tc in tool_calls:
            fn = (tc.get("function") or {})
            name = fn.get("name", "")
            raw_args = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError:
                args = {}

            if name == "submit_tags":
                submitted = args
                tool_result = {"status": "received"}
            else:
                tool_result = searchmod.dispatch(name, args)

            if debug:
                preview = json.dumps(tool_result)[:160]
                progress(f"[refiner]   {name}({json.dumps(args)[:60]}) -> {preview}")

            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id", ""),
                "name": name,
                "content": json.dumps(tool_result),
            })
            transcript.append({"tool": name, "args": args, "result": tool_result})

            if submitted is not None:
                final = parse_tag_list(submitted.get("tags", ""))
                return final, transcript

    return None, transcript


# ---------------------------------------------------------------------------
# ComfyUI node
# ---------------------------------------------------------------------------

class DanbooruTagRefiner:
    """Refine DanBot tag candidates using an LLM (or just normalize, in trust mode)."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": ("LLAMACPP_MODEL",),
                "enhanced_prose": ("STRING", {
                    "default": "An anime girl with twintails sitting on a park bench at sunset, holding a bouquet of flowers.",
                    "multiline": True,
                }),
                "danbot_tags": ("STRING", {
                    "default": "1girl, sitting, bench, twintails, sunset, holding flowers",
                    "multiline": True,
                }),
                "mode": (MODES, {"default": "filter+search+lookup"}),
            },
            "optional": {
                "character_core_tags": ("STRING", {
                    "default": "",
                    "multiline": True,
                    "tooltip": "Wire from Danbooru Agent so the refiner won't repeat tags already implied by the character.",
                }),
                "max_iterations": ("INT", {"default": 6, "min": 1, "max": 20}),
                "temperature": ("FLOAT", {"default": 0.3, "min": 0.0, "max": 2.0, "step": 0.05}),
                "max_tokens": ("INT", {"default": 1024, "min": 64, "max": 32768}),
                "seed": ("INT", {"default": -1, "min": -1, "max": 0xffffffffffffffff}),
                "timeout": ("INT", {"default": 120, "min": 10, "max": 600}),
                "debug": ("BOOLEAN", {"default": False}),
            },
        }

    RETURN_TYPES = ("STRING", "STRING", "STRING")
    RETURN_NAMES = ("refined_tags", "dropped_tags", "debug_info")
    FUNCTION = "run"
    CATEGORY = "🎨 danbooru-tsc"

    def run(self, model, enhanced_prose, danbot_tags, mode,
            character_core_tags="", max_iterations=6, temperature=0.3,
            max_tokens=1024, seed=-1, timeout=120, debug=False):

        original = parse_tag_list(danbot_tags)

        # Trust mode — no LLM call
        if mode == "trust":
            return (
                join_tags(original),
                "",
                f"trust mode: passed {len(original)} tags through unchanged",
            )

        if not dblayer.db_exists():
            err = f"danbooru.db not found at {dblayer.DB_PATH}. Run 'Danbooru DB Build'."
            return (join_tags(original), "", err)

        if isinstance(model, dict):
            host = model.get("host", "localhost")
            port = model.get("port", 1234)
        else:
            host, port = "localhost", 1234

        if not _server_alive(host, port):
            err = f"llama.cpp server not reachable at {host}:{port}"
            return (join_tags(original), "", err)

        start = time.time()
        final, transcript = run_refiner(
            host=host, port=port, mode=mode,
            prose=enhanced_prose, danbot_tags=danbot_tags,
            character_core_tags=character_core_tags,
            temperature=temperature, max_tokens=max_tokens,
            max_iterations=max_iterations, seed=seed, timeout=timeout,
            debug=debug,
        )
        elapsed = time.time() - start

        # Fall back to original if LLM failed to submit
        if final is None:
            err = "LLM did not call submit_tags within iteration cap; passing DanBot tags through."
            return (join_tags(original), "", f"{err}\n\n--- transcript ---\n" + _format_transcript(transcript))

        # Compute dropped: tags in original but not in final
        final_set = set(final)
        original_set = set(original)
        dropped = [t for t in original if t not in final_set]
        added = [t for t in final if t not in original_set]

        debug_info = (
            f"=== TagRefiner ({mode}, {elapsed:.1f}s, {len(transcript)} turns) ===\n"
            f"input danbot tags : {len(original)}\n"
            f"final tags        : {len(final)}\n"
            f"dropped           : {len(dropped)}\n"
            f"added             : {len(added)}\n"
            f"\nfinal: {join_tags(final)}\n"
            f"dropped: {join_tags(dropped)}\n"
            f"added: {join_tags(added)}\n"
            f"\n--- transcript ---\n" + _format_transcript(transcript)
        )
        return (join_tags(final), join_tags(dropped), debug_info)


def _format_transcript(transcript):
    lines = []
    for i, turn in enumerate(transcript):
        if "error" in turn:
            lines.append(f"[{i}] ERROR: {turn['error']}")
        elif "assistant" in turn:
            tcs = turn.get("tool_calls") or []
            if tcs:
                summary = ", ".join(
                    f"{(tc.get('function') or {}).get('name', '?')}"
                    f"({(tc.get('function') or {}).get('arguments', '')[:60]})"
                    for tc in tcs
                )
                lines.append(f"[{i}] assistant -> tool_calls: {summary}")
            else:
                txt = (turn['assistant'] or '')[:200].replace('\n', ' ')
                lines.append(f"[{i}] assistant: {txt}")
        elif "tool" in turn:
            res = turn["result"]
            preview = json.dumps(res)[:200] if not isinstance(res, str) else res[:200]
            lines.append(f"[{i}] tool {turn['tool']}({json.dumps(turn['args'])[:60]}) -> {preview}")
    return "\n".join(lines)

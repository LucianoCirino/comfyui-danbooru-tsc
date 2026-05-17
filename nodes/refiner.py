"""DanbooruTagRefiner: weave a natural-language prose description together
with applicable danbooru tags into one final comma-separated prompt.

The output is a single fluid stream where each comma-separated chunk is
either a bare tag (scene-level concepts that don't need a subject binding:
``2girls``, ``standing``, ``sunset``) or a short subject-binding natural-
language phrase (per-character actions that tags can't anchor: ``miku
winking``, ``reimu holding a flower``). This matches Anima-family prompt
conventions: tags + light NL, with NL used only where tags can't bind a
concept to a specific subject.

Modes:
  trust                — no LLM call. Pass the prose through as-is.
  weave                — 1 LLM call, no tools. Weave prose+tags.
  weave+search         — LLM with search_tag. Same plus tag-spelling lookup.
  weave+search+lookup  — LLM with search_tag + get_tag_definition. Verify
                         meaning of uncertain tags before weaving them.
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
        "name": "submit_prompt",
        "description": (
            "Call this exactly once when you have settled on the final "
            "refined prompt. Pass it as one comma-separated string mixing "
            "bare danbooru tags with short subject-binding natural-language "
            "phrases. Lowercase, spaces (not underscores) for tags. Do NOT "
            "re-emit the listed character core_tags or series — those are "
            "wired into the final prompt elsewhere."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": (
                        "The final refined prompt. Example: "
                        "'2girls, standing, miku winking, reimu smiles, sunset'"
                    ),
                },
                "reasoning": {
                    "type": "string",
                    "description": "One short sentence on the weaving choices. Optional.",
                },
            },
            "required": ["prompt"],
        },
    },
}


# ---------------------------------------------------------------------------
# Mode -> (system prompt, tool list)
# ---------------------------------------------------------------------------

MODES = ["trust", "weave", "weave+search", "weave+search+lookup"]


_MODE_PROMPT_NAMES = {
    "weave":               "tag_refiner_weave",
    "weave+search":        "tag_refiner_weave_search",
    "weave+search+lookup": "tag_refiner_weave_search_lookup",
}


def _system_prompt(mode: str) -> str:
    return promptlib.load(_MODE_PROMPT_NAMES[mode])


def _tools(mode: str) -> list[dict]:
    if mode == "weave":
        return [SUBMIT_SPEC]
    if mode == "weave+search":
        # search_tag only, plus terminal
        search_only = next(s for s in searchmod.TAG_TOOL_SPECS if s["function"]["name"] == "search_tag")
        return [search_only, SUBMIT_SPEC]
    # weave+search+lookup
    return list(searchmod.TAG_TOOL_SPECS) + [SUBMIT_SPEC]


# ---------------------------------------------------------------------------
# Loop runner
# ---------------------------------------------------------------------------

def run_refiner(host, port, mode, prose, danbot_tags, character_context,
                temperature, max_tokens, max_iterations, seed, timeout, debug,
                top_p=-1.0, top_k=-1, min_p=-1.0,
                presence_penalty=0.0, repeat_penalty=-1.0,
                progress=print):
    """Returns (final_prompt_string, transcript)."""
    user_msg = (
        f"Description of desired image:\n{prose.strip()}\n\n"
        f"DanBot's candidate tags (advisory — may be noisy; verify against the description):\n"
        f"{danbot_tags.strip()}"
    )
    if character_context and character_context.strip():
        user_msg += (
            f"\n\nCharacter context (per character: name, series, core_tags — these "
            f"are ALREADY wired into the final prompt elsewhere; use the names to "
            f"anchor per-character actions in your output but DO NOT re-emit the "
            f"listed core_tags or series):\n"
            f"{character_context.strip()}"
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
            "presence_penalty": presence_penalty,
            "stream": False,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if seed is not None and seed >= 0:
            body["seed"] = seed + it
        if top_p >= 0:
            body["top_p"] = top_p
        if top_k >= 0:
            body["top_k"] = top_k
        if min_p >= 0:
            body["min_p"] = min_p
        if repeat_penalty >= 0:
            body["repeat_penalty"] = repeat_penalty

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

            if name == "submit_prompt":
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
                final = (submitted.get("prompt") or "").strip()
                return final, transcript

    return None, transcript


# ---------------------------------------------------------------------------
# ComfyUI node
# ---------------------------------------------------------------------------

class DanbooruTagRefiner:
    """Weave a natural-language prose description and applicable danbooru
    tags into one final hybrid prompt. The output is comma-separated, mixing
    bare tags with short subject-binding NL phrases — Anima-friendly form."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": ("LLAMACPP_MODEL",),
                "enhanced_prose": ("STRING", {
                    "default": "Hatsune Miku and Hakurei Reimu standing together. Miku is winking while Reimu smiles.",
                    "multiline": True,
                }),
                "danbot_tags": ("STRING", {
                    "default": "2girls, standing, wink, smile",
                    "multiline": True,
                }),
                "mode": (MODES, {"default": "weave+search"}),
            },
            "optional": {
                "character_context": ("STRING", {
                    "default": "",
                    "multiline": True,
                    "tooltip": (
                        "Per-character context the LLM uses to anchor per-character actions and "
                        "skip tags already covered elsewhere. Expected format (one entry per line):\n"
                        "character_1: name, series, core_tag1, core_tag2, ...\n"
                        "character_2: name, series, core_tag1, ...\n"
                        "The refiner will use the names to anchor per-character actions in the "
                        "output (e.g. 'miku winking') and will NOT re-emit the listed core_tags "
                        "or series."
                    ),
                }),
                "max_iterations": ("INT", {"default": 6, "min": 1, "max": 20}),
                "temperature": ("FLOAT", {"default": 0.3, "min": 0.0, "max": 2.0, "step": 0.05}),
                "max_tokens": ("INT", {"default": 1024, "min": 64, "max": 32768}),
                "seed": ("INT", {"default": -1, "min": -1, "max": 0xffffffffffffffff}),
                "top_p": ("FLOAT", {
                    "default": 0.8, "min": -1.0, "max": 1.0, "step": 0.01,
                    "tooltip": "Nucleus sampling cutoff. -1 = use server default. Qwen3 non-thinking recommended: 0.8.",
                }),
                "top_k": ("INT", {
                    "default": 20, "min": -1, "max": 1000,
                    "tooltip": "Top-k sampling cutoff. -1 = use server default. Qwen3 recommended: 20.",
                }),
                "min_p": ("FLOAT", {
                    "default": 0.0, "min": -1.0, "max": 1.0, "step": 0.01,
                    "tooltip": "Min-p sampling cutoff. -1 = use server default. Qwen3 recommended: 0.0.",
                }),
                "presence_penalty": ("FLOAT", {
                    "default": 0.0, "min": -2.0, "max": 2.0, "step": 0.05,
                    "tooltip": "Penalty for tokens already present in the output. 0 = no penalty (OpenAI default). Always sent — overrides any server CLI flag.",
                }),
                "repeat_penalty": ("FLOAT", {
                    "default": -1.0, "min": -1.0, "max": 2.0, "step": 0.05,
                    "tooltip": "llama.cpp n-gram repetition penalty. -1 = use server default. Typical: 1.1 (1.0 = off).",
                }),
                "timeout": ("INT", {"default": 120, "min": 10, "max": 600}),
                "debug": ("BOOLEAN", {"default": False}),
            },
        }

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("refined_prompt", "debug_info")
    FUNCTION = "run"
    CATEGORY = "🎨 danbooru-tsc"

    def run(self, model, enhanced_prose, danbot_tags, mode,
            character_context="", max_iterations=6, temperature=0.3,
            max_tokens=1024, seed=-1, top_p=0.8, top_k=20, min_p=0.0,
            presence_penalty=0.0, repeat_penalty=-1.0,
            timeout=120, debug=False):

        # Trust mode — no LLM call. Pass the prose through as-is so the
        # caller can still wire downstream nodes without an LLM running.
        if mode == "trust":
            return (
                enhanced_prose.strip(),
                f"trust mode: passed prose through unchanged ({len(enhanced_prose)} chars)",
            )

        if not dblayer.db_exists():
            err = f"danbooru.db not found at {dblayer.DB_PATH}. Run 'Danbooru DB Build'."
            return (enhanced_prose.strip(), err)

        if isinstance(model, dict):
            host = model.get("host", "localhost")
            port = model.get("port", 1234)
        else:
            host, port = "localhost", 1234

        if not _server_alive(host, port):
            err = f"llama.cpp server not reachable at {host}:{port}"
            return (enhanced_prose.strip(), err)

        start = time.time()
        final, transcript = run_refiner(
            host=host, port=port, mode=mode,
            prose=enhanced_prose, danbot_tags=danbot_tags,
            character_context=character_context,
            temperature=temperature, max_tokens=max_tokens,
            max_iterations=max_iterations, seed=seed, timeout=timeout,
            debug=debug,
            top_p=top_p, top_k=top_k, min_p=min_p,
            presence_penalty=presence_penalty, repeat_penalty=repeat_penalty,
        )
        elapsed = time.time() - start

        # Fall back to the original prose if the LLM never called submit_prompt.
        if final is None or not final.strip():
            err = "LLM did not call submit_prompt within iteration cap; passing prose through."
            return (
                enhanced_prose.strip(),
                f"{err}\n\n--- transcript ---\n" + _format_transcript(transcript),
            )

        debug_info = (
            f"=== TagRefiner ({mode}, {elapsed:.1f}s, {len(transcript)} turns) ===\n"
            f"prose chars : {len(enhanced_prose)}\n"
            f"final chars : {len(final)}\n"
            f"\nrefined_prompt:\n{final}\n"
            f"\n--- transcript ---\n" + _format_transcript(transcript)
        )
        return (final, debug_info)


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

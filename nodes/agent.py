"""DanbooruAgent: a single ComfyUI node that orchestrates an LLM tool-call
loop against a local llama.cpp server.

Input: a free-text user request like "an anime image of miku in the style of
ebifurya, blue dress, sitting on a bench".

The node hands the request to the LLM along with four search/lookup tools and
one terminal tool, `submit_answer`. The LLM searches the danbooru CSV-backed
database, picks the best matching character + artist, and submits a structured
answer. The node then re-looks-up the chosen keys against the DB so the final
trigger / core_tags strings are guaranteed correct (the LLM never has to copy
them by hand).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from ..core import db as dblayer
from ..core import search as searchmod
from ..core import prompts as promptlib


# ---------------------------------------------------------------------------
# Tool specs (search.* + the terminal submit_answer)
# ---------------------------------------------------------------------------

SUBMIT_ANSWER_SPEC = {
    "type": "function",
    "function": {
        "name": "submit_answer",
        "description": (
            "Call this exactly once when you have settled on the best matches. "
            "Pass the danbooru character key (or empty if no character was "
            "implied), the danbooru artist key (or empty if no artist was "
            "implied), and any extra danbooru-style tags from the user's "
            "request that aren't already covered by the character or artist."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "character": {
                    "type": "string",
                    "description": "Exact danbooru character key, e.g. 'hatsune_miku'. Empty string if none.",
                },
                "artist": {
                    "type": "string",
                    "description": "Exact danbooru artist key, e.g. 'ebifurya'. Empty string if none.",
                },
                "extra_tags": {
                    "type": "string",
                    "description": (
                        "Comma-separated extra danbooru tags from the user's "
                        "request, e.g. 'sitting, blue dress, looking at viewer'. "
                        "Empty string if none."
                    ),
                },
                "reasoning": {
                    "type": "string",
                    "description": "One short sentence explaining the choice. Optional.",
                },
            },
            "required": ["character", "artist", "extra_tags"],
        },
    },
}

ALL_TOOLS = list(searchmod.TOOL_SPECS) + [SUBMIT_ANSWER_SPEC]


DEFAULT_SYSTEM_PROMPT = promptlib.load("danbooru_agent")


# ---------------------------------------------------------------------------
# HTTP helper
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
# Loop runner
# ---------------------------------------------------------------------------

def run_agent(host: str,
              port: int,
              user_request: str,
              system_prompt: str,
              max_iterations: int,
              temperature: float,
              max_tokens: int,
              seed: int,
              timeout: int,
              debug: bool,
              progress=print):
    """Run the tool-call loop. Returns (submitted_args, transcript).

    submitted_args may be None if the model never called submit_answer within
    the iteration cap; in that case the agent's final assistant message is
    appended to the transcript so the caller can show what went wrong.
    """
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_request},
    ]
    transcript = []

    for it in range(max_iterations):
        body = {
            "messages": messages,
            "tools": ALL_TOOLS,
            "tool_choice": "auto",
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
            # Disable thinking — we want fast, deterministic tool dispatch
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if seed is not None and seed >= 0:
            body["seed"] = seed + it  # vary slightly across iterations

        if debug:
            progress(f"[agent] iter {it+1}/{max_iterations} -> POST chat")

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

        # Append the assistant turn verbatim — required for tool flow.
        assistant_turn = {"role": "assistant", "content": content}
        if tool_calls:
            assistant_turn["tool_calls"] = tool_calls
        messages.append(assistant_turn)
        transcript.append({"assistant": content, "tool_calls": tool_calls})

        if not tool_calls:
            # Model decided to answer in plain text without calling submit_answer.
            # This counts as the agent giving up; let the caller decide.
            return None, transcript

        # Execute each tool call. Stop early if submit_answer is called.
        submitted = None
        for tc in tool_calls:
            fn = (tc.get("function") or {})
            name = fn.get("name", "")
            raw_args = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError:
                args = {}

            if name == "submit_answer":
                submitted = args
                tool_result = {"status": "received"}
            else:
                tool_result = searchmod.dispatch(name, args)

            if debug:
                preview = json.dumps(tool_result)[:200]
                progress(f"[agent]   tool {name}({json.dumps(args)[:80]}) -> {preview}")

            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id", ""),
                "name": name,
                "content": json.dumps(tool_result),
            })
            transcript.append({"tool": name, "args": args, "result": tool_result})

            if submitted is not None:
                return submitted, transcript

    # Hit iteration cap without submit_answer
    return None, transcript


# ---------------------------------------------------------------------------
# ComfyUI node
# ---------------------------------------------------------------------------

class DanbooruAgent:
    """Run an LLM agent with danbooru search tools to resolve a free-text
    request into a (character, artist, extra_tags) tuple."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": ("LLAMACPP_MODEL",),
                "user_request": ("STRING", {
                    "default": "an anime image of hatsune miku in the style of ebifurya",
                    "multiline": True,
                }),
            },
            "optional": {
                "system_prompt": ("STRING", {
                    "default": DEFAULT_SYSTEM_PROMPT,
                    "multiline": True,
                }),
                "max_iterations": ("INT", {"default": 6, "min": 1, "max": 20}),
                "temperature": ("FLOAT", {"default": 0.3, "min": 0.0, "max": 2.0, "step": 0.05}),
                "max_tokens": ("INT", {"default": 1024, "min": 64, "max": 32768}),
                "seed": ("INT", {"default": -1, "min": -1, "max": 0xffffffffffffffff}),
                "timeout": ("INT", {"default": 120, "min": 10, "max": 600}),
                "debug": ("BOOLEAN", {"default": False}),
            },
        }

    RETURN_TYPES = ("STRING", "STRING", "STRING", "STRING", "STRING", "STRING")
    RETURN_NAMES = (
        "character_trigger",
        "character_core_tags",
        "artist_trigger",
        "extra_tags",
        "combined_prompt",
        "debug_info",
    )
    FUNCTION = "run"
    CATEGORY = "🎨 danbooru-tsc"

    def run(self, model, user_request, system_prompt=DEFAULT_SYSTEM_PROMPT,
            max_iterations=6, temperature=0.3, max_tokens=1024,
            seed=-1, timeout=120, debug=False):

        if not dblayer.db_exists():
            err = (
                f"danbooru.db not found at {dblayer.DB_PATH}. "
                "Add a 'Danbooru DB Build' node and run it once, "
                "or run build_db.py from the pack folder."
            )
            return ("", "", "", "", "", err)

        if isinstance(model, dict):
            host = model.get("host", "localhost")
            port = model.get("port", 1234)
        else:
            host, port = "localhost", 1234

        if not _server_alive(host, port):
            err = (f"llama.cpp server not reachable at {host}:{port}. "
                   "Add a LlamaCpp Load Model or LlamaCpp External Server node first.")
            return ("", "", "", "", "", err)

        start = time.time()
        submitted, transcript = run_agent(
            host=host, port=port,
            user_request=user_request,
            system_prompt=system_prompt or DEFAULT_SYSTEM_PROMPT,
            max_iterations=max_iterations,
            temperature=temperature,
            max_tokens=max_tokens,
            seed=seed,
            timeout=timeout,
            debug=debug,
        )
        elapsed = time.time() - start

        # Resolve final triggers via authoritative DB lookup, not by trusting
        # whatever string the model copied back.
        character_trigger = ""
        character_core_tags = ""
        artist_trigger = ""
        extra_tags = ""
        chosen_char_key = ""
        chosen_art_key = ""

        if submitted:
            chosen_char_key = (submitted.get("character") or "").strip()
            chosen_art_key = (submitted.get("artist") or "").strip()
            extra_tags = (submitted.get("extra_tags") or "").strip()

            if chosen_char_key:
                row = searchmod.lookup_character(chosen_char_key)
                if row:
                    character_trigger = row["trigger"] or ""
                    character_core_tags = row["core_tags"] or ""
            if chosen_art_key:
                row = searchmod.lookup_artist(chosen_art_key)
                if row:
                    artist_trigger = row["trigger"] or ""

        # Compose a combined prompt the user can wire straight into a CLIP node.
        parts = [p for p in (character_trigger, character_core_tags, extra_tags, artist_trigger) if p]
        combined_prompt = ", ".join(parts)

        # Debug info
        lines = [
            f"=== DanbooruAgent ({elapsed:.1f}s, {len(transcript)} turns) ===",
            f"server: {host}:{port}",
            f"submitted: {submitted}",
            f"resolved character: '{chosen_char_key}' -> trigger='{character_trigger}'",
            f"resolved artist:    '{chosen_art_key}' -> trigger='{artist_trigger}'",
            f"extra_tags: '{extra_tags}'",
            "",
            "--- transcript ---",
        ]
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
                    txt = turn["assistant"][:200].replace("\n", " ")
                    lines.append(f"[{i}] assistant: {txt}")
            elif "tool" in turn:
                res = turn["result"]
                preview = json.dumps(res)[:200] if not isinstance(res, str) else res[:200]
                lines.append(f"[{i}] tool {turn['tool']}({json.dumps(turn['args'])[:80]}) -> {preview}")
        debug_info = "\n".join(lines)

        return (
            character_trigger,
            character_core_tags,
            artist_trigger,
            extra_tags,
            combined_prompt,
            debug_info,
        )

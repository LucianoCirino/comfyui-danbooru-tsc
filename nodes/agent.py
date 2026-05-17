"""DanbooruAgent: a single ComfyUI node that orchestrates an LLM tool-call
loop against a local llama.cpp server.

Input: a free-text user request like "miku and rin in the style of ebifurya".

The node hands the request to the LLM along with the character/artist
search+lookup tools and one terminal tool, `submit_answer`. The LLM resolves
the request into a list of danbooru character keys (zero or more) and at
most one artist key. The node then re-looks-up each chosen key against the
DB so the final trigger / series / core_tags strings are guaranteed correct
(the LLM never copies them by hand). Generic descriptive tags are NOT this
node's job — a downstream tag-refiner handles those.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from ..core import db as dblayer
from ..core import search as searchmod
from ..core import prompts as promptlib
from ..core.tagfmt import to_display_tag


# ---------------------------------------------------------------------------
# Tool specs (search.* + the terminal submit_answer)
# ---------------------------------------------------------------------------

SUBMIT_ANSWER_SPEC = {
    "type": "function",
    "function": {
        "name": "submit_answer",
        "description": (
            "Call this exactly once when you have settled on the best matches. "
            "Pass the list of danbooru character keys the user implied (empty "
            "list if none) and the single danbooru artist key (empty string "
            "if none). Do NOT include generic descriptive tags — they are "
            "handled by a different node downstream."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "characters": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Exact danbooru character keys the user named or "
                        "clearly implied, e.g. ['hatsune_miku', 'kagamine_rin']. "
                        "Use an empty list [] if the user did not name any "
                        "character — empty is the correct answer in that case."
                    ),
                },
                "artist": {
                    "type": "string",
                    "description": (
                        "Exact danbooru artist key, e.g. 'ebifurya'. Empty "
                        "string if the user did not name an artist or style — "
                        "empty is the correct answer in that case."
                    ),
                },
                "reasoning": {
                    "type": "string",
                    "description": "One short sentence explaining the choice. Optional.",
                },
            },
            "required": ["characters", "artist"],
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
              top_p: float = 0.8,
              top_k: int = 20,
              min_p: float = 0.0,
              presence_penalty: float = 0.0,
              repeat_penalty: float = -1.0,
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
            "presence_penalty": presence_penalty,
            "stream": False,
            # Disable thinking — we want fast, deterministic tool dispatch
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if seed is not None and seed >= 0:
            body["seed"] = seed + it  # vary slightly across iterations
        if top_p >= 0:
            body["top_p"] = top_p
        if top_k >= 0:
            body["top_k"] = top_k
        if min_p >= 0:
            body["min_p"] = min_p
        if repeat_penalty >= 0:
            body["repeat_penalty"] = repeat_penalty

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

def _normalize_character_keys(submitted: dict) -> list[str]:
    """Pull a clean list of character keys out of a submit_answer payload.

    Accepts the canonical `characters: []` shape; also tolerates a legacy
    `character: ""` field or a stringified list, in case a small model regresses.
    """
    keys: list[str] = []
    raw = submitted.get("characters")
    if isinstance(raw, list):
        keys = [str(x).strip() for x in raw if str(x).strip()]
    elif isinstance(raw, str) and raw.strip():
        # Model handed us a string instead of a list — accept comma-separated.
        keys = [t.strip() for t in raw.split(",") if t.strip()]
    legacy = submitted.get("character")
    if isinstance(legacy, str) and legacy.strip() and not keys:
        keys = [legacy.strip()]
    # Dedupe while preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _split_character_trigger(trigger: str) -> tuple[str, str]:
    """Split a character `trigger` string into (character_display, series_display).

    The CSV stores trigger as e.g. ``"hatsune miku, vocaloid"`` — first chunk
    is the character display name, second is the series. Returns ("", "") if
    the trigger is empty.
    """
    if not trigger:
        return "", ""
    parts = [p.strip() for p in trigger.split(",", 1)]
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


class DanbooruAgent:
    """LLM agent that extracts danbooru character keys (zero or more) and an
    optional artist key from a free-text request. Generic descriptive tags are
    deliberately out of scope — a downstream tag-refiner handles those."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": ("LLAMACPP_MODEL",),
                "user_request": ("STRING", {
                    "default": "hatsune miku and kagamine rin in the style of ebifurya",
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

    RETURN_TYPES = ("STRING", "STRING", "STRING", "STRING", "STRING")
    RETURN_NAMES = (
        "character_triggers",
        "character_series",
        "character_core_tags",
        "artist_trigger",
        "debug_info",
    )
    FUNCTION = "run"
    CATEGORY = "🎨 danbooru-tsc"

    def run(self, model, user_request, system_prompt=DEFAULT_SYSTEM_PROMPT,
            max_iterations=6, temperature=0.3, max_tokens=1024,
            seed=-1, top_p=0.8, top_k=20, min_p=0.0,
            presence_penalty=0.0, repeat_penalty=-1.0,
            timeout=120, debug=False):

        if not dblayer.db_exists():
            err = (
                f"danbooru.db not found at {dblayer.DB_PATH}. "
                "Add a 'Danbooru DB Build' node and run it once, "
                "or run build_db.py from the pack folder."
            )
            return ("", "", "", "", err)

        if isinstance(model, dict):
            host = model.get("host", "localhost")
            port = model.get("port", 1234)
        else:
            host, port = "localhost", 1234

        if not _server_alive(host, port):
            err = (f"llama.cpp server not reachable at {host}:{port}. "
                   "Add a LlamaCpp Load Model or LlamaCpp External Server node first.")
            return ("", "", "", "", err)

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
            top_p=top_p,
            top_k=top_k,
            min_p=min_p,
            presence_penalty=presence_penalty,
            repeat_penalty=repeat_penalty,
        )
        elapsed = time.time() - start

        # Resolve final triggers via authoritative DB lookup, not by trusting
        # whatever strings the model copied back. Per-character outputs use
        # newline as the inter-character boundary so downstream consumers can
        # tell where one character's tags end and the next begins; CLIP text
        # encoders treat newlines as whitespace so a single-CLIP workflow
        # still "just works". Series is comma-joined and deduped because
        # it's scene-level context, not per-character.
        chosen_char_keys: list[str] = []
        chosen_art_key = ""
        char_triggers: list[str] = []
        char_series: list[str] = []
        char_core_tags: list[str] = []
        artist_trigger = ""
        resolved_char_rows: list[tuple[str, dict | None]] = []

        if submitted:
            chosen_char_keys = _normalize_character_keys(submitted)
            chosen_art_key = (submitted.get("artist") or "").strip()

            seen_series: set[str] = set()
            for key in chosen_char_keys:
                row = searchmod.lookup_character(key)
                resolved_char_rows.append((key, row))
                if not row:
                    continue
                char_disp, series_disp = _split_character_trigger(row.get("trigger") or "")
                # Fall back to deriving display names from the keys if the
                # trigger field was missing/oddly shaped.
                if not char_disp:
                    char_disp = to_display_tag(row.get("character") or key)
                if not series_disp and row.get("copyright"):
                    series_disp = to_display_tag(row["copyright"])
                if char_disp:
                    char_triggers.append(char_disp)
                if series_disp and series_disp not in seen_series:
                    seen_series.add(series_disp)
                    char_series.append(series_disp)
                core = (row.get("core_tags") or "").strip()
                if core:
                    char_core_tags.append(core)

            if chosen_art_key:
                arow = searchmod.lookup_artist(chosen_art_key)
                if arow:
                    artist_trigger = arow.get("trigger") or ""

        # Per-character lines separated by '\n'. For 1 character there is no
        # newline, so single-char workflows look identical to before.
        character_triggers = "\n".join(char_triggers)
        character_core_tags = "\n".join(char_core_tags)
        # Series is scene-level — comma-join and dedup is what you want.
        character_series = ", ".join(char_series)

        # Debug info
        lines = [
            f"=== DanbooruAgent ({elapsed:.1f}s, {len(transcript)} turns) ===",
            f"server: {host}:{port}",
            f"submitted: {submitted}",
            f"resolved characters ({len(chosen_char_keys)}):",
        ]
        if not chosen_char_keys:
            lines.append("  (none — agent returned empty character list)")
        for key, row in resolved_char_rows:
            if row:
                disp, series = _split_character_trigger(row.get("trigger") or "")
                lines.append(f"  '{key}' -> '{disp}' (series='{series}')")
            else:
                lines.append(f"  '{key}' -> NOT FOUND in DB")
        lines.append(f"resolved artist:    '{chosen_art_key}' -> trigger='{artist_trigger}'")
        lines.append("")
        lines.append("--- transcript ---")
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
            character_triggers,
            character_series,
            character_core_tags,
            artist_trigger,
            debug_info,
        )

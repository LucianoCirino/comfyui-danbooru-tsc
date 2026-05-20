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
from ..core.tagfmt import to_display_tag, split_character_trigger


# ---------------------------------------------------------------------------
# Tool specs (search.* + the terminal submit_answer)
# ---------------------------------------------------------------------------

# Canonical focus-tag list (display form, lowercase + spaces). Anything outside
# this set submitted by the model is treated as empty in _normalize_focus.
FOCUS_TAGS: tuple[str, ...] = (
    "ass focus", "food focus", "weapon focus", "plant focus", "foot focus",
    "solo", "text focus", "book focus", "thigh focus", "hand focus",
    "monster focus", "cloud focus", "footwear focus", "solo focus",
    "armpit focus", "animal focus", "other focus", "eye focus",
    "navel focus", "vehicle focus", "leg focus", "object focus",
    "pectoral focus", "hip focus", "male focus", "back focus",
    "clothes focus", "breast focus", "soft focus",
)
_FOCUS_TAG_SET: frozenset[str] = frozenset(FOCUS_TAGS)


# Canonical character-count tag list (cast/quantity descriptors). Curated from
# danbooru's character_count grouping with the focus-only tags stripped (they
# live in the `focus` output) plus composition/meta noise removed.
CHARACTER_COUNT_TAGS: tuple[str, ...] = (
    "solo",
    "1girl", "2girls", "3girls", "4girls", "5girls", "6+girls", "multiple girls",
    "1boy", "2boys", "3boys", "4boys", "5boys", "6+boys", "multiple boys",
    "1other", "2others", "3others", "4others", "5others", "6+others", "multiple others",
    "no humans", "animal", "creature", "people", "crowd", "clone",
)
_CHARACTER_COUNT_TAG_SET: frozenset[str] = frozenset(CHARACTER_COUNT_TAGS)


SUBMIT_ANSWER_SPEC = {
    "type": "function",
    "function": {
        "name": "submit_answer",
        "description": (
            "Call this exactly once when you have settled on the best matches. "
            "Pass the list of danbooru character keys the user implied (empty "
            "list if none), the list of danbooru artist keys (empty list if "
            "none), and at most one image-focus tag (empty string if none "
            "applies). Do NOT include generic descriptive tags — they are "
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
                "artists": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Exact danbooru artist keys the user named or clearly "
                        "implied, e.g. ['ebifurya'] or ['wlop', 'sakimichan']. "
                        "Usually zero or one; only return multiple if the user "
                        "explicitly asked to blend styles. Use [] if no artist "
                        "or style was named — empty is the correct answer "
                        "in that case."
                    ),
                },
                "focus": {
                    "type": "string",
                    "enum": ("", *FOCUS_TAGS),
                    "description": (
                        "ONE image-focus tag from the predefined list (see "
                        "system prompt for the list with definitions), or "
                        "empty string if no focus tag clearly applies. Empty "
                        "is the right answer when the user's request doesn't "
                        "strongly center on a specific focus."
                    ),
                },
                "character_count": {
                    "type": "array",
                    "items": {"type": "string", "enum": CHARACTER_COUNT_TAGS},
                    "description": (
                        "One or more character-count tags describing the "
                        "cast/subjects of the image. Multiple can apply "
                        "(e.g. ['1girl','1boy'] for a girl with a boy; "
                        "['no humans','animal'] for an animal-only scene). "
                        "See system prompt for full list with definitions. "
                        "Use [] only if the request implies no subject at all."
                    ),
                },
                "reasoning": {
                    "type": "string",
                    "description": "One short sentence explaining the choice. Optional.",
                },
            },
            "required": ["characters", "artists"],
        },
    },
}

ALL_TOOLS = list(searchmod.TOOL_SPECS) + [SUBMIT_ANSWER_SPEC]

# Search tools whose `limit` argument the node's search_limit widget should
# govern. When the widget is set, we override whatever limit the model picked
# so the user's setting — not the model's guess — controls result breadth.
_LIMIT_AWARE_TOOLS = frozenset({"search_character", "search_artist", "search_tag"})


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
              repeat_penalty: float = 1.0,
              search_limit: int = 10,
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
                call_args = args
                # The node's search_limit widget governs result breadth for
                # the search tools, overriding the model's own limit guess.
                if name in _LIMIT_AWARE_TOOLS and search_limit and search_limit > 0:
                    call_args = {**args, "limit": search_limit}
                tool_result = searchmod.dispatch(name, call_args)

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

def _normalize_focus(submitted: dict) -> str:
    """Pull the focus field; accept empty string and only values from
    FOCUS_TAGS. Tolerant of underscore vs space variants (some small
    models emit `food_focus` despite the spec's enum). Anything that
    doesn't match → empty."""
    raw = (submitted.get("focus") or "").strip().lower()
    if not raw:
        return ""
    # Convert underscores to spaces to match the canonical display form.
    candidate = raw.replace("_", " ")
    return candidate if candidate in _FOCUS_TAG_SET else ""


def _normalize_character_count(submitted: dict) -> list[str]:
    """Pull the character_count field as a deduped list of valid tags.
    Tolerant of stringified comma-separated input, underscores, and case.
    Anything not in CHARACTER_COUNT_TAGS is silently dropped."""
    raw = submitted.get("character_count")
    if isinstance(raw, str):
        items = [t.strip() for t in raw.split(",") if t.strip()]
    elif isinstance(raw, list):
        items = [str(x).strip() for x in raw if str(x).strip()]
    else:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for t in items:
        candidate = t.lower().replace("_", " ")
        if candidate in _CHARACTER_COUNT_TAG_SET and candidate not in seen:
            seen.add(candidate)
            out.append(candidate)
    return out


def _normalize_keys(submitted: dict, plural_field: str, singular_field: str) -> list[str]:
    """Pull a clean list of keys out of a submit_answer payload.

    Accepts the canonical array shape (``plural_field``); also tolerates a
    legacy singular string field or a stringified comma-separated list, in
    case a small model regresses. Dedupes preserving order.
    """
    keys: list[str] = []
    raw = submitted.get(plural_field)
    if isinstance(raw, list):
        keys = [str(x).strip() for x in raw if str(x).strip()]
    elif isinstance(raw, str) and raw.strip():
        # Model handed us a string instead of a list — accept comma-separated.
        keys = [t.strip() for t in raw.split(",") if t.strip()]
    legacy = submitted.get(singular_field)
    if isinstance(legacy, str) and legacy.strip() and not keys:
        keys = [legacy.strip()]
    seen: set[str] = set()
    out: list[str] = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


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
                "search_limit": ("INT", {
                    "default": 50, "min": 1, "max": 10000,
                    "tooltip": (
                        "How many results each character/artist search returns. "
                        "Overrides the model's own per-call limit, so this is the "
                        "real knob controlling search breadth. Higher = better "
                        "recall of less-popular matches, but more tokens in "
                        "context (slower on local models). Crank to the thousands "
                        "for effectively-unlimited recall."
                    ),
                }),
                "temperature": ("FLOAT", {
                    "default": 0.6, "min": 0.0, "max": 2.0, "step": 0.05,
                    "tooltip": (
                        "Qwen3's non-thinking chat default is 0.7. This node runs a "
                        "structured tool-calling extraction, so 0.6 keeps picks "
                        "steady while still letting the model disambiguate fuzzy "
                        "character/artist names. Drop to ~0.3 for maximum "
                        "determinism; raise to 0.7 for the stock chat preset."
                    ),
                }),
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
                    "tooltip": (
                        "Token-presence penalty. 0 = off. This is Qwen3's "
                        "RECOMMENDED lever for curbing runaway repetition: nudge "
                        "up into 0-2 if the model loops, but high values can cause "
                        "language mixing. Always sent (overrides any server flag)."
                    ),
                }),
                "repeat_penalty": ("FLOAT", {
                    "default": 1.0, "min": -1.0, "max": 2.0, "step": 0.05,
                    "tooltip": (
                        "llama.cpp n-gram repetition penalty. 1.0 = OFF, which is "
                        "optimal for Qwen3 tool-calling: penalizing repeats can "
                        "corrupt the necessarily-repeated structural tokens in JSON "
                        "tool arguments. -1 = defer to the server default "
                        "(unpredictable). Prefer presence_penalty for repetition."
                    ),
                }),
                "timeout": ("INT", {"default": 120, "min": 10, "max": 600}),
                "debug": ("BOOLEAN", {"default": False}),
            },
        }

    RETURN_TYPES = ("STRING", "STRING", "STRING", "STRING", "STRING", "STRING", "STRING", "STRING")
    RETURN_NAMES = (
        "character_triggers",
        "character_series",
        "character_core_tags",
        "artist_triggers",
        "character_count",
        "focus",
        "all_tags",
        "debug_info",
    )
    FUNCTION = "run"
    CATEGORY = "🎨 danbooru-tsc"

    def run(self, model, user_request, system_prompt=DEFAULT_SYSTEM_PROMPT,
            max_iterations=6, search_limit=50, temperature=0.6, max_tokens=1024,
            seed=-1, top_p=0.8, top_k=20, min_p=0.0,
            presence_penalty=0.0, repeat_penalty=1.0,
            timeout=120, debug=False):

        if not dblayer.db_exists():
            err = (
                f"danbooru.db not found at {dblayer.DB_PATH}. "
                "Add a 'Danbooru DB Build' node and run it once, "
                "or run build_db.py from the pack folder."
            )
            return ("", "", "", "", "", "", "", err)

        if isinstance(model, dict):
            host = model.get("host", "localhost")
            port = model.get("port", 1234)
        else:
            host, port = "localhost", 1234

        if not _server_alive(host, port):
            err = (f"llama.cpp server not reachable at {host}:{port}. "
                   "Add a LlamaCpp Load Model or LlamaCpp External Server node first.")
            return ("", "", "", "", "", "", "", err)

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
            search_limit=search_limit,
        )
        elapsed = time.time() - start

        # Resolve final triggers via authoritative DB lookup, not by trusting
        # whatever strings the model copied back. All multi-valued outputs use
        # '\n' as the inter-item boundary so downstream consumers can split
        # cleanly. Per-character outputs (triggers, series, core_tags) align
        # by index — i.e. character_series.split('\n')[i] is the series of
        # character_triggers.split('\n')[i]. Single-item workflows see no
        # newlines, so they look identical to the old single-value behavior.
        chosen_char_keys: list[str] = []
        chosen_art_keys: list[str] = []
        char_triggers: list[str] = []
        char_series: list[str] = []
        char_core_tags: list[str] = []
        art_triggers: list[str] = []
        char_count_tags: list[str] = []
        focus: str = ""
        resolved_char_rows: list[tuple[str, dict | None]] = []
        resolved_art_rows: list[tuple[str, dict | None]] = []

        if submitted:
            chosen_char_keys = _normalize_keys(submitted, "characters", "character")
            chosen_art_keys = _normalize_keys(submitted, "artists", "artist")
            char_count_tags = _normalize_character_count(submitted)
            focus = _normalize_focus(submitted)

            for key in chosen_char_keys:
                row = searchmod.lookup_character(key)
                resolved_char_rows.append((key, row))
                if not row:
                    # Keep alignment: emit empty placeholders so triggers[i],
                    # series[i], core_tags[i] still correspond to the same
                    # character even when one lookup fails.
                    char_triggers.append("")
                    char_series.append("")
                    char_core_tags.append("")
                    continue
                char_disp, series_disp = split_character_trigger(row.get("trigger") or "")
                # Fall back to deriving display names from the keys if the
                # trigger field was missing/oddly shaped.
                if not char_disp:
                    char_disp = to_display_tag(row.get("character") or key)
                if not series_disp and row.get("copyright"):
                    series_disp = to_display_tag(row["copyright"])
                char_triggers.append(char_disp)
                char_series.append(series_disp)
                char_core_tags.append((row.get("core_tags") or "").strip())

            for key in chosen_art_keys:
                arow = searchmod.lookup_artist(key)
                resolved_art_rows.append((key, arow))
                if arow:
                    art_triggers.append(arow.get("trigger") or "")
                else:
                    art_triggers.append("")

        # Per-item lines separated by '\n'. Single-item case has no newline.
        character_triggers = "\n".join(char_triggers)
        character_series = "\n".join(char_series)
        character_core_tags = "\n".join(char_core_tags)
        artist_triggers = "\n".join(art_triggers)
        # character_count uses comma separation (one chunk per tag, all on one line).
        character_count = ", ".join(char_count_tags)

        # `all_tags`: everything flattened into one comma-separated monolith,
        # ordered Anima-style: count → char+series interleaved → artists →
        # focus → core_tags. Per-character name/series stay adjacent
        # (character_triggers and character_series align by index).
        monolith_parts: list[str] = []
        if character_count:
            monolith_parts.append(character_count)
        for i, name in enumerate(char_triggers):
            if name:
                monolith_parts.append(name)
            if i < len(char_series) and char_series[i]:
                monolith_parts.append(char_series[i])
        for art in art_triggers:
            if art:
                monolith_parts.append(art)
        if focus:
            monolith_parts.append(focus)
        for core_line in char_core_tags:
            if core_line:
                monolith_parts.append(core_line)
        all_tags = ", ".join(monolith_parts)

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
                disp, series = split_character_trigger(row.get("trigger") or "")
                lines.append(f"  '{key}' -> '{disp}' (series='{series}')")
            else:
                lines.append(f"  '{key}' -> NOT FOUND in DB")
        lines.append(f"resolved artists ({len(chosen_art_keys)}):")
        if not chosen_art_keys:
            lines.append("  (none — agent returned empty artist list)")
        for key, arow in resolved_art_rows:
            if arow:
                lines.append(f"  '{key}' -> trigger='{arow.get('trigger') or ''}'")
            else:
                lines.append(f"  '{key}' -> NOT FOUND in DB")
        lines.append(f"character_count: '{character_count}'")
        lines.append(f"focus: '{focus}'")
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
            artist_triggers,
            character_count,
            focus,
            all_tags,
            debug_info,
        )

# comfyui-danbooru-tsc

A ComfyUI custom-node pack for building **Anima-format anime prompts** end-to-end:
take a free-text user request, resolve it to a danbooru character + artist + tags
via an LLM agent (running on a local llama.cpp server), enrich it with
randomized seed tags, refine it, and compose a final positive/negative prompt
ready to wire into a CLIP encoder.

All nodes appear under the **🎨 danbooru-tsc** category in ComfyUI.

## Folder layout

```
comfyui-danbooru-tsc/
├── __init__.py                  ComfyUI entry point — registers node classes
├── build.bat                    rebuild danbooru.db from csv/
├── fetch_definitions.bat        scrape Danbooru wiki definitions
├── fetch_groups.bat             scrape Danbooru tag_group:* hierarchy
├── danbooru.db                  built SQLite DB
├── csv/                         source CSVs (characters, artists, tags)
├── prompts/                     LLM system-prompt templates
├── core/                        shared infra: db.py, search.py, prompts.py
├── nodes/                       ComfyUI node implementations
└── scripts/                     standalone scripts called by the .bat files
```

The three `.bat` launchers at the root call the ComfyUI portable's embedded
Python (`..\..\..\python_embeded\python.exe`) so you don't need a separate
system Python install.

---

## Architecture at a glance

```
                     CSVs (characters / artists / tags)
                                  │
                                  ▼
                     ┌────────────────────────┐
                     │  Danbooru DB Build     │  one-time / on-demand
                     └────────────┬───────────┘
                                  ▼
                          danbooru.db (SQLite + FTS5)
                                  │
       ┌──────────────────────────┼─────────────────────────────────┐
       ▼                          ▼                                 ▼
  Agent / Refiner          Random Tag Sampler                  Character
  (LLM tool calls)         (group → N random tags)             Matcher
                                  │                            (tags → char)
                                  ▼
                          Fast Enhancer (LLM)
                          (prose description)
```

Two independent flows on top of the DB:

1. **LLM-driven flow** (Agent → Fast Enhancer → Tag Refiner → Composer) — turns
   prose into a structured prompt.
2. **Random-roll flow** (Sampler / Matcher / Random Image) — purely DB-driven,
   no LLM required.

---

## The data layer

### `danbooru.db` (SQLite, FTS5 trigram-tokenized)

Tables (`core/db.py`):

| Table              | Purpose                                                           |
|--------------------|-------------------------------------------------------------------|
| `characters`       | Character key, copyright, trigger phrase, core_tags, popularity   |
| `character_tags`   | Exploded core_tags for tag-based character matching               |
| `artists`          | Artist key, trigger, popularity                                   |
| `tags`             | General-vocabulary tags + count + grouping + wiki definition      |
| `tag_groupings`    | Many-to-many: a tag can belong to multiple groups                 |
| `*_fts`            | FTS5 trigram virtual tables for fuzzy substring search            |
| `meta`             | Build timestamp + source CSV paths                                |

### Source CSVs (default location: `<pack>/csv/`)

- `danbooru_character.csv` — character key, copyright, trigger, core_tags, count
- `danbooru_artist.csv` — artist key, trigger, count
- `danbooru_tags_with_definitions.csv` — tag, count, grouping, definition
- `danbooru_tags.csv` — raw tag list (input to `scripts/fetch_tag_definitions.py`;
  not read directly by the DB build)

Paths are defined in `core/db.py` (`DEFAULT_CSV_DIR = PACK_DIR / "csv"`) and
reused by every script under `scripts/`, so the CSVs only need to live in one
place. `PACK_DIR` resolves to the package root, *not* the `core/` subdir.

### Building the tag CSVs from scratch (optional)

If you want to (re)scrape the Danbooru wiki rather than use a pre-baked CSV,
double-click these .bat files from the pack root (they call the ComfyUI
portable's embedded Python):

```
fetch_definitions.bat   # reads csv/danbooru_tags.csv → writes csv/danbooru_tags_with_definitions.csv
fetch_groups.bat        # scrapes tag_group:* pages, rewrites csv/danbooru_tags_with_definitions.csv with a 'grouping' column
```

Or run them directly:

```
python scripts/fetch_tag_definitions.py
python scripts/fetch_tag_groups.py
```

Both are resumable (read existing rows, skip them on rerun) and use `curl_cffi`
with Chrome impersonation to bypass Cloudflare. `fetch_tag_groups.py` also
captures sub-section headers inside each tag_group page (e.g. `view_angle`,
`perspective_depth`, `composition` inside `image_composition`) so the grouping
column ends up with `image_composition|image_composition:view_angle` etc.

### Building the DB

Either:

- Drop a **🎨 Danbooru DB Build** node in ComfyUI, set `rebuild=True`, queue once.
- Double-click `build.bat` from the pack root.
- Or from the pack folder: `python scripts/build_db.py`

`scripts/build_db.py` wipes and rebuilds `danbooru.db` from the three CSVs and stamps
`meta.built_at`. A working DB is a prerequisite for almost every other node in
this pack.

---

## System prompts (the `prompts/` folder)

Every LLM node loads its default system prompt from a `.md` file under
`<pack>/prompts/`. Edit any file in your editor of choice — restart ComfyUI to
pick up changes.

| File                                         | Used by                                              |
|----------------------------------------------|------------------------------------------------------|
| `prompts/danbooru_agent.md`                  | Danbooru Agent                                       |
| `prompts/anima_enhancer.md`                  | Anima Fast Enhancer                                  |
| `prompts/tag_refiner_filter.md`              | Tag Refiner (`filter` mode)                          |
| `prompts/tag_refiner_filter_search.md`       | Tag Refiner (`filter+search` mode)                   |
| `prompts/tag_refiner_filter_search_lookup.md`| Tag Refiner (`filter+search+lookup` mode)            |

The Agent and Fast Enhancer also expose a `system_prompt` STRING input on the
node itself — its default is the file content, but you can override per-run
without touching the file. The Refiner picks the right file automatically based
on its `mode` dropdown (no per-run override).

Loading is handled by the tiny `core/prompts.py` module: `promptlib.load("name")`
returns the raw text of `prompts/name.md`.

---

## The 9 nodes

### DB management

#### 🎨 Danbooru DB Build (`nodes/db_node.py` — `DanbooruDBBuild`)

Builds `danbooru.db` from the three CSVs. Toggle `rebuild=True` and queue once
to refresh; leave `False` afterwards to no-op (so it doesn't rebuild on every
queue).

- **In:** `character_csv`, `artist_csv`, `rebuild`
- **Out:** `status`

#### 🎨 Danbooru DB Stats (`nodes/db_node.py` — `DanbooruDBStats`)

Tiny info node — prints current row counts and `built_at`. Always re-runs (via
`IS_CHANGED`) so the displayed stats are fresh.

- **Out:** `stats`

---

### LLM nodes (require a llama.cpp server reachable at `host:port`)

All three of these nodes accept an `LLAMACPP_MODEL` input — a `{host, port}` dict
typically supplied by an external "LlamaCpp Load Model" or "LlamaCpp External
Server" node. They POST to `http://<host>:<port>/v1/chat/completions` with
OpenAI-style tool-call payloads.

#### 🎨 Danbooru Agent (LLM) (`nodes/agent.py` — `DanbooruAgent`)

The orchestrator. Takes a free-text request (e.g. *"an anime image of miku in
the style of ebifurya, blue dress, sitting on a bench"*) and hands it to the LLM
together with five tools:

- `search_character`, `lookup_character`
- `search_artist`, `lookup_artist`
- `submit_answer` (terminal — `{character, artist, extra_tags, reasoning}`)

The LLM searches, picks the best character + artist keys, and submits its
answer. The node then **re-looks-up the chosen keys against the DB** so the
returned trigger / core_tags strings are guaranteed correct (the LLM never has
to copy them by hand).

System prompt: `prompts/danbooru_agent.md` (overridable via the node's
`system_prompt` input).

- **Out:** `character_trigger`, `character_core_tags`, `artist_trigger`,
  `extra_tags`, `combined_prompt`, `debug_info`

#### 🎨 Anima Fast Enhancer (LLM) (`nodes/fast_enhancer.py` — `AnimaFastEnhancer`)

Single-LLM-call enhancer — takes a user request + character/artist context +
pre-rolled `seed_tags` and makes ONE chat completion call to write the 2-5
sentence prose description.

The LLM picks which seed tags fit, weaves them in, and ignores the rest. No DB
dependency.

Inputs of note:

- `seed_tags` — paste/wire the `tags_with_definitions` output from one or more
  `RandomTagSampler` nodes. Concat upstream if you want multiple groups.
- `character_core_tags` — when wired, the prompt tells the LLM not to
  re-describe canonical visuals (the Composer encodes them separately).
- `system_prompt` — defaults to `prompts/anima_enhancer.md`, overridable.

- **Out:** `enhanced_prose`, `debug_info`

#### 🎨 Danbooru Tag Refiner (LLM) (`nodes/refiner.py` — `DanbooruTagRefiner`)

Cleans up raw tag candidates (typically from a separate DanBot / WD-tagger
node) against the prose. Four modes:

| Mode                    | What it does                                                                                     |
|-------------------------|--------------------------------------------------------------------------------------------------|
| `trust`                 | No LLM call. Just normalize + pass through.                                                      |
| `filter`                | 1 LLM call, no tools. Drops tags not implied by the prose / already in `character_core_tags`.    |
| `filter+search`         | LLM with `search_tag`. Drops bad ones AND fills gaps the tagger missed.                          |
| `filter+search+lookup`  | Adds `get_tag_definition`. Verifies uncertain tags too. Most thorough.                           |

System prompts: `prompts/tag_refiner_<mode>.md` (one per non-trust mode, picked
by the `mode` dropdown).

If the LLM fails to call `submit_tags` within the iteration cap, the node falls
back to passing the original DanBot tags through (so the workflow still
completes).

- **Out:** `refined_tags`, `dropped_tags`, `debug_info`

---

### Prompt assembly

#### 🎨 Anima Prompt Composer (`nodes/composer.py` — `AnimaPromptComposer`)

The terminal node — assembles the final positive + negative prompt strings in
Anima's recommended order:

```
[quality/meta] [character_count] [character] [@artist] [extra_tags + general_tags]
```

Example output:

```
masterpiece, best quality, score_7, safe, 1girl, hatsune miku, vocaloid,
@ebifurya, sitting on bench, looking at viewer, blue dress, sunset
```

Notes:

- Lowercase, spaces (not underscores), comma-separated
- `score_N` is preserved underscored (Anima requires that)
- Artist is auto-prefixed with `@`
- `character_count='auto'` runs a lightweight prose heuristic to pick
  `1girl` / `1boy` / `multiple girls` / `no humans` etc.
- Quality + negative prompts have presets (`default` / `high` / `lite` /
  `(custom)`)
- `append_prose=True` enables Anima's "mixed mode" — tag list + free prose

- **Out:** `positive_prompt`, `negative_prompt`

---

### Random / matcher utilities (no LLM)

#### 🎨 Random Tag Sampler (`nodes/sampler.py` — `RandomTagSampler`)

Picks N random tags from one tag group (e.g. `face_tags`, `lighting`) using
**weighted sampling without replacement** (Efraimidis-Spirakis). The
`popularity_weight` knob:

| Value     | Behavior                                          |
|-----------|---------------------------------------------------|
| `0.0`     | Uniform random                                    |
| `1.0`     | Linear in post_count (popular = more likely)      |
| `2.0+`    | Strongly favor popular tags                       |
| Negative  | Favor rare tags / deep cuts                       |

Filters: `min_post_count`, comma- or newline-separated `banned_tags` (either
underscore or space form works).

Outputs four formats so you can wire it into different downstream nodes:

- `tags` — `"blush, smile, twintails"`
- `labeled_tags` — `"face_tags: blush, smile"` (lets the consumer know the source)
- `tags_with_definitions` — YAML-ish block with first-sentence definitions
  (this is the format the Fast Enhancer's `seed_tags` input expects)
- `group` — just the selected group name (e.g. `"face_tags"`), useful for
  labeling downstream or feeding into a prompt template

The `group` dropdown is populated dynamically from the DB at node-load time;
if the DB isn't built, you'll see `(rebuild DB first)`.

- **Out:** `tags`, `labeled_tags`, `tags_with_definitions`, `group`, `debug_info`

#### 🎨 Danbooru Character Matcher (`nodes/matcher.py` — `DanbooruCharacterMatcher`)

The inverse of the Agent — given a list of tags, find characters whose
`core_tags` overlap the most, then weighted-random-pick one. Knobs:

- `match_weight` — how heavily to favor characters with more matching tags
  (`match_count ** match_weight`)
- `popularity_bias` — how heavily to favor popular characters
  (`(1/(rank+1)) ** popularity_bias`)
- `random_seed` — `random` (time-based) or `fixed`

Useful for "given these visual tags, pick a fitting character" workflows.

- **Out:** `trigger`, `core_tags`, `matched_tags`, `match_count`,
  `probability_%`, `debug_info`

#### 🎨 Danbooru Random Image (`nodes/random_image.py` — `DanbooruRandomImage`)

Hits the public `danbooru.donmai.us` JSON API for `tags=<artist>+order:random`,
with optional `required_tags` filtering, and returns a torch IMAGE batch (with
zero-padding to the largest size in the batch). Mostly used to pull style
references for an artist.

Note: this is the only node that makes an outbound HTTP call to Danbooru
itself — everything else uses the local DB.

- **Out:** `IMAGES`, `TAGS`, `URLS`

---

## End-to-end LLM workflow

```
                          ┌──────────────────────┐
                user ───→ │ Danbooru Agent       │ → character_trigger, character_core_tags,
                          │ (resolves char/art)  │   artist_trigger, extra_tags
                          └──────────────────────┘
                                  │
   RandomTagSampler ×N ──┐        │
   (concat tags_with_    ├──────┐ │
    definitions outputs) │      ▼ ▼
                                ┌──────────────────────┐
                                │ Anima Fast Enhancer  │ → enhanced_prose
                                │   (1 LLM call)       │
                                └──────────────────────┘
                                  │
                                  ├──── enhanced_prose ────┐
                                  │                        ▼
                                  │               ┌──────────────────────┐
                                  │               │ (external) DanBot or │ → raw tag candidates
                                  │               │  WD tagger node      │
                                  │               └──────────────────────┘
                                  │                        │
                                  ▼                        ▼
                                ┌──────────────────────────────────┐
                                │ Danbooru Tag Refiner             │ → refined_tags
                                │ (filter+search+lookup mode)      │
                                └──────────────────────────────────┘
                                  │
                                  ▼
                                ┌──────────────────────┐
                                │ Anima Prompt Composer│ → positive_prompt, negative_prompt
                                └──────────────────────┘
                                  │
                                  ▼
                              CLIP encoder → KSampler → image
```

Connections to wire:

- **Agent → Fast Enhancer**: `character_trigger` → `character`,
  `character_core_tags` → `character_core_tags`, `artist_trigger` → `artist`
- **Sampler(s) → Fast Enhancer**: each sampler's `tags_with_definitions`
  string, concatenated upstream (single `seed_tags` input)
- **Agent → Refiner**: `character_core_tags` (so the refiner won't repeat tags
  the character already implies)
- **Agent → Composer**: `character_trigger`, `artist_trigger`, `extra_tags`
- **Fast Enhancer → Refiner**: `enhanced_prose` (used as the prose context
  for filtering DanBot's output)
- **Fast Enhancer → Composer**: `enhanced_prose` (for `auto` character_count
  heuristic and optional `append_prose`)
- **Refiner → Composer**: `refined_tags` → `general_tags`

---

## Common gotchas

- **`danbooru.db not found`** — Add and run a 🎨 Danbooru DB Build node once,
  double-click `build.bat`, or run `python scripts/build_db.py` from the pack folder.
- **`llama.cpp server not reachable at host:port`** — The LLM nodes need a
  llama.cpp server (or compatible OpenAI-style endpoint) at the host/port the
  `LLAMACPP_MODEL` input points to. The node hits `/health` first; the chat
  endpoint is `/v1/chat/completions`.
- **`(rebuild DB first)` in the Sampler dropdown** — same as above; the
  dropdown is populated from `tag_groupings` at node-load time.
- **LLM fails to call `submit_*`** — agent / refiner / fast enhancer all
  surface a debug transcript in `debug_info`. If it ran out of iterations,
  bump `max_iterations`. If it returned plain text without using tools, the
  model's chat template may not support tool-calling — try a different model.
  If `max_tokens` is too low the model may be cut off mid-call → empty output.
- **All LLM nodes disable thinking** (`enable_thinking: False`) — they want
  fast, deterministic tool dispatch, not chain-of-thought.
- **Tag formats differ across the pack:** the DB stores `underscore_form`;
  Anima output (Composer, Refiner, Sampler `tags` output) uses `space form`
  with lowercase. The Composer preserves `score_N` underscored as a special
  case.
- **Edited a prompt file but nothing changed** — restart ComfyUI. Prompts are
  loaded at module import, not per-call.

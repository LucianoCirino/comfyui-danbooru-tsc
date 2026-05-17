"""Tag-based weighted-random character matcher.

Moved from ComfyUI-invAIder-Nodes. Now reads from the unified danbooru.db
built by build_db.py instead of a separate character_data.db.
"""

from __future__ import annotations

import random
import re
import time

from ..core import db as dblayer
from ..core.tagfmt import split_character_trigger


class DanbooruCharacterMatcher:
    """Match random danbooru tags to characters, prioritising characters with
    more matching tags. Weighted random pick across candidates."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "tags": ("STRING", {
                    "multiline": True,
                    "default": "1girl, long hair, blue eyes, brown hair",
                    "tooltip": "Comma or newline separated danbooru tags",
                }),
                "max_db_rows": ("INT", {
                    "default": 10000,
                    "min": 100,
                    "max": 100000,
                    "tooltip": "How many characters to search (by popularity rank)",
                }),
                "match_weight": ("FLOAT", {
                    "default": 5.0, "min": 2.0, "max": 10.0, "step": 0.5,
                    "tooltip": "HIGHER = characters with more matching tags are MUCH more likely",
                }),
                "popularity_bias": ("FLOAT", {
                    "default": 0.5, "min": 0.1, "max": 3.0, "step": 0.1,
                    "tooltip": "LOWER = more variety, HIGHER = favor popular characters",
                }),
                "random_seed": (["random", "fixed"], {"default": "random"}),
                "seed": ("INT", {
                    "default": 0, "min": 0, "max": 0xffffffffffffffff,
                    "tooltip": "Only used if random_seed is 'fixed'",
                }),
            }
        }

    RETURN_TYPES = ("STRING", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("character_triggers", "character_series", "character_core_tags", "debug_info")
    FUNCTION = "match_character"
    CATEGORY = "🎨 danbooru-tsc"

    def match_character(self, tags, max_db_rows, match_weight, popularity_bias, random_seed, seed):
        if not dblayer.db_exists():
            return self._no_match(
                f"danbooru.db not found at {dblayer.DB_PATH}. "
                "Run the 'Danbooru DB Build' node once."
            )

        actual_seed = int(time.time() * 1000) % (2**32) if random_seed == "random" else seed
        random.seed(actual_seed)

        input_tags = [t.strip().lower() for t in re.split(r"[,\n]", tags) if t.strip()]
        if not input_tags:
            return self._no_match("No valid tags provided")

        conn = dblayer.connect()
        try:
            total_db_rows = conn.execute("SELECT COUNT(*) FROM characters").fetchone()[0]
            actual_max_rows = min(max_db_rows, total_db_rows)

            placeholders = ",".join("?" * len(input_tags))
            query = f"""
                SELECT
                    c.id, c.trigger, c.core_tags, c.popularity_rank,
                    COUNT(DISTINCT ct.tag) AS match_count,
                    GROUP_CONCAT(DISTINCT ct.tag) AS matched_tags_list
                FROM characters c
                JOIN character_tags ct ON c.id = ct.character_id
                WHERE ct.tag IN ({placeholders})
                  AND c.popularity_rank < ?
                GROUP BY c.id
                ORDER BY match_count DESC, c.popularity_rank ASC
            """
            params = list(input_tags) + [actual_max_rows]
            results = conn.execute(query, params).fetchall()
        finally:
            conn.close()

        if not results:
            return self._no_match(f"No matches found in top {actual_max_rows:,} characters")

        match_count_distribution: dict[int, int] = {}
        matches = []
        for row in results:
            cid = row["id"]
            trigger = row["trigger"]
            core_tags = row["core_tags"]
            pop_rank = row["popularity_rank"]
            match_count = row["match_count"]
            matched_tags_str = row["matched_tags_list"]

            match_count_distribution[match_count] = match_count_distribution.get(match_count, 0) + 1
            matched_tags = matched_tags_str.split(",") if matched_tags_str else []

            match_score = match_count ** match_weight
            popularity_score = (1.0 / (pop_rank + 1)) ** popularity_bias
            total_weight = match_score * popularity_score

            matches.append({
                "trigger": trigger,
                "core_tags": core_tags,
                "matched_tags": matched_tags,
                "match_count": match_count,
                "weight": total_weight,
                "popularity_rank": pop_rank,
            })

        total_weight = sum(m["weight"] for m in matches)
        if total_weight == 0:
            return self._no_match("Total weight is zero")

        rand_val = random.uniform(0, total_weight)
        cumulative = 0
        selected = matches[-1]
        for match in matches:
            cumulative += match["weight"]
            if rand_val <= cumulative:
                selected = match
                break

        selection_prob = (selected["weight"] / total_weight) * 100
        matched_tags_str = ", ".join(selected["matched_tags"])

        debug_lines = [
            f"=== SEARCH RESULTS ===",
            f"Seed: {actual_seed} ({'random' if random_seed == 'random' else 'fixed'})",
            f"Database: {actual_max_rows:,} / {total_db_rows:,} characters searched",
            f"Input: {len(input_tags)} tags -> {', '.join(input_tags[:8])}{'...' if len(input_tags) > 8 else ''}",
            "",
            f"TOTAL CANDIDATES: {len(matches)}",
            "Match Distribution:",
        ]
        for match_cnt in sorted(match_count_distribution.keys(), reverse=True):
            count = match_count_distribution[match_cnt]
            debug_lines.append(f"  {match_cnt} tags matched: {count} characters")

        debug_lines.extend([
            "",
            f"SELECTED: {selected['trigger']}",
            f"  Matched: {selected['match_count']}/{len(input_tags)} tags",
            f"  Tags: {matched_tags_str}",
            f"  Probability: {selection_prob:.2f}%",
            f"  Rank: #{selected['popularity_rank']}",
            "",
            "Top 20 by Probability:",
        ])
        sorted_matches = sorted(matches, key=lambda m: m["weight"], reverse=True)
        for i, m in enumerate(sorted_matches[:20], 1):
            prob = (m["weight"] / total_weight) * 100
            marker = ">>>" if m is selected else "   "
            trigger_short = m["trigger"][:35] + "..." if len(m["trigger"]) > 35 else m["trigger"]
            debug_lines.append(
                f"{marker}{i:2}. {prob:6.2f}% | {m['match_count']} matches | "
                f"Rank #{m['popularity_rank']:5} | {trigger_short}"
            )
        if len(matches) > 20:
            remaining_prob = sum((m["weight"] / total_weight) * 100 for m in sorted_matches[20:])
            debug_lines.append(f"\n... and {len(matches) - 20} more candidates ({remaining_prob:.1f}% combined)")

        # Split the DB trigger ("hatsune miku, vocaloid") into name + series so
        # the matcher's outputs line up with the extractor's shape.
        char_disp, series_disp = split_character_trigger(selected["trigger"] or "")

        return (
            char_disp,
            series_disp,
            selected["core_tags"] or "",
            "\n".join(debug_lines),
        )

    def _no_match(self, reason):
        return ("", "", "", f"No match: {reason}")

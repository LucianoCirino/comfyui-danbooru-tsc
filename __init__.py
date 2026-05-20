"""comfyui-danbooru-tsc — danbooru tag/character/artist agent + utilities."""

from .nodes.agent import DanbooruAgent
from .nodes.matcher import DanbooruCharacterMatcher
from .nodes.random_image import DanbooruRandomImage
from .nodes.db_node import DanbooruDBBuild, DanbooruDBStats
from .nodes.sampler import RandomTagSampler
from .nodes.gelbooru_swap import GelbooruTagSwap
from .nodes.comfy_escape import ComfyTagEscape
from .nodes.tag_annotator import DanbooruTagAnnotator

NODE_CLASS_MAPPINGS = {
    "DanbooruAgent_tsc": DanbooruAgent,
    "RandomTagSampler_tsc": RandomTagSampler,
    "GelbooruTagSwap_tsc": GelbooruTagSwap,
    "ComfyTagEscape_tsc": ComfyTagEscape,
    "DanbooruTagAnnotator_tsc": DanbooruTagAnnotator,
    "DanbooruCharacterMatcher_tsc": DanbooruCharacterMatcher,
    "DanbooruRandomImage_tsc": DanbooruRandomImage,
    "DanbooruDBBuild_tsc": DanbooruDBBuild,
    "DanbooruDBStats_tsc": DanbooruDBStats,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "DanbooruAgent_tsc":            "🎨 Danbooru Character/Artist Extractor (LLM)",
    "RandomTagSampler_tsc":         "🎨 Random Tag Sampler",
    "GelbooruTagSwap_tsc":          "🎨 Gelbooru Tag Swap",
    "ComfyTagEscape_tsc":           "🎨 ComfyUI Tag Escape",
    "DanbooruTagAnnotator_tsc":     "🎨 Danbooru Tag Annotator",
    "DanbooruCharacterMatcher_tsc": "🎨 Danbooru Character Matcher",
    "DanbooruRandomImage_tsc":      "🎨 Danbooru Random Image",
    "DanbooruDBBuild_tsc":          "🎨 Danbooru DB Build",
    "DanbooruDBStats_tsc":          "🎨 Danbooru DB Stats",
}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]

"""comfyui-danbooru-tsc — danbooru tag/character/artist agent + utilities."""

from .nodes.agent import DanbooruAgent
from .nodes.matcher import DanbooruCharacterMatcher
from .nodes.random_image import DanbooruRandomImage
from .nodes.db_node import DanbooruDBBuild, DanbooruDBStats
from .nodes.refiner import DanbooruTagRefiner
from .nodes.composer import AnimaPromptComposer
from .nodes.sampler import RandomTagSampler
from .nodes.fast_enhancer import AnimaFastEnhancer
from .nodes.gelbooru_swap import GelbooruTagSwap

NODE_CLASS_MAPPINGS = {
    "DanbooruAgent_tsc": DanbooruAgent,
    "RandomTagSampler_tsc": RandomTagSampler,
    "DanbooruTagRefiner_tsc": DanbooruTagRefiner,
    "AnimaPromptComposer_tsc": AnimaPromptComposer,
    "AnimaFastEnhancer_tsc": AnimaFastEnhancer,
    "GelbooruTagSwap_tsc": GelbooruTagSwap,
    "DanbooruCharacterMatcher_tsc": DanbooruCharacterMatcher,
    "DanbooruRandomImage_tsc": DanbooruRandomImage,
    "DanbooruDBBuild_tsc": DanbooruDBBuild,
    "DanbooruDBStats_tsc": DanbooruDBStats,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "DanbooruAgent_tsc":            "🎨 Danbooru Agent (LLM)",
    "RandomTagSampler_tsc":         "🎨 Random Tag Sampler",
    "DanbooruTagRefiner_tsc":       "🎨 Danbooru Tag Refiner (LLM)",
    "AnimaPromptComposer_tsc":      "🎨 Anima Prompt Composer",
    "AnimaFastEnhancer_tsc":        "🎨 Anima Fast Enhancer (LLM)",
    "GelbooruTagSwap_tsc":          "🎨 Gelbooru Tag Swap",
    "DanbooruCharacterMatcher_tsc": "🎨 Danbooru Character Matcher",
    "DanbooruRandomImage_tsc":      "🎨 Danbooru Random Image",
    "DanbooruDBBuild_tsc":          "🎨 Danbooru DB Build",
    "DanbooruDBStats_tsc":          "🎨 Danbooru DB Stats",
}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]

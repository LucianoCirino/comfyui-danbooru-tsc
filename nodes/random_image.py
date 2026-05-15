"""Fetch random images for a danbooru tag (typically an artist), with
optional required-tag filtering. Moved from ComfyUI-invAIder-Nodes.
"""

from __future__ import annotations

from io import BytesIO

import numpy as np
import requests
import torch
from PIL import Image


class DanbooruRandomImage:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "artist_tag": ("STRING", {"default": "ebifurya", "multiline": False}),
                "num_images": ("INT", {"default": 1, "min": 1, "max": 20, "step": 1}),
            },
            "optional": {
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
                "required_tags": ("STRING", {"default": "1girl, solo", "multiline": True}),
            },
        }

    RETURN_TYPES = ("IMAGE", "STRING", "STRING")
    RETURN_NAMES = ("IMAGES", "TAGS", "URLS")
    FUNCTION = "fetch"
    CATEGORY = "🎨 danbooru-tsc"

    def parse_required_tags(self, required_tags_str):
        if not required_tags_str or not required_tags_str.strip():
            return set()
        tags_str = required_tags_str.replace(",", " ")
        tags = [t.strip().lower().replace(" ", "_") for t in tags_str.split()]
        return {t for t in tags if t}

    def post_has_required_tags(self, post_tags_str, required_tags):
        if not required_tags:
            return True
        post_tags = {t.lower() for t in post_tags_str.split()}
        return required_tags.issubset(post_tags)

    def fetch(self, artist_tag, num_images=1, seed=0, required_tags=""):
        tag = artist_tag.strip()
        if "danbooru.donmai.us" in tag and "tags=" in tag:
            tag = tag.split("tags=")[-1].split("&")[0]

        required_tags_set = self.parse_required_tags(required_tags)
        headers = {"User-Agent": "ComfyUI-danbooru-tsc/1.0"}

        images = []
        all_tags = []
        all_urls = []
        seen_ids = set()
        max_attempts = 20 if required_tags_set else 10
        attempt = 0

        while len(images) < num_images and attempt < max_attempts:
            attempt += 1
            fetch_count = min((num_images - len(images)) * 5, 20)
            url = f"https://danbooru.donmai.us/posts.json?tags={tag}+order:random&limit={fetch_count}"

            try:
                response = requests.get(url, headers=headers, timeout=30)
                response.raise_for_status()
                posts = response.json()
            except Exception as e:
                raise Exception(f"Danbooru API error: {e}")

            if not posts:
                if attempt == 1:
                    raise Exception(f"No posts found for tag: {tag}")
                break

            for post in posts:
                if len(images) >= num_images:
                    break
                post_id = post.get("id")
                if post_id in seen_ids:
                    continue
                seen_ids.add(post_id)

                post_tags_str = post.get("tag_string", "")
                if not self.post_has_required_tags(post_tags_str, required_tags_set):
                    continue

                image_url = post.get("large_file_url") or post.get("file_url")
                if not image_url:
                    continue
                if any(image_url.endswith(ext) for ext in (".mp4", ".webm", ".zip")):
                    continue

                try:
                    img_response = requests.get(image_url, headers=headers, timeout=60)
                    img_response.raise_for_status()
                    pil_image = Image.open(BytesIO(img_response.content))
                    if pil_image.mode != "RGB":
                        pil_image = pil_image.convert("RGB")
                    img_array = np.array(pil_image).astype(np.float32) / 255.0
                    images.append(torch.from_numpy(img_array))
                    all_tags.append(post_tags_str)
                    all_urls.append(image_url)
                except Exception as e:
                    print(f"Skipping image {image_url}: {e}")
                    continue

        if not images:
            if required_tags_set:
                raise Exception(f"No images found matching required tags: {', '.join(required_tags_set)}")
            raise Exception("Failed to load any images")

        if len(images) < num_images:
            print(f"Warning: Only found {len(images)} valid images out of {num_images} requested")

        if len(images) == 1:
            batch = images[0].unsqueeze(0)
        else:
            max_h = max(img.shape[0] for img in images)
            max_w = max(img.shape[1] for img in images)
            padded = []
            for img in images:
                h, w, _ = img.shape
                pad_top = (max_h - h) // 2
                pad_bottom = max_h - h - pad_top
                pad_left = (max_w - w) // 2
                pad_right = max_w - w - pad_left
                p = torch.nn.functional.pad(
                    img.permute(2, 0, 1),
                    (pad_left, pad_right, pad_top, pad_bottom),
                    mode="constant", value=0,
                ).permute(1, 2, 0)
                padded.append(p)
            batch = torch.stack(padded, dim=0)

        return (batch, "\n---\n".join(all_tags), "\n".join(all_urls))

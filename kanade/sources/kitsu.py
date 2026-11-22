"""Kitsu (kitsu.io) client: age ratings / NSFW flags, used as an extra content-rating source."""
from __future__ import annotations

from ..net import http

BASE = "https://kitsu.io/api/edge/anime"


def ratings(ids: list[int]) -> dict[int, dict]:
    """{kitsu_id: {"nsfw": bool, "age": "R18"/"R"/..., "guide": "..."}} for up to 20 ids per call."""
    out = {}
    for i in range(0, len(ids), 20):
        chunk = ids[i:i + 20]
        d = http().request_json("GET", BASE, params={
            "filter[id]": ",".join(str(x) for x in chunk),
            "fields[anime]": "nsfw,ageRating,ageRatingGuide",
            "page[limit]": "20",
        }, headers={"Accept": "application/vnd.api+json"}, max_age=60 * 86400, timeout=30, retries=3)
        for item in (d or {}).get("data", []):
            a = item.get("attributes") or {}
            out[int(item["id"])] = {"nsfw": bool(a.get("nsfw")), "age": a.get("ageRating") or "",
                                    "guide": a.get("ageRatingGuide") or ""}
    return out

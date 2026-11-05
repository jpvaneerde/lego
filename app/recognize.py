"""Part recognition per crop via Brickognize, plus BrickLink→Rebrickable id mapping."""
import asyncio
import logging

import httpx

from . import config

log = logging.getLogger("recognize")

_bl_to_rb: dict[str, str | None] = {}


async def brickognize(client: httpx.AsyncClient, jpeg: bytes) -> list[dict]:
    """Return candidate parts [{id, name, score, img_url}] best first."""
    files = {"query_image": ("piece.jpg", jpeg, "image/jpeg")}
    for url in (config.BRICKOGNIZE_URL, config.BRICKOGNIZE_FALLBACK_URL):
        for attempt in range(3):
            try:
                r = await client.post(url, files=files, timeout=45)
            except httpx.HTTPError as e:
                log.warning("brickognize error %s (attempt %d)", e, attempt + 1)
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code == 404:
                break  # try fallback URL
            if r.status_code == 429 or r.status_code >= 500:
                await asyncio.sleep(2.0 * (attempt + 1))
                continue
            r.raise_for_status()
            data = r.json()
            items = data.get("items") or []
            out = []
            for it in items:
                if it.get("type") not in (None, "part"):
                    continue
                out.append({
                    "id": str(it.get("id")),
                    "name": it.get("name") or "",
                    "score": float(it.get("score") or 0),
                    "img_url": it.get("img_url") or "",
                })
            return out[:5]
    raise RuntimeError("part recognition service unavailable")


async def to_rebrickable(client: httpx.AsyncClient, bl_id: str, known: set[str]) -> str | None:
    """Brickognize returns BrickLink ids; most equal Rebrickable's. Others are
    looked up through the Rebrickable API (if a key is configured)."""
    if bl_id in known:
        return bl_id
    if bl_id in _bl_to_rb:
        return _bl_to_rb[bl_id]
    rb = None
    if config.REBRICKABLE_API_KEY:
        try:
            r = await client.get(f"{config.REBRICKABLE_API}/lego/parts/",
                                 params={"bricklink_id": bl_id},
                                 headers={"Authorization": f"key {config.REBRICKABLE_API_KEY}"},
                                 timeout=20)
            if r.status_code == 200:
                res = r.json().get("results") or []
                if res:
                    rb = res[0]["part_num"]
            await asyncio.sleep(1.0)  # Rebrickable asks for ~1 request/second
        except httpx.HTTPError as e:
            log.warning("rebrickable lookup failed for %s: %s", bl_id, e)
    _bl_to_rb[bl_id] = rb
    return rb


async def alternates(set_num: str) -> list[dict]:
    """MOCs (alternate builds) designed from a set's parts."""
    if not config.REBRICKABLE_API_KEY:
        return []
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{config.REBRICKABLE_API}/lego/sets/{set_num}/alternates/",
                             params={"page_size": 12},
                             headers={"Authorization": f"key {config.REBRICKABLE_API_KEY}"}, timeout=20)
        if r.status_code != 200:
            return []
        return [{"set_num": m.get("set_num"), "name": m.get("name"), "img_url": m.get("moc_img_url"),
                 "url": m.get("moc_url"), "num_parts": m.get("num_parts"),
                 "designer": m.get("designer_name")} for m in r.json().get("results", [])]

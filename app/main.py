import asyncio
import logging
import threading
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path

import httpx
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import catalog, config, ideas, recognize
from .colors import Palette
from .segment import decode_image, preview_jpeg, segment

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("brickpile")

app = FastAPI(title="Brickpile")
STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")

JOBS: dict[str, dict] = {}
JOB_TTL = 3 * 3600
_palette: Palette | None = None
_palette_at = 0.0


# ------------------------------------------------------------ helpers ---

def palette() -> Palette | None:
    global _palette, _palette_at
    built = catalog.status().get("built_at")
    if catalog.status()["status"] != "ready":
        return None
    if _palette is None or (built and built > _palette_at):
        con = catalog.connect()
        _palette = Palette(catalog.colors(con))
        con.close()
        _palette_at = built or time.time()
    return _palette


_hits: dict[str, deque] = defaultdict(deque)


def rate_limit(request: Request, bucket: str, per_hour: int) -> None:
    if per_hour <= 0:
        return
    ip = (request.headers.get("x-forwarded-for") or request.client.host or "?").split(",")[0].strip()
    q = _hits[f"{bucket}:{ip}"]
    now = time.time()
    while q and q[0] < now - 3600:
        q.popleft()
    if len(q) >= per_hour:
        raise HTTPException(429, "Too many requests, try again later.")
    q.append(now)


def cleanup_jobs() -> None:
    cutoff = time.time() - JOB_TTL
    for jid in [j for j, v in JOBS.items() if v["created"] < cutoff]:
        JOBS.pop(jid, None)


# ------------------------------------------------------------ lifecycle ---

@app.on_event("startup")
def startup() -> None:
    threading.Thread(target=catalog.refresher_loop, daemon=True).start()


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/status")
def api_status():
    return {"catalog": catalog.status(), "ideas": ideas.available(),
            "alternates": bool(config.REBRICKABLE_API_KEY),
            "max_images": config.MAX_IMAGES}


@app.get("/api/colors")
def api_colors():
    if catalog.status()["status"] != "ready":
        raise HTTPException(503, "Catalog is still loading")
    con = catalog.connect()
    try:
        rows = catalog.colors(con)
    finally:
        con.close()
    # Common colours first, then the rest alphabetically.
    rows.sort(key=lambda r: (-(r["num_parts"] or 0) if (r["num_parts"] or 0) >= 1500 else 1, r["name"]))
    return rows


# ------------------------------------------------------------ recognition jobs ---

@app.post("/api/jobs")
async def create_job(request: Request, images: list[UploadFile] = File(...)):
    rate_limit(request, "jobs", config.RATE_JOBS_PER_HOUR)
    if catalog.status()["status"] != "ready":
        raise HTTPException(503, "The parts catalog is still loading, try again in a minute.")
    if not images or len(images) > config.MAX_IMAGES:
        raise HTTPException(400, f"Upload 1 to {config.MAX_IMAGES} photos.")
    blobs = []
    for f in images:
        data = await f.read(config.MAX_IMAGE_BYTES + 1)
        if len(data) > config.MAX_IMAGE_BYTES:
            raise HTTPException(413, f"{f.filename} is too large.")
        blobs.append(data)

    cleanup_jobs()
    jid = uuid.uuid4().hex[:12]
    JOBS[jid] = {"id": jid, "created": time.time(), "state": "segmenting", "done": 0, "total": 0,
                 "pieces": [], "photos": [], "crops": {}, "error": None, "warnings": []}
    asyncio.create_task(run_job(jid, blobs))
    return {"id": jid}


async def run_job(jid: str, blobs: list[bytes]) -> None:
    job = JOBS[jid]
    try:
        pal = palette()
        crops = []
        for pi, data in enumerate(blobs):
            img = await asyncio.to_thread(decode_image, data)
            job["photos"].append(await asyncio.to_thread(preview_jpeg, img))
            pieces = await asyncio.to_thread(segment, img, config.MAX_PIECES_PER_IMAGE)
            if not pieces:
                job["warnings"].append(f"No pieces found in photo {pi + 1}. Use a plain, contrasting background.")
            if len(pieces) >= config.MAX_PIECES_PER_IMAGE:
                job["warnings"].append(f"Photo {pi + 1} hit the {config.MAX_PIECES_PER_IMAGE}-piece limit; split it into more photos.")
            for p in pieces:
                crops.append((pi, p))

        job["total"] = len(crops)
        job["state"] = "recognising"
        con = catalog.connect()
        sem = asyncio.Semaphore(config.BRICKOGNIZE_CONCURRENCY)
        failures = 0

        async with httpx.AsyncClient(headers={"User-Agent": "brickpile/1.0"}) as client:
            async def one(idx: int, photo_idx: int, piece):
                nonlocal failures
                async with sem:
                    try:
                        cands = await recognize.brickognize(client, piece.crop_jpeg)
                    except Exception as e:
                        log.warning("recognition failed: %s", e)
                        cands, failures = [], failures + 1
                known = catalog.part_info(con, [c["id"] for c in cands])
                for c in cands:
                    c["part_num"] = await recognize.to_rebrickable(client, c["id"], set(known))
                    if c["part_num"] and c["part_num"] not in known:
                        known.update(catalog.part_info(con, [c["part_num"]]))
                    if c["part_num"] in known:
                        c["catalog_name"] = known[c["part_num"]]["name"]
                cands = [c for c in cands if c.get("part_num")] or cands
                col = pal.nearest(piece.lab) if pal else []
                job["crops"][idx] = piece.crop_jpeg
                best = cands[0] if cands else None
                job["pieces"].append({
                    "idx": idx, "photo": photo_idx, "bbox": piece.bbox,
                    "candidates": cands,
                    "part_num": best["part_num"] if best else None,
                    "part_name": (best.get("catalog_name") or best["name"]) if best else None,
                    "score": best["score"] if best else 0,
                    "colors": col,
                    "color_id": col[0]["id"] if col else None,
                    "color_name": col[0]["name"] if col else None,
                    "include": bool(best and best.get("part_num")),
                })
                job["done"] += 1

            await asyncio.gather(*(one(i, pi, p) for i, (pi, p) in enumerate(crops)))
        con.close()
        job["pieces"].sort(key=lambda p: p["idx"])
        if failures and failures == len(crops):
            job["error"] = "The part recognition service did not respond. Try again later."
            job["state"] = "error"
        else:
            if failures:
                job["warnings"].append(f"{failures} pieces could not be recognised.")
            job["state"] = "done"
    except Exception as e:
        log.exception("job %s failed", jid)
        job["state"], job["error"] = "error", str(e)


@app.get("/api/jobs/{jid}")
def get_job(jid: str):
    job = JOBS.get(jid)
    if not job:
        raise HTTPException(404, "Unknown or expired job")
    return {k: v for k, v in job.items() if k not in ("crops", "photos")} | {"photos": len(job["photos"])}


@app.get("/api/jobs/{jid}/crops/{idx}.jpg")
def get_crop(jid: str, idx: int):
    job = JOBS.get(jid)
    if not job or idx not in job["crops"]:
        raise HTTPException(404)
    return Response(job["crops"][idx], media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})


@app.get("/api/jobs/{jid}/photos/{idx}.jpg")
def get_photo(jid: str, idx: int):
    job = JOBS.get(jid)
    if not job or idx >= len(job["photos"]):
        raise HTTPException(404)
    return Response(job["photos"][idx], media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})


# ------------------------------------------------------------ matching / ideas ---

class Part(BaseModel):
    part_num: str
    color_id: int = -1
    qty: int = Field(1, ge=1, le=10000)
    part_name: str | None = None
    color_name: str | None = None


class MatchReq(BaseModel):
    parts: list[Part]
    mode: str = "color"
    limit: int = Field(24, ge=1, le=60)


@app.post("/api/match")
def api_match(req: MatchReq):
    if catalog.status()["status"] != "ready":
        raise HTTPException(503, "Catalog is still loading")
    mode = "color" if req.mode == "color" else "shape"
    con = catalog.connect()
    try:
        have = [p.model_dump() for p in req.parts]
        sets = catalog.match_sets(con, have, mode=mode, limit=req.limit)
        for s in sets:
            miss = catalog.missing_parts(con, s.pop("_inv_id"), mode=mode)
            s["missing"] = miss[:40]
            s["missing_count"] = sum(m["qty"] for m in miss)
        return {"mode": mode, "pieces": sum(p.qty for p in req.parts), "sets": sets}
    finally:
        con.close()


class IdeasReq(BaseModel):
    parts: list[Part]
    job_id: str | None = None
    audience: str = ""
    count: int = Field(4, ge=1, le=6)


@app.post("/api/ideas")
async def api_ideas(req: IdeasReq, request: Request):
    if not ideas.available():
        raise HTTPException(501, "Build ideas are not configured (no ANTHROPIC_API_KEY).")
    rate_limit(request, "ideas", config.RATE_IDEAS_PER_HOUR)
    photo = None
    if req.job_id and req.job_id in JOBS and JOBS[req.job_id]["photos"]:
        photo = JOBS[req.job_id]["photos"][0]
    try:
        result = await asyncio.to_thread(ideas.generate, [p.model_dump() for p in req.parts],
                                         photo, req.audience, req.count)
    except Exception as e:
        log.exception("ideas failed")
        raise HTTPException(502, f"Idea generation failed: {e}")
    return {"ideas": result}


@app.get("/api/sets/{set_num}/alternates")
async def api_alternates(set_num: str):
    return {"mocs": await recognize.alternates(set_num)}


@app.exception_handler(HTTPException)
async def http_exc(_, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

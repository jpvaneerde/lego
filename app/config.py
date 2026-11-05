import os

DATA_DIR = os.environ.get("DATA_DIR", "/data")
CATALOG_DB = os.path.join(DATA_DIR, "catalog.sqlite")
CATALOG_MAX_AGE_DAYS = float(os.environ.get("CATALOG_MAX_AGE_DAYS", "7"))
REBRICKABLE_DOWNLOADS = os.environ.get(
    "REBRICKABLE_DOWNLOADS", "https://cdn.rebrickable.com/media/downloads"
)
REBRICKABLE_API = os.environ.get("REBRICKABLE_API", "https://rebrickable.com/api/v3")
REBRICKABLE_API_KEY = os.environ.get("REBRICKABLE_API_KEY", "")

BRICKOGNIZE_URL = os.environ.get("BRICKOGNIZE_URL", "https://api.brickognize.com/predict/parts/")
BRICKOGNIZE_FALLBACK_URL = os.environ.get("BRICKOGNIZE_FALLBACK_URL", "https://api.brickognize.com/predict/")
BRICKOGNIZE_CONCURRENCY = int(os.environ.get("BRICKOGNIZE_CONCURRENCY", "4"))

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")

MAX_IMAGES = int(os.environ.get("MAX_IMAGES", "6"))
MAX_IMAGE_BYTES = int(os.environ.get("MAX_IMAGE_BYTES", str(25 * 1024 * 1024)))
MAX_PIECES_PER_IMAGE = int(os.environ.get("MAX_PIECES_PER_IMAGE", "250"))

# Simple per-IP limits (requests per hour). 0 disables.
RATE_JOBS_PER_HOUR = int(os.environ.get("RATE_JOBS_PER_HOUR", "30"))
RATE_IDEAS_PER_HOUR = int(os.environ.get("RATE_IDEAS_PER_HOUR", "15"))

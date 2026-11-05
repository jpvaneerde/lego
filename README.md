# lego.jplay — lego.jplay.cloud

Upload photos of loose bricks spread out on a plain background. lego.jplay
finds each piece (OpenCV), identifies it (Brickognize), estimates its colour,
lets you correct the inventory, then ranks official sets by how much of each
your pile covers (Rebrickable catalog, local SQLite) and asks Claude for
original build ideas.

## Config (env)
| Var | Default | |
|---|---|---|
| `ANTHROPIC_API_KEY` | – | enables the Ideas tab |
| `REBRICKABLE_API_KEY` | – | optional: BrickLink→Rebrickable id lookups and "alternate builds" |
| `CLAUDE_MODEL` | `claude-sonnet-5-5` | |
| `DATA_DIR` | `/data` | catalog is built here on start, refreshed weekly |
| `RATE_JOBS_PER_HOUR` / `RATE_IDEAS_PER_HOUR` | 30 / 15 | per IP |

## Deploy
Argo CD manifests: `jpvaneerde/argocd` › `apps/lego`. API keys go in the `lego-api-keys` secret (see `api-keys-secret.example.yaml` there).

## Run locally
    pip install -r requirements.txt
    DATA_DIR=./data uvicorn app.main:app --port 8080

First start downloads the Rebrickable dumps and builds the catalog (about a minute).

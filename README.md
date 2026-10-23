# CT-200 traceable test-case generator

FastAPI + SQLAlchemy/SQLite (tree, versions, selections) + JSON-file or MongoDB store (LLM output).
Turns the CT-200 manual PDF into a versioned section tree, lets you select sections, generates 3–5 QA test cases with an
LLM, and tells you at retrieval time whether those test cases still match the current document.
Design rationale and the decision log are in [APPROACH.md](APPROACH.md).

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then edit LLM_* (see below)
uvicorn app.main:create_app --factory --reload      # http://localhost:8000/docs
```

| Env var | Default | Notes |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./data/ct200.db` | tree, versions, selections |
| `STORE_BACKEND` | `json` | `json` (file per generation in `JSON_STORE_DIR`) or `mongo` (`MONGO_URI`, `MONGO_DB`) |
| `LLM_PROVIDER` | `openai_compat` | `mock` = deterministic offline stand-in for demos only (responses say `model: "mock"`) |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | Groq / – / llama-3.3-70b | any OpenAI-compatible endpoint: Groq, OpenRouter, Gemini (`https://generativelanguage.googleapis.com/v1beta/openai`) |
| `LLM_JSON_MODE` | `true` | sends `response_format=json_object`; set `false` if your provider rejects it |
| `LLM_MAX_ATTEMPTS` | `3` | validation retries (transport errors are retried separately, 2x with backoff) |
| `MAX_SOURCE_CHARS` | `20000` | selections larger than this are rejected with 413, never truncated |
| `MATCH_FUZZY_THRESHOLD` | `0.72` | version matcher, fuzzy pass only |

With no `LLM_API_KEY` and `LLM_PROVIDER=openai_compat`, `/generate` returns 503 (it does **not** silently fall back to the mock).

## Run the tests

```bash
pytest -q          # 43 tests: parser irregularities, matcher, LLM validation/retry, store contract, end-to-end flow
```
The Mongo contract test runs against `mongomock`; it has not been run against a real `mongod`.

## The v1 → v2 flow (curl)

```bash
# 1. ingest v1 (creates the document; note document_id in the response)
curl -F file=@data/ct200_manual.pdf localhost:8000/documents/ingest
# 2. browse: ids are stable "logical" node ids
curl localhost:8000/documents/1/tree
curl "localhost:8000/documents/1/search?q=Error%20Codes&scope=heading"      # 4.2 AND 7.1: distinct ids
# 3. select (pinned to v1) and generate
curl -X POST localhost:8000/selections -H 'content-type: application/json' \
     -d '{"name":"inflation+errors","node_ids":["<id of 3.2>","<id of 4.2>"]}'
curl -X POST localhost:8000/selections/<selection_id>/generate               # same call again => reused; ?force=true => new
# 4. re-ingest the modified manual AS A NEW VERSION of the same document
curl -F file=@data/ct200_manual_v2.pdf -F document_id=1 localhost:8000/documents/ingest
# 5. what changed? and are my test cases still valid?
curl localhost:8000/nodes/<id of 3.2>/changes
curl localhost:8000/selections/<selection_id>/generations                    # staleness per generation AND per test case
curl localhost:8000/nodes/<id of 3.2>/generations                            # same, by node id
curl "localhost:8000/selections/<selection_id>/generations?against_version=1"  # still fresh against the version it was made from
curl localhost:8000/selections/<selection_id>                                # pinned: still returns the exact v1 text
```
Or run it all: `LLM_PROVIDER=mock uvicorn app.main:create_app --factory` then `python scripts/demo.py`.

## API summary

| | |
|---|---|
| `POST /documents/ingest` (multipart `file`, optional `document_id`, `allow_errors`) | new document v1, or new version; 422 if the parser reports errors; same file as latest version is a no-op (200) |
| `GET /documents`, `/documents/{id}/versions`, `/documents/{id}/versions/{n}/report` | versions, parser issues, ingest diff summary |
| `GET /documents/{id}/sections?version=` · `/tree?version=` · `/search?q=&scope=heading\|body\|both&version=` | browse (default latest) |
| `GET /nodes/{node_id}?version=` | text, hash, children, breadcrumbs, references, flags |
| `GET /nodes/{node_id}/changes?from_version=&to_version=` | changed?, diff summary, subtree changes |
| `POST /selections` · `GET /selections/{id}` | version-pinned selections (descendants included by default) |
| `POST /selections/{id}/generate?force=` | 201 new · 200 reused · 502 invalid LLM output (stored as failed) · 413 too large · 503 no LLM |
| `GET /selections/{id}/generations` · `/nodes/{node_id}/generations` · `/generations/{id}` | stored test cases **with staleness** (`?against_version=`, `?include_failed=`) |

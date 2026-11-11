# Deploying to Railway

One service (API + console + embedded worker), one Postgres, one volume.

## 1. Create the project

1. railway.com → **New Project** → **Deploy from GitHub repo** → `apkhadzeangio/medical-translation-studio`.
2. In the project: **Create** → **Database** → **PostgreSQL**.
3. On the app service: **Settings → Volumes → Add volume**, mount path `/data`.
4. On the app service: **Settings → Networking → Generate Domain**.

Railway builds the `Dockerfile`; `railway.toml` runs `alembic upgrade head` before each deploy
and waits for `/health/db`.
The container also runs `alembic upgrade head` when it starts, so the schema is created even if
the pre-deploy step is skipped or overridden in the dashboard. `/health/db` answers 503 until the
database is at the revision the code needs, which keeps the previous deployment serving.

## 2. Variables (app service → Variables)

| Variable | Value |
|---|---|
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` |
| `ENVIRONMENT` | `production` |
| `API_TOKEN` | a long random string; this is the console's access code |
| `STORAGE_DIR` | `/data` |
| `EMBEDDED_WORKER` | `true` |
| `WORKER_THREADS` | `2` |
| `LLM_API_KEYS` | the model provider key (several: comma-separated) |
| `LLM_BASE_URL` | `https://api.openai.com/v1` (or another OpenAI-compatible endpoint) |
| `LLM_MODEL` | `gpt-6-astra` |
| `LLM_PRICES` | `{"gpt-6-astra": {"input": 10, "cached_input": 1, "output": 50}}` |
| `LLM_BUDGET_USD` | hard cap across all projects, e.g. `20` |
| `LLM_MAX_OUTPUT_TOKENS` | optional; cap on one call's output tokens, reasoning included (e.g. `16000`). A cut-off answer fails the job instead of being retried |
| `PDF_STRUCTURE_MODEL` | `all` (default): the model reads every page image to decide structure, one request per page. `complex`: only pages with figures, tables or formulas. `off`: geometric parser alone |
| `LLM_QA_MODEL` | optional; leave empty to skip model QA. Prefer another vendor's model, with `LLM_QA_BASE_URL` and `LLM_QA_API_KEYS`, and add its price to `LLM_PRICES` |

With `ENVIRONMENT=production` the app refuses to start without `API_TOKEN`.

## 3. First run

Open the generated domain. Enter the access code, create a project with a small budget, upload
a PDF, wait for "დამუშავებულია", press "თარგმნა", then download the DOCX or PDF.

## Checking a deployment

| URL | Healthy answer |
|---|---|
| `/health/db` | `{"status":"ok","revision":"0004"}` |
| `/health/worker` | `"status":"ok"`, `"alive"` equal to `WORKER_THREADS`, `"llm_keys"` at least 1, `"llm_model"` as configured |

`/health/worker` answers 503 if the worker threads are not running or cannot read the job queue.
`"llm_keys": 0` means `LLM_API_KEYS` is not set on this service. A valid key is only proven by a
translation: the `llm_calls` table and the console's cost panel show each request and its error.

## Notes

- Keep it to one replica: the worker runs inside the API process and files live on the volume.
- The model key lives only in Railway variables. It is never stored in the database or the repo.
- To change the key, edit `LLM_API_KEYS`; Railway redeploys. Jobs in flight are retried.
- Every model request is in the `llm_calls` table; the console shows the total per stage.

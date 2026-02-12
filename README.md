# Docker Deployment (Recommended)

## Local Docker Compose (mykeeta + gpt-load + Postgres)

This repo includes a `docker-compose.yml` that starts:

- `postgres` (required for gpt-load; avoids SQLite locking)
- `gpt-load` on `http://localhost:3001` (UI + import API)
- `mykeeta` (generates keys and imports them into gpt-load if `GPT_LOAD_AUTH_KEY` is set)

Recommended: use a `.env` file (see `.env.example`) or set env vars in your shell:

- `EMAIL_PROVIDER` (optional: `gptmail` default; auto `duckmail` if `duckmail_apikey` is set)
- `GPTMAIL_API_KEY` (required when provider=`gptmail`)
- `duckmail_apikey` (required when provider=`duckmail`)
- `GPT_LOAD_AUTH_KEY` (required if you want auto-import into gpt-load)
- `GPT_LOAD_GROUP_NAME` (optional, default `#pinhaofan`)
- `POSTGRES_PASSWORD` (optional, default `123456`)

Run:

```bash
docker compose up --build --abort-on-container-exit
```

Then open gpt-load:

- `http://localhost:3001`

## HuggingFace Spaces (Docker Space)

This repo is a job-style container (generate keys then exit). HF Spaces expects a long-running
web process, so the image runs `hf_server.py` by default. For HF, the container also starts a
co-located `gpt-load` service and reverse-proxies it.

Note: `gpt-load` is built from source at image build time (to support forks without prebuilt images).

After deploying the Docker Space:

- Open the Space URL (`/`) -> GPT-Load management UI
- Open `/log` -> key generator runner + tail logs
- Or trigger runs via `POST /run`
- Check `GET /status` for tail logs / exit code (JSON)

If you want it to run continuously, set one of the following env vars:

- `AUTO_RUN_ON_START=1` (run once on boot)
- `RUN_EVERY_MINUTES=60` (run periodically)
- or `RUN_EVERY_SECONDS=3600`

Set env vars in Space Settings -> Variables / Secrets:

- `EMAIL_PROVIDER` (optional)
- `GPTMAIL_API_KEY` (required when provider=`gptmail`)
- `duckmail_apikey` (required when provider=`duckmail`)
- `GPT_LOAD_AUTH_KEY` (optional, for GPT-Load sync)
- `GPT_LOAD_BASE_URL` (optional)
- `GPT_LOAD_GROUP_NAME` (optional)
- `GPT_LOAD_DATABASE_DSN` (recommended for gpt-load; avoid SQLite locking)


3) Go to Zeabur -> New Project -> Import from GitHub -> select your fork
4) Deploy using the included `Dockerfile`
5) Set environment variables in Zeabur (Settings -> Environment Variables):

- `EMAIL_PROVIDER` (optional)
- `GPTMAIL_API_KEY` (required when provider=`gptmail`)
- `duckmail_apikey` (required when provider=`duckmail`)
- `GPT_LOAD_BASE_URL` (optional)
- `GPT_LOAD_GROUP_NAME` (optional)
- `GPT_LOAD_AUTH_KEY` (optional)

```bash
# Email provider (choose one)
EMAIL_PROVIDER=duckmail
duckmail_apikey=dk_xxx

# or
EMAIL_PROVIDER=gptmail
GPTMAIL_API_KEY=sk-xxxx

# optional (GPT-Load)
GPT_LOAD_BASE_URL=xxx
GPT_LOAD_GROUP_NAME=#xxxx
GPT_LOAD_AUTH_KEY=your-auth-key
```

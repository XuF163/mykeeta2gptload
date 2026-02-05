# Docker Deployment (Recommended)

## HuggingFace Spaces (Docker Space)

This repo is a job-style container (generate keys then exit). HF Spaces expects a long-running
web process, so the image runs `hf_server.py` by default.

After deploying the Docker Space:

- Open the Space URL, click `Run Job`
- Or call `POST /run`
- Check `GET /status` for tail logs / exit code

If you want it to run continuously, set one of the following env vars:

- `AUTO_RUN_ON_START=1` (run once on boot)
- `RUN_EVERY_MINUTES=60` (run periodically)
- or `RUN_EVERY_SECONDS=3600`

Set env vars in Space Settings -> Variables / Secrets:

- `GPTMAIL_API_KEY` (required)
- `GPT_LOAD_AUTH_KEY` (optional, for GPT-Load sync)
- `GPT_LOAD_BASE_URL` (optional)
- `GPT_LOAD_GROUP_NAME` (optional)


3) Go to Zeabur -> New Project -> Import from GitHub -> select your fork
4) Deploy using the included `Dockerfile`
5) Set environment variables in Zeabur (Settings -> Environment Variables):

- `GPTMAIL_API_KEY` (required)
- `GPT_LOAD_BASE_URL` (optional)
- `GPT_LOAD_GROUP_NAME` (optional)
- `GPT_LOAD_AUTH_KEY` (optional)

```bash
# required
GPTMAIL_API_KEY=sk-xxxx

# optional (GPT-Load)
GPT_LOAD_BASE_URL=xxx
GPT_LOAD_GROUP_NAME=#xxxx
GPT_LOAD_AUTH_KEY=your-auth-key
```

# Docker Deployment (Recommended)

## HuggingFace Spaces (Docker Space)

This repo is a job-style container (generate keys then exit). HF Spaces expects a long-running
web process, so the image runs `hf_server.py` by default.

After deploying the Docker Space:

- Open the Space URL, click `Run Job`
- Or call `POST /run`
- Check `GET /status` for tail logs / exit code

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

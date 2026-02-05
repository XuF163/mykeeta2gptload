# Docker Deployment (Recommended)


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

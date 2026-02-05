# LongCat API Key Automation

This repo now includes a small workflow to:

1) Open `passport.mykeeta.com` login URL
2) Click `Continue with email`
3) Receive OTP via GPTMail (temporary email)
4) Create an API key on `https://longcat.chat/platform/api_keys`
5) (Optional) Go to Usage -> Apply more quota -> fill Industry/Scenario -> agree -> submit

## Requirements

- Network access to `passport.mykeeta.com` and `longcat.chat`.

## Run

```bash
uv run python run.py
```

The script prints (one line per key):

- `LongCat API Key: ak_...` (copyable)
- A JSON array at the end (machine-readable)

It appends:

- keys to `temp/longcat_keys.txt`
- records to `temp/longcat_keys.csv`

All paths and the key count are configured in `config.toml` under `[longcat]`.

## GPT-Load Auto Sync (Optional)

This repo can *best-effort* submit newly generated keys to an existing GPT-Load deployment
without modifying the server (client-side only).

Default target:

- Base URL: `https://great429gptload.zeabur.app`
- Group: `#pinhaofan`

Configure `config.toml`:

```toml
[gpt_load]
enabled = true
base_url = "https://great429gptload.zeabur.app"
group_name = "#pinhaofan"
auth_key = "YOUR_GPT_LOAD_AUTH_KEY"
poll = true
force = false
poll_timeout_s = 120
poll_interval_s = 1
```

Note: GPT-Load sync settings are read from `config.toml` (no environment variables needed).
For Docker deployments, you can override via environment variables:

- `GPTMAIL_API_KEY`
- `GPT_LOAD_BASE_URL`
- `GPT_LOAD_GROUP_NAME`
- `GPT_LOAD_AUTH_KEY`

Manual sync command (reads a keys file and submits new keys):

```bash
uv run python run.py gpt-load-sync temp/longcat_keys.txt
```

## Default Behavior (No Args)

If you run without args, it will generate `[longcat].keys_count` keys and (optionally)
sync each key to GPT-Load right after generation (if `[gpt_load].enabled=true`).

## Smoke Test (Normal Chat)

OpenAI-compatible chat endpoint:

- `POST https://api.longcat.chat/openai/v1/chat/completions`

Run the included smoke test:

```bash
set LONGCAT_API_KEY=ak_...
python longcat_smoke.py
```

Or directly:

```bash
set LONGCAT_API_KEY=ak_...
python longcat_smoke.py
```

API smoke testing does not use proxies in this trimmed repo.

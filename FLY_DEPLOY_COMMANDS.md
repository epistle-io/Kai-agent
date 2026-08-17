# Fly.io Deployment Commands (Copy-Paste)

This file gives the exact commands to deploy the current backend safely.

## 1) Login and select app

```bash
fly auth login
fly apps list
fly status -a kai-backend
```

## 2) Set production secrets

Run once, then update whenever keys change.

PowerShell note: each `NAME=VALUE` must stay on the same command invocation. If you split lines without continuation, PowerShell treats them as separate commands.

### Option A: PowerShell one-liner (safest)

```powershell
fly secrets set GROQ_API_KEY="replace_with_groq_key" METAAPI_ACCOUNT_ID="replace_with_metaapi_account_id" KAI_API_KEY="replace_with_strong_random_string" KAI_CORS_ORIGINS="https://kai.yourdomain.com" PAIR_1="EURUSDm" PAIR_2="GBPUSDm" PAIR_3="BTCUSDm" DEFAULT_TIMEFRAME="M5" CANDLES_TO_ANALYZE="100" CHECK_INTERVAL_MINUTES="30" MAX_RISK_PERCENT="1.0" LOT_SIZE_MODE="risk" FIXED_LOT_SIZE="0.02" MIN_LOT_SIZE="0.01" MAX_LOT_SIZE="10.0" LOT_SIZE_STEP="0.01" MIN_ATR_PCT="0.03" MAX_ATR_PCT="1.50" MIN_TREND_STRENGTH_PCT="0.02" KAI_DB_PATH="memory/kai_memory.db" -a kai-backend
```

Then set the long token separately:

```powershell
fly secrets set METAAPI_TOKEN="replace_with_metaapi_token" GROQ_API_KEY="replace_with_groq_key" -a kai-backend
```

### Option B: PowerShell multiline (with backticks)

```powershell
fly secrets set `
  GROQ_API_KEY="replace_with_groq_key" `
  METAAPI_TOKEN="replace_with_metaapi_token" `
  METAAPI_ACCOUNT_ID="replace_with_metaapi_account_id" `
  KAI_API_KEY="replace_with_strong_random_string" `
  KAI_CORS_ORIGINS="https://kai.yourdomain.com" `
  PAIR_1="EURUSDm" `
  PAIR_2="GBPUSDm" `
  PAIR_3="BTCUSDm" `
  DEFAULT_TIMEFRAME="M5" `
  CANDLES_TO_ANALYZE="100" `
  CHECK_INTERVAL_MINUTES="30" `
  MAX_RISK_PERCENT="1.0" `
  LOT_SIZE_MODE="risk" `
  FIXED_LOT_SIZE="0.02" `
  MIN_LOT_SIZE="0.01" `
  MAX_LOT_SIZE="10.0" `
  LOT_SIZE_STEP="0.01" `
  MIN_ATR_PCT="0.03" `
  MAX_ATR_PCT="1.50" `
  MIN_TREND_STRENGTH_PCT="0.02" `
  KAI_DB_PATH="memory/kai_memory.db" `
  -a kai-backend
```

```bash
fly secrets set \
  GROQ_API_KEY="replace_with_groq_key" \
  METAAPI_TOKEN="replace_with_metaapi_token" \
  METAAPI_ACCOUNT_ID="replace_with_metaapi_account_id" \
  KAI_API_KEY="replace_with_strong_random_string" \
  KAI_CORS_ORIGINS="https://kai.yourdomain.com" \
  PAIR_1="EURUSDm" \
  PAIR_2="GBPUSDm" \
  PAIR_3="BTCUSDm" \
  DEFAULT_TIMEFRAME="M5" \
  CANDLES_TO_ANALYZE="100" \
  CHECK_INTERVAL_MINUTES="30" \
  MAX_RISK_PERCENT="1.0" \
  LOT_SIZE_MODE="risk" \
  FIXED_LOT_SIZE="0.02" \
  MIN_LOT_SIZE="0.01" \
  MAX_LOT_SIZE="10.0" \
  LOT_SIZE_STEP="0.01" \
  MIN_ATR_PCT="0.03" \
  MAX_ATR_PCT="1.50" \
  MIN_TREND_STRENGTH_PCT="0.02" \
  KAI_DB_PATH="memory/kai_memory.db" \
  -a kai-backend
```

## 3) Scale VM memory (recommended)

Current workload is safer on 1GB than 512MB.

```bash
fly scale memory 1024 -a kai-backend
```

If you need to go back:

```bash
fly scale memory 512 -a kai-backend
```

## 4) Deploy

From the kai-railway folder:

```bash
fly deploy -a kai-backend
```

## 5) Verify health and logs

```bash
fly logs -a kai-backend
fly status -a kai-backend
```

Optional direct checks:

```bash
curl https://kai-backend.fly.dev/ping
curl https://kai-backend.fly.dev/health
curl https://kai-backend.fly.dev/status
```

## 6) Test secured endpoints (API key)

```bash
curl -X POST "https://kai-backend.fly.dev/scan" \
  -H "X-API-Key: replace_with_strong_random_string"
```

Emergency mode on/off:

```bash
curl -X POST "https://kai-backend.fly.dev/controls/approvals" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: replace_with_strong_random_string" \
  -d '{"enabled": false}'

curl -X POST "https://kai-backend.fly.dev/controls/approvals" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: replace_with_strong_random_string" \
  -d '{"enabled": true}'
```

## 7) Mobile app config sync

In mobile app config, set:

- extra.kaiServerUrl = your Fly URL
- extra.kaiApiKey = same value as KAI_API_KEY
- extra.checkIntervalMinutes = same as CHECK_INTERVAL_MINUTES

## 8) Rotate secrets (important)

If any secret was committed or shared accidentally, rotate immediately:

```bash
fly secrets set GROQ_API_KEY="new_key" METAAPI_TOKEN="new_key" -a kai-backend
```

## 9) Backup export/import for server migration

Create backup on server:

```bash
curl -X POST "https://kai-backend.fly.dev/admin/backup-db" \
  -H "X-API-Key: replace_with_strong_random_string"
```

Download backup directly to your local machine (creates fresh backup first):

```bash
curl -L "https://kai-backend.fly.dev/admin/backup-db/download?create=true" \
  -H "X-API-Key: replace_with_strong_random_string" \
  -o kai_memory_backup.db
```

Import backup to a new server:

```bash
curl -X POST "https://new-server-url/admin/backup-db/import" \
  -H "X-API-Key: replace_with_strong_random_string" \
  -H "Content-Type: application/octet-stream" \
  --data-binary @kai_memory_backup.db
```

Tip: run import during low traffic or with approvals disabled.

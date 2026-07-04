# RandomBot

## Features
- Creator monetization: paid boosts, featured placement, and fast-click participation
- Security: rate limits, abuse detection, and admin protection controls
- Admin tools: statistics, user inspection, security mode, broadcast, and giveaway closing
- Mini app: web app entry point for creator dashboard

## Environment
Set the following variables in Railway or your shell environment:
- BOT_TOKEN
- ADMIN_ID
- MINI_APP_URL
- DB_PATH
- DATABASE_URL
- PAYMENTS_PROVIDER
- PAYMENTS_PROVIDER_TOKEN
- PAYMENTS_CURRENCY
- CRYPTO_BOT_URL
- WEBHOOK_URL
- WEBHOOK_PATH
- WEBHOOK_SECRET_TOKEN
- PORT

## Run locally
```bash
python main.py
```

## Run via Docker
```bash
docker build -t randombot .
docker run -p 8000:8000 -e BOT_TOKEN=... -e ADMIN_ID=... -e WEBHOOK_URL=https://example.com/webhook -e PORT=8000 randombot
```

## Railway
Railway uses the existing [railway.json](railway.json) and Dockerfile. Set the same environment variables in Railway Variables; no local .env file is required.

### Click-by-click checklist
1. Create a new Railway project.
2. Connect this GitHub repository.
3. Add a PostgreSQL plugin from Railway.
4. In Variables, add:
   - BOT_TOKEN
   - ADMIN_ID
   - MINI_APP_URL
   - PORT=8000
   - WEBHOOK_PATH=/webhook
   - WEBHOOK_SECRET_TOKEN=any-random-string
   - WEBHOOK_URL=https://<your-railway-domain>/webhook
   - DATABASE_URL=<copied from the Railway PostgreSQL plugin>
   - PAYMENTS_PROVIDER=telegram_stars
   - PAYMENTS_PROVIDER_TOKEN=
   - PAYMENTS_CURRENCY=XTR
   - CRYPTO_BOT_URL=https://t.me/CryptoBot
5. Deploy.
6. After the first start, the bot creates the tables automatically.


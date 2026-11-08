---
tags: agency/playbook
---
# 🚀 Going Live Checklist

- [ ] Business registered (LLC/EIN), business bank account, Stripe connected
- [ ] `.env`: real company name, **postal address**, email, URL
- [ ] 2–3 secondary sending domains with SPF/DKIM/DMARC, one or two inboxes each, listed in `senders.json` with a `warmup_start` date
- [ ] `GOOGLE_PLACES_API_KEY`, `NETLIFY_TOKEN`, `STRIPE_SECRET_KEY` (test key first), `SMTP_*`, `IMAP_HOST`
- [ ] Ran in `DRY_RUN=1`, read 10 generated sites and `preview-email` outputs
- [ ] Targeting US businesses (CAN-SPAM). EU/UK/Canada need consent first.
- [ ] Simple service agreement and refund policy written
- [ ] VPS + domain for the bots (e.g. `bots.yourstudio.com`), `docker compose up -d --build`
- [ ] `APP_SECRET` set (one-click unsubscribe), Stripe webhook endpoint added, `STRIPE_WEBHOOK_SECRET` set
- [ ] `ALERT_WEBHOOK_URL` set (e.g. ntfy.sh topic on your phone) and a test alert received
- [ ] Copy `data/backups/` off the server weekly (or snapshot the VPS)
- [ ] Uptime monitor pointed at `https://bots.yourstudio.com/health`
- [ ] `DRY_RUN=0`, `DAILY_EMAIL_CAP=20` for week one, then the daemon or cron

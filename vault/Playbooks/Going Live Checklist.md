---
tags: agency/playbook
---
# 🚀 Going Live Checklist

- [ ] Business registered (LLC/EIN), business bank account, Stripe connected
- [ ] `.env`: real company name, **postal address**, email, URL
- [ ] Separate sending domain, warmed up for 2–3 weeks, SPF/DKIM/DMARC set
- [ ] `GOOGLE_PLACES_API_KEY`, `NETLIFY_TOKEN`, `STRIPE_SECRET_KEY` (test key first), `SMTP_*`, `IMAP_HOST`
- [ ] Ran in `DRY_RUN=1`, read 10 generated sites and `preview-email` outputs
- [ ] Targeting US businesses (CAN-SPAM). EU/UK/Canada need consent first.
- [ ] Simple service agreement and refund policy written
- [ ] `DRY_RUN=0`, `DAILY_EMAIL_CAP=20` for week one, then the daemon or cron

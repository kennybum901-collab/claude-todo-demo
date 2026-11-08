# Website agency automation

A Python system that runs a "done-for-you websites" business for local companies.

```
find businesses with no website ─► build them a site ─► put a preview online ─► email it
        ▲                                                               │
        │      uptime monitoring ◄─ publish final site ◄─ detect Stripe payment ◄─ read replies
```

See **[BUSINESS_PLAN.md](BUSINESS_PLAN.md)** for the offer, pricing, funnel math and scaling roadmap.

## The bots

| Bot | File | Does |
|---|---|---|
| Lead bot | `agency/bots/lead_bot.py` | Google Places API search for businesses **without** a website; CSV import |
| Builder bot | `agency/bots/builder_bot.py` | Writes copy (Claude, or templates offline) and renders a responsive one-page site with LocalBusiness schema |
| Deploy bot | `agency/bots/deploy_bot.py` | Publishes each site to Netlify as a live preview URL |
| Billing bot | `agency/bots/billing_bot.py` | Creates a Stripe Payment Link per prospect ($499 setup + $49/mo), detects payments |
| Outreach bot | `agency/bots/outreach_bot.py` | Personalized email and up to 2 follow-ups, daily cap, opt-out handling, reads replies over IMAP |
| Monitor bot | `agency/bots/monitor_bot.py` | Uptime checks for paying clients |
| Orchestrator | `agency/orchestrator.py` | Runs everything in order; one failing bot never stops the rest |

## Quick start (safe: dry-run)

```bash
pip install -r requirements.txt
cp .env.example .env               # fill in your company name + address
python -m agency import-csv sample_leads.csv
python -m agency run               # builds sites, logs (doesn't send) emails
python -m agency report            # pipeline, MRR, close rate
python -m agency preview-email 1   # read exactly what lead #1 would receive
open data/sites/*/index.html       # look at the generated sites
```

`DRY_RUN=1` is the default. Nothing is emailed, deployed or charged until you set `DRY_RUN=0`.

## Going live

1. **Leads:** create a Google Cloud key with *Places API (New)* enabled and set `GOOGLE_PLACES_API_KEY` and `LEAD_QUERIES`. Places doesn't return emails: phone-only leads go to `python -m agency call-list`. To email them, add their address via CSV import.
2. **Copy:** set `ANTHROPIC_API_KEY` for AI-written site copy and email openers. Without it, built-in templates are used.
3. **Hosting:** set `NETLIFY_TOKEN` (Netlify → User settings → Applications → Personal access token).
4. **Payments:** set `STRIPE_SECRET_KEY` (start with `sk_test_...`).
5. **Email:** use a *separate, warmed-up sending domain*. Set `SMTP_*` and `IMAP_HOST`, and keep `DAILY_EMAIL_CAP` at about 30 per inbox.
6. Set `DRY_RUN=0`, then run on a schedule:

```bash
python -m agency daemon --every 60          # or cron: 0 * * * * cd /app && python -m agency run
```

The outreach bot refuses to email anyone until that lead has a real `https://` preview and a real Stripe link.

## Other commands

```bash
python -m agency mark-paid 12              # offline payment (cash / check) → site goes live next run
python -m agency unsubscribe a@b.com       # do-not-contact, honored forever
```

When a prospect replies with anything other than an opt-out, their status becomes `replied`. **That's your cue to answer them personally.** That's where deals close.

## Tests

```bash
python -m pytest -q
```

Covers the full dry-run pipeline, deduplication, follow-up timing, the daily cap, opt-outs, payment → client onboarding, Stripe and Netlify request shapes (with a fake HTTP session), and the live-send safety guard.

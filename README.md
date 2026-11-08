# Website agency automation

A Python system that runs a "done-for-you websites" business for local companies.

```
find businesses with no website ─► build them a site ─► put a preview online ─► email it
        ▲                                                               │
        │      uptime monitoring ◄─ publish final site ◄─ detect Stripe payment ◄─ read replies
```

See **[vault/Business Plan.md](vault/Business%20Plan.md)** for the offer, pricing, funnel math and scaling roadmap.

**Run the business from Obsidian:** every pipeline run mirrors your CRM into an Obsidian vault (see [Obsidian](#obsidian)).

## The bots

| Bot | File | Does |
|---|---|---|
| Lead bot | `agency/bots/lead_bot.py` | Google Places API search for businesses **without** a website; CSV import |
| Builder bot | `agency/bots/builder_bot.py` | Writes copy (Claude, or templates offline) and renders a responsive one-page site with LocalBusiness schema |
| Deploy bot | `agency/bots/deploy_bot.py` | Publishes each site to Netlify as a live preview URL; takes previews down for opt-outs and dead leads |
| Billing bot | `agency/bots/billing_bot.py` | Creates a Stripe Payment Link per prospect ($499 setup + $49/mo), detects payments, tracks subscriptions (churn, failing cards) |
| Outreach bot | `agency/bots/outreach_bot.py` | Personalized email and up to 2 follow-ups, subject-line A/B test, daily cap, opt-out handling, reads replies over IMAP and **drafts an answer** for you |
| Upsell bot | `agency/bots/upsell_bot.py` | Offers clients niche-matched add-ons (Review Booster $99, Online Booking $79, Local SEO $199/mo) on a schedule, each with its own Stripe link; tracks activations and cancellations |
| Report bot | `agency/bots/report_bot.py` | Monthly email to each client: uptime, outages handled, work done, their plan, one suggested add-on; copy saved to Obsidian |
| Monitor bot | `agency/bots/monitor_bot.py` | Uptime checks for paying clients |
| Obsidian sync | `agency/obsidian.py` | Writes one CRM note per lead, a live Dashboard and a daily log into your vault; reads `do_not_contact` flags back |
| Mailer | `agency/mailer.py` | Rotates sends across multiple inboxes with per-inbox caps, warm-up ramps, sticky threads and auto-pause on bounces |
| Server | `agency/server.py` | `agency serve`: bots on a schedule, Stripe webhooks, one-click unsubscribe, `/health` |
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

## Deploying to a server

A $5–10/month VPS (Hetzner, DigitalOcean, Lightsail…) is enough. `python -m agency serve` runs everything in one process:

- the full bot pipeline every `RUN_EVERY_MINUTES` (default 60)
- `POST /stripe/webhook` for instant payments, add-on purchases, cancellations and failed cards (signature-verified with `STRIPE_WEBHOOK_SECRET`)
- `GET/POST /unsubscribe` for signed one-click unsubscribe links (RFC 8058, which Gmail and Yahoo require for bulk senders)
- `GET /health` for liveness only, with no customer data; point an uptime monitor at it

**With Docker (recommended):**

```bash
# on the server, with DNS for bots.yourstudio.com pointing at it
git clone <this repo> agency && cd agency
cp .env.example .env              # fill in, set APP_SECRET (random) and DRY_RUN=1 at first
cp senders.example.json senders.json
DOMAIN=bots.yourstudio.com docker compose up -d --build
```

Caddy gets the HTTPS certificate automatically. Data lives in `./data` (back it up) and the Obsidian vault in `./vault`; sync it to your laptop with Obsidian Sync, Syncthing or similar.

**Stripe webhook:** Dashboard → Developers → Webhooks → *Add endpoint* `https://bots.yourstudio.com/stripe/webhook` with events `checkout.session.completed`, `customer.subscription.deleted` and `invoice.payment_failed`. Copy the signing secret into `STRIPE_WEBHOOK_SECRET`. Hourly polling stays on as a backup, and both paths are idempotent.

**Without Docker:** `deploy/agency.service` is a systemd unit. Put Caddy or nginx in front for HTTPS.

## Obsidian

Open `vault/` in Obsidian (**Open folder as vault**), or set `OBSIDIAN_VAULT=/path/to/your/vault` to use your existing vault. Each run writes:

| Note | Contents |
|---|---|
| `Dashboard.md` | MRR, clients, close rate, **replies waiting for you**, client sites down, pipeline, a Dataview table |
| `Leads/<Business> (id).md` | Properties (status, phone, email, preview & payment links, follow-ups) plus the full bot activity timeline |
| `Daily/<date>.md` | Everything the bots did today, linked to lead notes |

The vault also ships with `Home`, the `Business Plan` and playbooks (daily routine, closing replies, go-live checklist, choosing niches).

- Anything you write under `## My notes` in a lead note is never overwritten.
- Set `do_not_contact: true` in a lead's properties and the next run suppresses that email **before** outreach.
- Generated notes contain prospect contact details, so `Leads/`, `Daily/` and `Dashboard.md` are git-ignored.
- `python -m agency obsidian-sync` refreshes the vault without running the bots. Set `OBSIDIAN_SYNC=0` to turn it off.

## Other commands

```bash
python -m agency mark-paid 12              # offline payment (cash / check) → site goes live next run
python -m agency unsubscribe a@b.com       # do-not-contact, honored forever
```

When a prospect replies with anything other than an opt-out, their status becomes `replied` and the bot writes a **suggested answer** into their Obsidian note (Claude if `ANTHROPIC_API_KEY` is set, templates otherwise). Drafts are never sent automatically. Review, edit and send it yourself; that's where deals close.

## Scaling email safely (multiple inboxes)

One inbox should send about 30 cold emails a day at most. To grow, add inboxes, ideally on 2–3 secondary domains (e.g. `getnorthstar.com`, `trynorthstar.com`) with SPF, DKIM and DMARC set up. Never send from your main domain.

```bash
cp senders.example.json senders.json   # git-ignored; passwords come from env vars via password_env
```

- **Per-inbox caps and warm-up:** a new inbox starts at `warmup_base` sends a day (default 5) and adds `warmup_step` a day (default 2) until it reaches `daily_cap`.
- **Rotation:** new leads go to the inbox with the most capacity left today.
- **Sticky threads:** follow-ups always come from the inbox that sent the first email, so replies stay in one thread.
- **Bounce handling:** bounce notices are detected in every inbox. The address is suppressed and the lead marked `bounced`.
- **Auto-pause:** an inbox whose bounce rate over its last 50 sends passes `BOUNCE_PAUSE_RATE` (5%) stops sending. Its leads move to healthy inboxes.
- **Monitoring:** `report` and the Obsidian Dashboard show each inbox's sends, cap, bounce rate and health.

Without `senders.json`, the single `SMTP_*` settings and `DAILY_EMAIL_CAP` are used exactly as before.

## Growth & retention features

- **Reply drafts:** each reply shows up on the Dashboard with a ready-to-edit answer in the lead note.
- **Subject-line A/B test:** each lead gets one of `SUBJECT_VARIANTS`; `report` and the Dashboard show reply and win rate per variant. Wait for about 100 sends per variant before picking a winner.
- **Churn tracking:** cancelled Stripe subscriptions become `churned` and drop out of MRR. Failing cards (`past_due`) appear under *Payment problems* so you can call before they cancel.
- **Preview cleanup:** Netlify previews for `unsubscribed` and `lost` leads are deleted automatically.
- **Upsells:** 60 days after a client's first payment (`UPSELL_AFTER_DAYS`), they get one add-on offer at a time, at most every 30 days (`UPSELL_GAP_DAYS`). Salons get booking first and trades get reviews first. Each add-on is a separate subscription the client explicitly buys. Add-on MRR shows in `report` and on the Dashboard. Edit `CATALOG` in `agency/bots/upsell_bot.py` to change offers and prices. Activate a deal closed by phone with `python -m agency addon-activate <lead_id> <addon>`. A client who replies STOP stops getting offers but stays a client.
- **Monthly client reports:** on `REPORT_DAY` (default the 1st), every client gets a report on the previous month covering uptime %, outages handled, average load time, work done, their plan and add-ons, and one suggested next add-on. Each client gets one per month, only after a full month as a client, and reports go out before cold outreach so clients get inbox capacity first. Copies land in `vault/Reports/`. Preview one with `python -m agency preview-report <lead_id>`.
- **Automatic DB migration:** existing databases get new columns on startup.

## Tests

```bash
python -m pytest -q
```

Covers the full dry-run pipeline, Obsidian sync (notes preserved, `do_not_contact` honored), deduplication, follow-up timing, the daily cap, opt-outs, payment → client onboarding, Stripe and Netlify request shapes (with a fake HTTP session), and the live-send safety guard.

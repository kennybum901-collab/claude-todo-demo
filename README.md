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
| Builder bot | `agency/bots/builder_bot.py` | Writes copy (Claude, or templates offline) and renders a responsive one-page site with LocalBusiness schema in one of 4 designs matched to the niche |
| Deploy bot | `agency/bots/deploy_bot.py` | Publishes each site to Netlify as a live preview URL; takes previews down for opt-outs and dead leads |
| Billing bot | `agency/bots/billing_bot.py` | Creates a Stripe Payment Link per prospect ($499 setup + $49/mo), detects payments, tracks subscriptions (churn, failing cards) |
| Outreach bot | `agency/bots/outreach_bot.py` | Personalized email and up to 2 follow-ups, subject-line A/B test, daily cap, opt-out handling, reads replies over IMAP and **drafts an answer** for you |
| Upsell bot | `agency/bots/upsell_bot.py` | Offers clients niche-matched add-ons (Review Booster $99, Online Booking $79, Local SEO $199/mo) on a schedule, each with its own Stripe link; tracks activations and cancellations |
| Report bot | `agency/bots/report_bot.py` | Monthly email to each client: uptime, outages handled, work done, their plan, one suggested add-on; copy saved to Obsidian |
| Monitor bot | `agency/bots/monitor_bot.py` | Uptime checks for paying clients |
| Obsidian sync | `agency/obsidian.py` | Writes one CRM note per lead, a live Dashboard and a daily log into your vault; reads `do_not_contact` flags back |
| Mailer | `agency/mailer.py` | Rotates sends across multiple inboxes with per-inbox caps, warm-up ramps, sticky threads and auto-pause on bounces |
| Server | `agency/server.py` | `agency serve`: bots on a schedule, Stripe webhooks, one-click unsubscribe, `/health` |
| Quote form | `agency/quotes.py` | "Get a free quote" form on client sites; requests are emailed to the owner instantly (Reply-To = customer) and counted in reports |
| Supervisor | `agency/supervisor.py` | `agency supervise`: keeps everything running 24/7 with auto-restart on crash or hang, phone/email alerts, daily DB backups, rotating logs, disk check |
| Edit bot | `agency/edits.py` | Turns client emails ("new hours, new phone") into drafted site changes; you approve in Obsidian; it rebuilds, redeploys and confirms to the client |
| Win-back bot | `agency/bots/winback_bot.py` | Second chance with the setup fee waived: 2 emails to cancelled clients (day 30 and 60), 1 final email to silent prospects (day 90); a purchase relaunches the site |
| Analytics | `agency/analytics.py`, `agency/dashboard.py` | MRR history (base + add-ons), funnel, results by niche and city, revenue per client, churn, lifetime value, plus recommendations: an interactive page at `/admin` and `vault/Performance.md` |
| Lead scoring | `agency/scoring.py` | 0–100 likelihood-to-buy score from your own close rates by niche and city (smoothed) plus rating and review count; orders site building, outreach and the call list |
| Daily review | `agency/digest.py`, `agency/qa.py` | Morning email to you: yesterday's numbers, sample emails word for word, newest sites attached, automatic quality checks, one-click pause |
| Referrals | `agency/referrals.py` | Personal referral link per client; a sign-up page with consent for referred businesses ($100 off setup, priority outreach); the referrer gets a free month via Stripe coupon |
| Review Booster | `agency/reviews.py` | Delivers the $99/mo add-on: a private "job done" phone page per client, review-request emails to their customers sent in the business's name, 1 reminder, click tracking, optional private feedback (never review-gated) |
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

## Daily review & kill switch

Once a day after `DIGEST_HOUR` (UTC) you get one email at `DIGEST_EMAIL` (falls back to `ALERT_EMAIL`, then `COMPANY_EMAIL`):

- **Needs a look:** anything the automatic quality checks flagged, plus bot errors. The subject starts with ⚠️ when there's something.
- **Last 24 hours:** leads, sites, emails, replies, opt-outs and bounces, new clients, add-ons, quote requests, cancellations, errors.
- **Sample of emails sent:** up to `DIGEST_SAMPLE` real emails, word for word, at least one of each kind (outreach, offer, report…), each with its check result.
- **Newest sites:** attached as HTML files you can open on your phone.

**It is sent even in `DRY_RUN`**, so you can review exactly what the bots *would* send before going live. Only this email to you goes out.

**Quality checks** (`python -m agency qa` runs them on demand):
- Sites: business name present, no leftover template text, tap-to-call phone link, title and phone viewport, page size, live URL returns 200.
- Emails: subject length and capitals, `!!`, leftover template text, CAN-SPAM postal address and opt-out, spam-trigger phrases, no `file://` links when live.

**Kill switch:** the digest has a one-click **pause** link (signed, and it asks for confirmation first). `python -m agency pause` / `resume` do the same. While paused, outreach, offers, win-back and reports stop. Quote forwarding, edit confirmations and alerts keep working. Every email the bots send is archived for 90 days in the `messages` table.

## Running 24/7

`python -m agency supervise` is the always-on mode (Docker and systemd use it by default):

| Problem | What the supervisor does |
|---|---|
| Server crashes | Restarts it within seconds; backoff grows 1s → 5 min if it keeps crashing and resets once stable |
| Server hangs | `/health` fails 3 checks in a row → kill and restart |
| Bots stuck mid-run | No pipeline heartbeat for 3 × `RUN_EVERY_MINUTES` → restart |
| Data loss risk | Online SQLite backup every day to `data/backups/`, keeping `BACKUP_KEEP_DAYS` |
| Disk filling up | Alert below `MIN_FREE_DISK_MB` |
| You're asleep | Alerts on crash, hang and recovery via `ALERT_WEBHOOK_URL` (Slack, Discord, or `https://ntfy.sh/<secret-topic>` for free phone push) and/or `ALERT_EMAIL`; repeat alerts are throttled |
| Machine reboots | Docker `restart: unless-stopped` / systemd `Restart=always` start the supervisor again |

Logs rotate in `data/logs/agency.log`. `python -m agency backup` makes a backup on demand.

## Deploying to a server

A $5–10/month VPS (Hetzner, DigitalOcean, Lightsail…) is enough. `python -m agency serve` runs everything in one process (and `supervise` keeps it alive):

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
python -m agency top-leads                 # best leads waiting, with the reason for each score
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
- **Quote form on client sites:** once `PUBLIC_URL` and `APP_SECRET` are set, every paying client's site gets a *Get a free quote* form (never on previews). Submissions go to `agency serve`, are stored, and are emailed to the owner right away with Reply-To set to the customer. These notices skip the cold-email cap but never use a paused inbox. Spam protection: signed per-site URL, honeypot field, length limits, and 5 requests per visitor per hour. The count appears in the monthly report ("3 quote requests came in through your website"), in add-on offers and in Obsidian. Phone-only clients' requests are flagged on the Dashboard so you can forward them. After first setting `PUBLIC_URL`, run `python -m agency rebuild-clients` to add the form to existing sites.
- **Client self-service edits:** any client email is treated as a possible change request. Claude (with a strict JSON schema) drafts the complete updated site text and only changes what was asked. Allowed fields: headline, tagline, about, services, button text, **hours**, phone and address. You get an `Edits/` note in Obsidian with a before/after table: set `approve: true` (or `reject: true`), or use `python -m agency edits`, `edit-approve <id>` or `edit-reject <id>`. On the next run the site is rebuilt and redeployed (only that site) and the client gets a "your update is live" email. Nothing is published without approval. Photos, logos, design changes, AI failures and setups without an API key are routed to you as `needs_human`.
- **Win-back:** cancelled clients get two emails (`WINBACK_CHURN_DAYS`, default 30, then 30 days later) offering to relaunch their saved site with no setup fee, mentioning the quote requests it brought in. Prospects who never replied get exactly one final email after `WINBACK_LOST_DAYS` (90), then never again. Each offer is a monthly-only Stripe link. Paying (by webhook or polling) relaunches the site through normal onboarding, and a late cancellation notice for a won-back client's *old* subscription is ignored. Suppressed, bounced and unsubscribed contacts are never emailed. Results appear in `report` and on the Dashboard.
- **Performance dashboard:** set `ADMIN_PASSWORD` and open `https://<your server>/admin` (user `admin`), or run `python -m agency dashboard` for `data/dashboard.html`. It shows MRR and its 30-day change, clients, revenue per client, churn, lifetime value (only once churn is measurable), a 12-month MRR chart split into base and add-ons (reconstructed from payments, cancellations and win-backs), the sales funnel, and close rate and MRR per niche and city. "What to do next" recommendations wait until a segment has 20+ emailed leads. The page loads nothing from the internet (inline SVG), works in light and dark mode, has hover tooltips and a table view, and works on phones. The same numbers are written to `vault/Performance.md` on every run.
- **Lead scoring:** every run, each open lead gets a 0–100 score: your close rate for its niche × its city's lift × a reputation factor (rating and review count). Rates are smoothed with 20 pseudo-leads, so one lucky sale doesn't skew things, and they start from a 3% default before you have data. Sites are built and first emails sent **highest score first**. Follow-ups always go before new first emails. `MIN_LEAD_SCORE` stops weak leads from ever being cold-emailed; they stay on the call list instead. See `python -m agency top-leads`. The score and its reason show in each Obsidian lead note, and the Dashboard lists the best leads not yet emailed.
- **Referral program:** each client's monthly report includes their link (`https://<server>/r/<code>`, unguessable, active only while they're a client; list them with `python -m agency referral-links`). A referred business fills in a short form with a **consent checkbox**, so it's an opted-in lead. It's emailed first ("Rivera Plumbing recommended us"), skips `MIN_LEAD_SCORE`, and gets `REFERRAL_DISCOUNT_CENTS` ($100) off setup on its Stripe link. When it pays, the referrer gets a one-time coupon worth one month on their subscription plus a thank-you email (once per referral). The form never undoes an opt-out and is rate-limited per visitor. Stats appear on the Dashboard; the performance page has a by-source table.
- **Four site designs** (`agency/templates/site/`), picked from the niche:
  - **bold**: trades (roofers, plumbers, electricians, HVAC…). Dark hero, giant tap-to-call number, trust strip.
  - **elegant**: salons, spas, barbers, nails, beauty. Serif type, cream background, menu-style services.
  - **fresh**: lawn care, landscaping, cleaning, pools, pets. Bright color block, wave divider, checklist services.
  - **classic**: everything else.

  The design is chosen once and saved with the site, so rebuilds never change it. `SITE_THEME` forces one design for every *new* site. All brand colors keep white text at ≥4.5:1 contrast. Only real facts are shown (actual rating and review count, no invented badges). The performance page has a **by design** table so you can see which converts best. Button text also fits the business now ("Book your appointment", "Call for a free estimate", "Call to order").
- **Review Booster (automated):** when a client buys the add-on, they get a private link (`/jobs/<id>/<token>`, list them with `python -m agency jobs-links`) to bookmark on their phone. After a job they type the customer's email or tap a recent quote request. The customer then gets a short email **from the business's name** (replies go to the business) with the review link, plus one reminder after `REVIEW_REMINDER_DAYS` (4) if they haven't clicked. Clicks are tracked through a redirect and counted in the monthly report. The Google review link is filled in automatically for leads found through Google Places; for others use `python -m agency set-review-url <id> <url>`. **Every customer always gets the public review link.** The optional private-feedback form sits alongside it and is never a filter, because Google bans "review gating" and the FTC bans suppressing negative reviews. One request per customer per business; opt-outs respected.
- **Automatic DB migration:** existing databases get new columns on startup.

## Tests

```bash
python -m pytest -q
```

Covers the full dry-run pipeline, Obsidian sync (notes preserved, `do_not_contact` honored), deduplication, follow-up timing, the daily cap, opt-outs, payment → client onboarding, Stripe and Netlify request shapes (with a fake HTTP session), and the live-send safety guard.

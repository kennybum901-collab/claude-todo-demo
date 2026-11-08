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
| Monitor bot | `agency/bots/monitor_bot.py` | Uptime checks for paying clients |
| Obsidian sync | `agency/obsidian.py` | Writes one CRM note per lead, a live Dashboard and a daily log into your vault; reads `do_not_contact` flags back |
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

## Growth & retention features

- **Reply drafts:** each reply shows up on the Dashboard with a ready-to-edit answer in the lead note.
- **Subject-line A/B test:** each lead gets one of `SUBJECT_VARIANTS`; `report` and the Dashboard show reply and win rate per variant. Wait for about 100 sends per variant before picking a winner.
- **Churn tracking:** cancelled Stripe subscriptions become `churned` and drop out of MRR. Failing cards (`past_due`) appear under *Payment problems* so you can call before they cancel.
- **Preview cleanup:** Netlify previews for `unsubscribed` and `lost` leads are deleted automatically.
- **Upsells:** 60 days after a client's first payment (`UPSELL_AFTER_DAYS`), they get one add-on offer at a time, at most every 30 days (`UPSELL_GAP_DAYS`). Salons get booking first and trades get reviews first. Each add-on is a separate subscription the client explicitly buys. Add-on MRR shows in `report` and on the Dashboard. Edit `CATALOG` in `agency/bots/upsell_bot.py` to change offers and prices. Activate a deal closed by phone with `python -m agency addon-activate <lead_id> <addon>`. A client who replies STOP stops getting offers but stays a client.
- **Automatic DB migration:** existing databases get new columns on startup.

## Tests

```bash
python -m pytest -q
```

Covers the full dry-run pipeline, Obsidian sync (notes preserved, `do_not_contact` honored), deduplication, follow-up timing, the daily cap, opt-outs, payment → client onboarding, Stripe and Netlify request shapes (with a fake HTTP session), and the live-send safety guard.

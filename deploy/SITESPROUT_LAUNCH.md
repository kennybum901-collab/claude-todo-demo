# SiteSprout launch plan

Business: **SiteSprout**, websites for roofers, plumbers, hair salons/barbers and lawn care companies in **Austin, TX**. $499 setup + $49/month.

Everything below needs *your* accounts (they hold your money, your domain and your email reputation), so these are steps for you. Each one says what it costs and what to copy where. Total to start: roughly **$45 + ~$7/month server + ~$7/month email**.

> Name note: SiteSprout and the domains below were checked for *domain availability* only, not trademarks. Do a quick search (USPTO TESS + Texas SOS business search) before printing anything.

---

## 1. Domains (~$36)
Buy at any registrar (Cloudflare Registrar, Namecheap, Porkbun…):

| Domain | Use | Status when checked |
|---|---|---|
| `sitesproutaustin.com` | your main website + the bots server (`bots.sitesproutaustin.com`) | available, $17 first year |
| `sitesproutaustin.net` | **cold email only**, so spam complaints can never hurt your main domain | available, $19 first year |

Availability was checked on 2026-11-08. Register promptly; a check doesn't reserve anything.

## 2. Sending email (~$7/month)
1. Google Workspace (or Microsoft 365) on **sitesproutaustin.net**, one mailbox, e.g. `alex@sitesproutaustin.net`.
2. In the admin console, turn on **SPF, DKIM and DMARC** (the setup wizard walks you through the DNS records).
3. Turn on 2-Step Verification, then create an **App Password**. That's what goes into setup.
4. **Warm it up for 2–3 weeks before cold email** (send a few normal emails a day). Use this time for steps 3–8.

## 3. Business basics
- Register the business (an LLC is common in Texas, filed through SOSDirect), get an EIN (free, irs.gov), open a business bank account.
- You need a **postal address** for email footers (required by CAN-SPAM). A PO box or virtual mailbox is fine.

## 4. Stripe (free, ~3% per payment)
stripe.com → create account → connect your business bank. Copy **Developers → API keys → Secret key** (`sk_test_…` first).

## 5. Netlify (free tier)
netlify.com → sign up → **User settings → Applications → Personal access token**.

## 6. Claude API key (optional, recommended)
Claude Console → API keys. Better site copy, reply drafts, automatic site edits. Costs cents per site.

## 7. Server (~$7/month)
1. Rent an **Ubuntu 24.04** VPS: Hetzner CX22, DigitalOcean Basic, or similar (2 GB RAM is plenty).
2. At your registrar, add a DNS **A record**: `bots.sitesproutaustin.com` → the server's IP address.
3. On the server:
   ```bash
   ssh root@<server-ip>
   apt update && apt install -y docker.io docker-compose-v2 git python3-venv
   git clone https://github.com/kennybum901-collab/claude-todo-demo.git agency   # private repo: use a GitHub token or deploy key
   cd agency && git checkout ccr-12b41a46-pu54ia
   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
   cp sitesprout.env .env
   .venv/bin/python -m agency setup        # answer the questions, paste keys when asked
   .venv/bin/python -m agency doctor       # fix anything marked ❌, repeat
   DOMAIN=bots.sitesproutaustin.com docker compose up -d --build
   ```
4. In Stripe: **Developers → Webhooks → Add endpoint** `https://bots.sitesproutaustin.com/stripe/webhook` with events `checkout.session.completed`, `customer.subscription.deleted`, `invoice.payment_failed`. Paste the signing secret: re-run `setup` (or edit `.env`), then `docker compose up -d`.
5. Phone alerts: install the **ntfy** app, subscribe to a long random topic, and set `ALERT_WEBHOOK_URL=https://ntfy.sh/<that-topic>`.

## 7b. Your own website (sitesproutaustin.com)
Prospects will look you up. Generate SiteSprout's site from your settings and publish it:
```bash
docker compose exec agency python -m agency company-site            # writes data/company-site/
# with DRY_RUN=0 and NETLIFY_TOKEN set:  ... company-site --deploy
# or drag data/company-site/ onto app.netlify.com/drop
```
Then in Netlify go to **Domain settings → Add domain** `sitesproutaustin.com` and follow its DNS instructions. The site has pricing, the four example designs, an FAQ and a **"Get my free preview"** form. Requests go to the front of the outreach queue and you get an email for each one. Re-run the command after changing prices or niches.

## 8. Dry run: one week, nothing is sent
1. Google Places key in setup (or import a list: `docker compose exec agency python -m agency import-csv leads.csv`).
2. Let it run. Every morning read the **daily review email**: sample sites (open the attachments) and the exact emails it would send.
3. Open `https://bots.sitesproutaustin.com/admin` (user `admin`, password from setup) to watch the funnel.

## 9. Go live
- `doctor` says **✅ Nothing blocks going live**, the inbox has warmed up, and a week of daily reviews looked right.
- Switch Stripe to the `sk_live_…` key, set `DRY_RUN=0`, `DAILY_EMAIL_CAP=20` for week one, then `docker compose up -d`.
- Every daily review email has a **pause** link if anything looks wrong.

Answer every reply personally the same day. That's where the deals close (`vault/Playbooks/Closing a Reply.md`).

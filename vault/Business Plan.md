---
tags: agency/plan
---
# Northstar Web Studio: Business Plan

> Rename the company with `COMPANY_NAME` in `.env`. The plan is the same whatever you call it. Related: [[Home]] · [[Dashboard]] · [[Playbooks/Daily Routine]]

## 1. The offer

Many local service businesses (plumbers, roofers, salons, landscapers, cleaners) have a Google listing with good reviews but **no website**. We build their site *before* we contact them, then send it: *"I built this for you. Want to keep it?"* Showing a finished site beats asking whether they want one.

| Product | Price | Why |
|---|---|---|
| Website setup | **$499 one-time** | Custom domain, branding tweaks, Google Business link |
| Hosting & care plan | **$49/month** | Hosting, SSL, uptime monitoring, small edits each month |
| Upsells (later) | $99–$300/mo | Local SEO, review-request automation, booking widget, ads management |

The monthly plan is the business. One-time setup fees pay for acquiring the customer. Recurring revenue is what gives the company its value.

## 2. Unit economics (per client)

| Item | Estimate |
|---|---|
| Hosting (Netlify free/pro tier, shared) | ~$0–2/mo |
| AI copywriting per site (Claude) | cents |
| Domain (resold at cost or included) | ~$12/yr |
| **Gross margin on the $49 plan** | **~95%** |
| Lifetime at ~24-month average retention | $499 + 24 × $49 = **~$1,675** |

## 3. Funnel math (assumptions, not promises; measure your own)

Cold-email reply rates for "here's a site I already built" usually land in the low single-digit percent. The table below is a planning model. Replace it with your real numbers from `python -m agency report` after the first few weeks.

| Stage | Per month | Assumed rate |
|---|---|---|
| Leads found (no website) | 1,000 | — |
| Have a reachable email | 400 | 40% |
| Emails delivered | 400 | (cap ~30/day per inbox) |
| Positive replies | 12 | 3% |
| Close | 5 | ~40% of replies |
| **New MRR** | **+$245/mo** + $2,495 setup | |

To grow, increase the number of inboxes and niches, add phone outreach for phone-only leads (`call-list`), and raise the price per client with upsells. Don't grow by sending more email from a single inbox. That burns your domain.

## 4. Scaling roadmap

**Phase 1: Validate (months 0–2, solo).**
Pick one niche in 1–2 cities. Run in dry-run mode, read every email and site before going live, then send 20–30 emails a day. Reply to every response personally. Target: 10 paying clients.

**Phase 2: Systematize (months 2–6).**
- Two or three sending domains, each warmed up, each capped around 30/day.
- Expand `LEAD_QUERIES` to 5–10 niches × 5–10 cities.
- Hire a part-time VA to call phone-only leads from `python -m agency call-list` and handle edit requests.
- Add upsell packages. Target: 50 clients, about $2.5k MRR plus setup fees.

**Phase 3: Scale (months 6–18).**
- Move from SQLite to Postgres, run the daemon on a small VPS or cron, and use a Stripe webhook instead of polling.
- Use niche-specific templates with booking and quote forms (higher conversion, higher price).
- Add partner channels: accountants, chambers of commerce, sign shops (referral fee per client).
- Target: 200+ clients, $10k+ MRR, where the business becomes sellable (agencies with recurring revenue often sell for several times annual profit).

## 5. What is automated vs. what stays human

| Automated by the bots | Keep a human on it |
|---|---|
| Finding businesses without sites | Choosing niches and cities |
| Writing copy and building the site | Spot-checking sites before going live |
| Deploying previews | Replying to interested prospects and closing |
| Personalized email and follow-ups | Custom edit requests |
| Opt-out handling and send caps | Handling complaints and refunds |
| Payment links and payment detection | Pricing changes |
| Final publish and uptime monitoring | Taxes, contracts, business registration |

No bot reliably closes sales on its own. Treat the bots as a lead and production machine that hands warm, replied prospects to you.

## 6. Legal & risk checklist

- Register the business (an LLC is common in the US), get an EIN, and open a business bank account. Connect Stripe to that account.
- **Cold email laws.** In the US, CAN-SPAM requires a truthful subject, a physical address and an opt-out you honor. The bots do all three. The **EU/UK (GDPR/PECR)** and **Canada (CASL)** are much stricter, so target US businesses or get consent first.
- Use the **official Google Places API** (built in). Don't scrape Google Maps. Follow the Places API terms on data storage and attribution.
- Preview sites are marked noindex and carry a "free preview" banner. Take unclaimed previews down after about 30 days.
- Write a simple service agreement and terms (cancellation, what "small edits" includes, refund policy).
- Use a separate sending domain so your main domain's reputation is protected.

---
tags: agency/playbook
---
# 📈 Delivering Add-ons

The upsell bot sells these; you (or a VA) deliver them. Each add-on is offered once, matched to the client's niche, from day 60.

| Add-on | Price | How to deliver (low effort) |
|---|---|---|
| **Review Booster** | $99/mo | **Automated.** The client gets a private "job done" link and taps it after each job; the bot emails their customer the review link (+1 reminder). Check their Google review link is set: `python -m agency set-review-url <id> <url>`. Never filter out unhappy customers (Google policy / FTC). |
| **Online Booking** | $79/mo | Embed a booking widget (Calendly, Square Appointments, Fresha…) on their site; rebuild and deploy |
| **Local SEO** | $199/mo | Optimize their Google Business Profile, add one local landing page a month, send a short monthly ranking note |

When someone buys, their lead note shows ✅ under **Add-ons** and the Dashboard's add-on MRR goes up. Closed a deal by phone? Run `python -m agency addon-activate <lead_id> <addon>`.

Back to [[Home]] · [[Dashboard]]

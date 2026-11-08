---
tags: agency/playbook
---
# 🤝 Closing a Reply

A reply means they looked at the site. Answer within a few hours.

**"How much?" / "What's the catch?"**
> No catch. It's $499 to put it on your own domain with any changes you want, then $49/month for hosting, security and small edits. Cancel anytime. Here's the link when you're ready: *(payment_url from their lead note)*

**"Can you change X?"**
> Absolutely. Send me the changes (photos, logo, hours) and I'll update the preview today so you can see it before paying.

**"I already have a guy / I'm on Facebook."**
> Totally fair. Most of your customers search Google first, and this gives them somewhere to land. Happy to keep the preview up for a week if you want to think it over.

**"Not interested."**
> Thanks for letting me know. I won't reach out again. *(Set `do_not_contact: true` in their note.)*

After they pay, the bots detect it automatically. For cash or check, run `python -m agency mark-paid <lead_id>`.

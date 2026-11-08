---
tags: agency/playbook
---
# ☀️ Daily Routine (≈30 min)

1. Open [[Dashboard]] and check **🔥 Replies waiting for you**. Each lead note has a suggested answer; edit it and send it personally ([[Playbooks/Closing a Reply]]).
2. Check **✏️ Site edits waiting for your approval**. Open each note, check the before/after table, set `approve: true`. ⚠️ ones need a manual touch.
   Then check **💳 Payment problems**. Call those clients today; a failing card is the #1 cause of churn.
3. Check **🚨 Client sites down** and **📝 Quote requests** (forward any marked ⚠️ by hand). Paying clients come first.
4. Skim today's note in `Daily/` for bot errors.
5. Spot-check two or three new preview sites (open the `preview_url` in their lead notes).
6. Call phone-only leads, best score first: `python -m agency call-list`. Log the outcome under `## My notes`.
7. Weekly: open [[Performance]] and follow *What to do next*; review churn and the **A/B test** in [[Dashboard]] and adjust niches or cities in `.env` (`LEAD_QUERIES`).

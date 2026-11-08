---
tags: agency/home
---
# 🏠 Agency HQ

Open this folder as a vault in Obsidian (**Open folder as vault → `vault/`**), or set `OBSIDIAN_VAULT=/path/to/your/vault` in `.env` so the bots write into your existing vault.

## Start here
- [[Dashboard]]: MRR, pipeline, replies waiting for you, sites down (updated every bot run)
- [[Business Plan]]: offer, pricing, funnel math, scaling roadmap
- `Leads/`: one CRM note per business (properties + full bot activity timeline)
- `Daily/`: what the bots did each day
- `Reports/`: a copy of every monthly client report

## Playbooks
- [[Playbooks/Daily Routine]]
- [[Playbooks/Closing a Reply]]
- [[Playbooks/Going Live Checklist]]
- [[Playbooks/Choosing Niches]]
- [[Playbooks/Delivering Add-ons]]

## How the bots and Obsidian work together
| You do in Obsidian | What happens |
|---|---|
| Write anything under `## My notes` in a lead | Kept forever; bots never touch it |
| Set `do_not_contact: true` in a lead's properties | Next run suppresses that email before any outreach |
| Open [[Dashboard]] | See who replied and needs a personal answer |

Recommended community plugin: **Dataview** (powers the live table on the dashboard).

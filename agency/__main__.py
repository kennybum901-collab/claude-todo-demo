"""Command-line interface:  python -m agency <command>"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys

from .bots.billing_bot import BillingBot
from .bots.lead_bot import LeadBot
from .bots.outreach_bot import OutreachBot
from .bots.report_bot import ReportBot, previous_period
from .bots.upsell_bot import CATALOG, UpsellBot
from .config import config
from .db import DB
from .mailer import Mailer
from .obsidian import ObsidianSync
from .orchestrator import Orchestrator


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="agency", description="Automated website agency")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="run the full pipeline once")
    d = sub.add_parser("daemon", help="run the pipeline forever on a schedule")
    d.add_argument("--every", type=int, default=60, help="minutes between runs")
    i = sub.add_parser("import-csv", help="import leads from a CSV file")
    i.add_argument("path")
    sv = sub.add_parser("serve", help="production server: bots on a schedule + Stripe webhooks + unsubscribe")
    sv.add_argument("--no-bots", action="store_true", help="only serve HTTP, don't run the pipeline")
    sub.add_parser("supervise", help="24/7 mode: run `serve`, auto-restart on crash/hang, alerts, daily backups")
    sub.add_parser("backup", help="back up the database now")
    sub.add_parser("report", help="show pipeline + revenue metrics")
    sub.add_parser("call-list", help="print phone-only leads to call (CSV)")
    pr = sub.add_parser("preview-email", help="print the email a lead would receive")
    pr.add_argument("lead_id", type=int)
    mp = sub.add_parser("mark-paid", help="record an offline payment (cash/check/Zelle)")
    mp.add_argument("lead_id", type=int)
    mp.add_argument("--amount", type=int, default=None, help="amount in cents")
    ad = sub.add_parser("addon-activate", help="manually activate an add-on for a client")
    ad.add_argument("lead_id", type=int)
    ad.add_argument("addon", choices=[a.key for a in CATALOG])
    sub.add_parser("rebuild-clients", help="rebuild + redeploy every client site (e.g. after setting PUBLIC_URL)")
    rp = sub.add_parser("preview-report", help="print last month's report for a client")
    rp.add_argument("lead_id", type=int)
    sub.add_parser("obsidian-sync", help="write CRM notes + dashboard into the Obsidian vault")
    un = sub.add_parser("unsubscribe", help="add an email to the do-not-contact list")
    un.add_argument("email")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db = DB(config.db_path)

    if config.dry_run and args.cmd in ("run", "daemon", "serve", "supervise"):
        logging.warning("DRY_RUN is on: nothing will be emailed, deployed or charged.")

    if args.cmd == "run":
        print(json.dumps(Orchestrator(db, config).run_once(), indent=2, default=str))
    elif args.cmd == "daemon":
        Orchestrator(db, config).run_forever(args.every)
    elif args.cmd == "supervise":
        from .supervisor import main as supervise
        supervise(config)
    elif args.cmd == "backup":
        from .supervisor import backup_database
        print(f"backup: {backup_database(config)}")
    elif args.cmd == "serve":
        from .server import serve
        serve(config, run_bots=not args.no_bots)
    elif args.cmd == "import-csv":
        print(f"imported {LeadBot(db, config).import_csv(args.path)} leads")
    elif args.cmd == "report":
        m = db.metrics(config.monthly_fee_cents)
        print(f"Pipeline:          {m['pipeline']}")
        print(f"Clients:           {m['clients']}")
        print(f"MRR:               ${m['mrr_cents'] / 100:,.2f}  "
              f"(base ${m['base_mrr_cents'] / 100:,.2f} + add-ons ${m['addon_mrr_cents'] / 100:,.2f})")
        print(f"Revenue collected: ${m['revenue_collected_cents'] / 100:,.2f}")
        print(f"Close rate:        {m['close_rate']:.1%}")
        print(f"Churn:             {m['churned']} ({m['churn_rate']:.1%})")
        print(f"Emails today:      {m['emails_today']}/{config.daily_email_cap}")
        print("\nInboxes:")
        for i in Mailer(db, config).status():
            health = "PAUSED" if i["paused"] else "warming" if i["warming"] else "ok"
            print(f"  {i['email']:<32} {i['sent_today']:>3}/{i['cap_today']:<3} sent  "
                  f"bounce {i['bounce_rate']:.1%}  {health}")
        stats = db.variant_stats()
        if stats:
            print("\nSubject A/B test:")
            for v in stats:
                print(f"  {v['variant']}: {v['sent']:>4} sent  {v['reply_rate']:>6.1%} replied  "
                      f"{v['win_rate']:>6.1%} won")
    elif args.cmd == "call-list":
        w = csv.writer(sys.stdout)
        w.writerow(["id", "business_name", "phone", "city", "preview_url", "payment_url"])
        for r in db.leads_by_status("built", "deployed"):
            if r["phone"] and not r["email"]:
                w.writerow([r["id"], r["business_name"], r["phone"], r["city"],
                            r["preview_url"], r["payment_url"]])
    elif args.cmd == "preview-email":
        lead = db.get_lead(args.lead_id)
        if not lead or not lead["email"]:
            print("lead not found or has no email", file=sys.stderr)
            return 1
        print(OutreachBot(db, config).compose(dict(lead), 0).as_string())
    elif args.cmd == "mark-paid":
        amount = args.amount or config.setup_fee_cents + config.monthly_fee_cents
        BillingBot(db, config).mark_paid(args.lead_id, f"manual_{args.lead_id}", amount, config.currency)
        print(f"lead {args.lead_id} marked paid; it will go live on the next `run`")
    elif args.cmd == "addon-activate":
        ok = UpsellBot(db, config).activate(args.lead_id, args.addon)
        print(f"{args.addon} {'activated' if ok else 'was already active'} for lead {args.lead_id}")
    elif args.cmd == "rebuild-clients":
        orch = Orchestrator(db, config)
        clients = db.leads_by_status("client")
        for row in clients:
            orch.builder.build(dict(row), preview=False)
        n = orch.deployer.run(statuses=("client",), next_status="client")
        print(f"rebuilt {len(clients)} client sites, redeployed {n}")
    elif args.cmd == "preview-report":
        from datetime import date
        lead = db.get_lead(args.lead_id)
        if not lead or not lead["email"]:
            print("lead not found or has no email", file=sys.stderr)
            return 1
        period, start, end = previous_period(date.today())
        msg, _ = ReportBot(db, config).compose(lead, period, start, end)
        print(f"Subject: {msg['Subject']}\n\n{msg.get_content()}")
    elif args.cmd == "obsidian-sync":
        result = ObsidianSync(db, config).run()
        print(f"{result} -> {config.obsidian_vault}")
    elif args.cmd == "unsubscribe":
        db.suppress(args.email, "manual")
        print(f"{args.email} will never be contacted")
    return 0


if __name__ == "__main__":
    sys.exit(main())

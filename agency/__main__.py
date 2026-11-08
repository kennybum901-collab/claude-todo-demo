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
from .config import config
from .db import DB
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
    sub.add_parser("report", help="show pipeline + revenue metrics")
    sub.add_parser("call-list", help="print phone-only leads to call (CSV)")
    pr = sub.add_parser("preview-email", help="print the email a lead would receive")
    pr.add_argument("lead_id", type=int)
    mp = sub.add_parser("mark-paid", help="record an offline payment (cash/check/Zelle)")
    mp.add_argument("lead_id", type=int)
    mp.add_argument("--amount", type=int, default=None, help="amount in cents")
    sub.add_parser("obsidian-sync", help="write CRM notes + dashboard into the Obsidian vault")
    un = sub.add_parser("unsubscribe", help="add an email to the do-not-contact list")
    un.add_argument("email")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db = DB(config.db_path)

    if config.dry_run and args.cmd in ("run", "daemon"):
        logging.warning("DRY_RUN is on: nothing will be emailed, deployed or charged.")

    if args.cmd == "run":
        print(json.dumps(Orchestrator(db, config).run_once(), indent=2, default=str))
    elif args.cmd == "daemon":
        Orchestrator(db, config).run_forever(args.every)
    elif args.cmd == "import-csv":
        print(f"imported {LeadBot(db, config).import_csv(args.path)} leads")
    elif args.cmd == "report":
        m = db.metrics(config.monthly_fee_cents)
        print(f"Pipeline:          {m['pipeline']}")
        print(f"Clients:           {m['clients']}")
        print(f"MRR:               ${m['mrr_cents'] / 100:,.2f}")
        print(f"Revenue collected: ${m['revenue_collected_cents'] / 100:,.2f}")
        print(f"Close rate:        {m['close_rate']:.1%}")
        print(f"Emails today:      {m['emails_today']}/{config.daily_email_cap}")
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
    elif args.cmd == "obsidian-sync":
        result = ObsidianSync(db, config).run()
        print(f"{result} -> {config.obsidian_vault}")
    elif args.cmd == "unsubscribe":
        db.suppress(args.email, "manual")
        print(f"{args.email} will never be contacted")
    return 0


if __name__ == "__main__":
    sys.exit(main())

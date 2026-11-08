"""Command-line interface:  python -m agency <command>"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
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
    sub.add_parser("dashboard", help="write the performance dashboard to data/dashboard.html")
    dg = sub.add_parser("digest", help="send today's review email now (or --print it)")
    dg.add_argument("--print", action="store_true", dest="print_only")
    sub.add_parser("pause", help="KILL SWITCH: stop all marketing email (outreach, offers, reports)")
    sub.add_parser("resume", help="resume marketing email after `pause`")
    sub.add_parser("qa", help="run quality checks on recent sites and emails")
    cs = sub.add_parser("company-site", help="generate your own company website (and --deploy it to Netlify)")
    cs.add_argument("--deploy", action="store_true")
    st = sub.add_parser("setup", help="guided setup: answers a few questions and writes .env for you")
    st.add_argument("--offline", action="store_true", help="skip the connection tests at the end")
    dr = sub.add_parser("doctor", help="check settings and test every connection before going live")
    dr.add_argument("--offline", action="store_true", help="only check settings, no network calls")
    dr.add_argument("--json", action="store_true")
    sub.add_parser("report", help="show pipeline + revenue metrics")
    tl = sub.add_parser("top-leads", help="show the highest-scoring leads not yet emailed")
    tl.add_argument("-n", type=int, default=15)
    sub.add_parser("call-list", help="print phone-only leads to call (CSV)")
    pr = sub.add_parser("preview-email", help="print the email a lead would receive")
    pr.add_argument("lead_id", type=int)
    mp = sub.add_parser("mark-paid", help="record an offline payment (cash/check/Zelle)")
    mp.add_argument("lead_id", type=int)
    mp.add_argument("--amount", type=int, default=None, help="amount in cents")
    ad = sub.add_parser("addon-activate", help="manually activate an add-on for a client")
    ad.add_argument("lead_id", type=int)
    ad.add_argument("addon", choices=[a.key for a in CATALOG])
    sub.add_parser("edits", help="list site edit requests waiting for approval")
    ea = sub.add_parser("edit-approve", help="approve a drafted site edit (goes live next run)")
    ea.add_argument("edit_id", type=int)
    er = sub.add_parser("edit-reject", help="reject / close a site edit request")
    er.add_argument("edit_id", type=int)
    sr = sub.add_parser("set-review-url", help="set a client's Google review link (for Review Booster)")
    sr.add_argument("lead_id", type=int)
    sr.add_argument("url")
    sub.add_parser("jobs-links", help="print each Review Booster client's private 'job done' link")
    sub.add_parser("referral-links", help="print every client's referral link")
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
    elif args.cmd == "dashboard":
        from .analytics import Analytics
        from .dashboard import render
        out = config.db_path.parent / "dashboard.html"
        out.write_text(render(Analytics(db, config).snapshot(), config.company_name), encoding="utf-8")
        print(f"dashboard written to {out}")
    elif args.cmd == "digest":
        from .digest import Digest
        d = Digest(db, config)
        if args.print_only:
            msg, n = d.compose()
            print(f"To: {msg['To']}\nSubject: {msg['Subject']}\n")
            print(msg.get_body(("plain",)).get_content())
        else:
            print("sent" if d.send(force=True) else "not sent: no inbox can send (check SMTP_* / senders.json)")
    elif args.cmd in ("pause", "resume"):
        db.set_setting("sending_paused", "1" if args.cmd == "pause" else None)
        db.log("control", args.cmd, "cli")
        print("marketing email PAUSED" if args.cmd == "pause" else "marketing email resumed")
    elif args.cmd == "qa":
        from .qa import check_email, check_site
        bad = 0
        for lead in db.conn.execute("SELECT * FROM leads WHERE site_slug IS NOT NULL ORDER BY id DESC LIMIT 25"):
            for p in check_site(lead, config):
                bad += 1
                print(f"site  #{lead['id']} {lead['business_name']}: {p}")
        for m in db.conn.execute("SELECT * FROM messages ORDER BY id DESC LIMIT 50"):
            for p in check_email(m["subject"], m["body"], config, bot=m["bot"]):
                bad += 1
                print(f"email #{m['id']} to {m['to_addr']}: {p}")
        print("all checks passed" if not bad else f"{bad} problem(s)")
    elif args.cmd == "company-site":
        from .company_site import build, deploy
        out = build(config)
        print(f"company website written to {out}/index.html")
        if args.deploy:
            print(f"live at {deploy(config, out, db)}  -> point {config.company_url} at it in Netlify > Domain settings")
        else:
            print("publish it: python -m agency company-site --deploy  (or drag the folder onto app.netlify.com/drop)")
    elif args.cmd == "setup":
        import importlib
        from . import config as config_module
        from .doctor import Doctor
        from .setup_wizard import Wizard, read_env
        wiz = Wizard()
        wiz.run()
        # Config defaults are read when the module loads: load the new .env, then reload
        for k, v in read_env(wiz.path).items():
            os.environ[k] = v
        fresh = importlib.reload(config_module).Config()
        print("\nChecking your setup...\n")
        doc = Doctor(fresh, offline=args.offline)
        doc.run()
        print(doc.render())
        return 1 if doc.r.blocking else 0
    elif args.cmd == "doctor":
        from .doctor import Doctor
        doc = Doctor(config, offline=args.offline)
        doc.run()
        print(doc.as_json() if args.json else doc.render())
        return 1 if doc.r.blocking else 0
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
        from .bots.winback_bot import WinbackBot
        wb = WinbackBot(db, config).stats()
        print(f"Win-back:          {wb['recovered']} won back of {wb['contacted']} contacted")
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
    elif args.cmd == "top-leads":
        from .scoring import LeadScorer
        LeadScorer(db, config).score_all()
        for r in db.leads_by_status("new", "built", "deployed", limit=args.n, by_score=True):
            print(f"{r['score'] or 0:>3}  #{r['id']:<5} {r['business_name'][:32]:<32} {r['score_reason'] or ''}")
    elif args.cmd == "call-list":
        w = csv.writer(sys.stdout)
        from .scoring import LeadScorer
        LeadScorer(db, config).score_all()
        w.writerow(["id", "score", "business_name", "phone", "city", "preview_url", "payment_url"])
        for r in db.leads_by_status("built", "deployed", by_score=True):  # best first
            if r["phone"] and (not r["email"] or (r["score"] or 0) < config.min_lead_score):
                w.writerow([r["id"], r["score"], r["business_name"], r["phone"], r["city"],
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
    elif args.cmd == "edits":
        for e in db.conn.execute("SELECT e.*, l.business_name FROM edits e JOIN leads l ON l.id=e.lead_id "
                                 "WHERE e.status IN ('new','drafted','needs_human') ORDER BY e.id"):
            print(f"#{e['id']:<4} {e['status']:<12} {e['business_name']}: {e['summary'] or e['message'][:60]}")
    elif args.cmd in ("edit-approve", "edit-reject"):
        from .edits import EditBot
        ok = EditBot(db, config).decide(args.edit_id, approve=args.cmd == "edit-approve")
        print("done" if ok else "not found, already decided, or nothing to apply automatically")
    elif args.cmd == "set-review-url":
        if not args.url.startswith("https://"):
            print("the review link must start with https://", file=sys.stderr)
            return 1
        db.update_lead(args.lead_id, review_url=args.url)
        print(f"review link set for lead {args.lead_id}")
    elif args.cmd == "jobs-links":
        from .reviews import has_booster, jobs_url, review_url_for
        for r in db.leads_by_status("client"):
            if has_booster(db, r["id"]):
                print(f"#{r['id']:<5} {r['business_name'][:30]:<30} {jobs_url(r['id'], config) or '(set PUBLIC_URL)'}"
                      + ("" if review_url_for(r) else "   ⚠ no review link: set-review-url"))
    elif args.cmd == "referral-links":
        from .referrals import code_for, link_for
        for r in db.leads_by_status("client"):
            print(f"#{r['id']:<5} {r['business_name'][:32]:<32} {link_for(r, config) or code_for(r, config.app_secret)}")
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

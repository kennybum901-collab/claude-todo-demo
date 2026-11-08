import os
import stat

from agency.setup_wizard import Wizard, read_env, render_env


def scripted(answers):
    it = iter(answers)

    def ask(prompt):
        try:
            return next(it)
        except StopIteration:
            raise AssertionError(f"unexpected prompt: {prompt}")
    return ask


FRESH = [
    # company
    "Northstar Web", "Alex", "hi@example.com", "hi@northstar.example", "9 Oak Ave, Austin, TX 78701",
    "http://northstar.example", "https://northstar.example",
    # pricing
    "abc", "399", "",
    # leads: niches, cities, places key (secret -> separate fn)
    "roofer, hair salon", "Austin, TX; Dallas, TX",
    # email: provider gmail, accept host, port, user, imap accept, digest default, alert url skip
    "1", "", "", "alex@get-northstar.example", "", "", "",
    # server: public url skip
    "",
    # services: optional extras? no
    "n",
]
SECRETS = ["", "app-pass-1234", "", "", ""]   # places skip, smtp pw, stripe skip, netlify skip, claude skip


def test_fresh_setup_writes_env(tmp_path):
    out = []
    w = Wizard(tmp_path / ".env", ask_fn=scripted(FRESH), secret_fn=scripted(SECRETS), out=out.append)
    assert w.run() is None
    env = read_env(tmp_path / ".env")
    assert env["COMPANY_EMAIL"] == "hi@northstar.example"            # placeholder was rejected and re-asked
    assert env["COMPANY_URL"] == "https://northstar.example"           # http was rejected
    assert env["SETUP_FEE_CENTS"] == "39900" and env["MONTHLY_FEE_CENTS"] == "4900"
    assert env["LEAD_QUERIES"] == "roofer|Austin, TX;roofer|Dallas, TX;hair salon|Austin, TX;hair salon|Dallas, TX"
    assert env["SMTP_HOST"] == "smtp.gmail.com" and env["IMAP_HOST"] == "imap.gmail.com"
    assert env["SMTP_PASSWORD"] == "app-pass-1234" and env["DIGEST_EMAIL"] == "hi@northstar.example"
    assert env["DRY_RUN"] == "1" and len(env["APP_SECRET"]) == 64 and len(env["ADMIN_PASSWORD"]) >= 20
    assert "STRIPE_SECRET_KEY" not in env and "GOOGLE_PLACES_API_KEY" not in env
    assert stat.S_IMODE(os.stat(tmp_path / ".env").st_mode) == 0o600
    assert any("placeholder" in line for line in out) and any("admin" in line for line in out)


def test_rerun_keeps_existing_values_and_backs_up(tmp_path):
    env_path = tmp_path / ".env"
    Wizard(env_path, ask_fn=scripted(FRESH), secret_fn=scripted(SECRETS), out=lambda s: None).run()
    first = read_env(env_path)
    keep_all = ["", "", "", "", "",   # company: keep
                "", "",               # pricing: keep
                "", "",               # leads: keep niches & cities
                "n",                  # don't change email settings
                "", "",               # digest keep, alert skip
                "",                   # public url skip
                "n"]                  # extras no
    backup = Wizard(env_path, ask_fn=scripted(keep_all), secret_fn=scripted(["", "", "", ""]),
                    out=lambda s: None).run()
    second = read_env(env_path)
    assert backup is not None and backup.exists() and read_env(backup) == first
    for k in ("COMPANY_NAME", "SETUP_FEE_CENTS", "LEAD_QUERIES", "SMTP_PASSWORD", "APP_SECRET", "ADMIN_PASSWORD"):
        assert second[k] == first[k], k                                 # secrets not regenerated


def test_render_quotes_values_with_spaces_and_keeps_unknown_keys():
    text = render_env({"COMPANY_ADDRESS": "9 Oak Ave, Austin", "CUSTOM_THING": "x", "DRY_RUN": "1"})
    assert 'COMPANY_ADDRESS="9 Oak Ave, Austin"' in text and "CUSTOM_THING=x" in text
    assert text.index("COMPANY_ADDRESS") < text.index("DRY_RUN") < text.index("CUSTOM_THING")


def test_custom_lead_queries_survive_rerun(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("COMPANY_NAME=X\nLEAD_QUERIES=roofer|Austin, TX;salon|Dallas, TX\n")
    w = Wizard(env_path, ask_fn=scripted(["", ""]), secret_fn=scripted([""]), out=lambda s: None)
    w.leads()
    assert w.v["LEAD_QUERIES"] == "roofer|Austin, TX;salon|Dallas, TX"   # not expanded to every combination

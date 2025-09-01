#!/usr/bin/env python3
import os, re, json, io, hashlib, datetime as dt, smtplib, ssl, argparse
import requests
from bs4 import BeautifulSoup
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

# ----------------------------
# Config
# ----------------------------
SOURCE_URL   = os.getenv("SOURCE_URL", "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/README.md")
STATE_FILE   = "state/sent.json"
TEMPLATE_PATH = os.getenv("TEMPLATE_PATH", "templates/email.html")  # <- your HTML file path

SMTP_SERVER  = os.environ["SMTP_SERVER"]
SMTP_PORT    = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER    = os.environ["SMTP_USER"]
SMTP_PASS    = os.environ["SMTP_PASS"]

# ----------------------------
# Helpers: recipients & state
# ----------------------------
def load_recipients() -> list[str]:
    """
    Preferred: recipients.json with {"recipients": ["a@x","b@y"]}.
    Fallback: RECIPIENT env (single address).
    """
    p = Path("recipients.json")
    if p.exists():
        data = json.loads(p.read_text(encoding="utf-8"))
        recips = data.get("recipients", [])
        if recips:
            return recips
    recip = os.getenv("RECIPIENT", "").strip()
    if recip:
        return [recip]
    raise RuntimeError("No recipients found. Provide recipients.json or RECIPIENT env.")

def ensure_state_dir():
    Path("state").mkdir(parents=True, exist_ok=True)

def load_state() -> set:
    ensure_state_dir()
    p = Path(STATE_FILE)
    if not p.exists():
        return set()
    return set(json.loads(p.read_text(encoding="utf-8")))

def save_state(ids: set):
    ensure_state_dir()
    Path(STATE_FILE).write_text(json.dumps(sorted(list(ids)), indent=2), encoding="utf-8")

# ----------------------------
# Fetch / parse
# ----------------------------
def fetch_markdown() -> str:
    r = requests.get(SOURCE_URL, timeout=30)
    r.raise_for_status()
    return r.text

def parse_age_to_minutes(age_text: str) -> int:
    """
    Robustly parse Age like:
      - 0d, 2d, 5h, 30m, 1w
      - 1 mo, 2 mos, 1 month, 3 months
      - 10 min, 45 mins, 1 hr, 2 hrs, 2 hours
      - today, new, just posted
    Returns minutes (smaller = newer). Unknown => very large number.
    """
    if not age_text:
        return 10**9

    s = age_text.strip().lower()

    if s in {"new", "just posted", "today"}:
        return 0

    # Remove punctuation
    s = re.sub(r"[^\w\s]", "", s)

    # Prefer months over minutes: 'mo'/'month' should not be matched as 'm'
    m = re.search(
        r"(?P<num>\d+)\s*("
        r"(?P<months>months?|mos?)|"
        r"(?P<weeks>w|weeks?)|"
        r"(?P<days>d|days?)|"
        r"(?P<hours>h|hrs?|hours?)|"
        r"(?P<minutes>m(?!o)|mins?|minutes?)"
        r")\b",
        s
    )

    if not m:
        # fallback for compact forms '1d','2h','30m'
        m2 = re.match(r"^(\d+)\s*([wdhm])$", s)
        if m2:
            num = int(m2.group(1)); unit = m2.group(2)
            return (num * 60 * 24 * 7) if unit == "w" else \
                   (num * 60 * 24)     if unit == "d" else \
                   (num * 60)          if unit == "h" else \
                   (num)               if unit == "m" else 10**9
        return 10**9

    num = int(m.group("num"))
    if m.group("months"):
        return num * 30 * 24 * 60
    if m.group("weeks"):
        return num * 7 * 24 * 60
    if m.group("days"):
        return num * 24 * 60
    if m.group("hours"):
        return num * 60
    if m.group("minutes"):
        return num
    return 10**9

def extract_tables_with_links(md_text: str) -> list[dict]:
    """
    Convert markdown to HTML, then parse tables and extract rows keeping link hrefs.
    We pick tables where headers include Company & (Role or Position).
    """
    try:
        from markdown import markdown
        html = markdown(md_text, output_format="html")
    except Exception:
        html = "<html><body><pre>" + md_text + "</pre></body></html>"

    soup = BeautifulSoup(html, "lxml")
    tables = soup.find_all("table")
    results = []
    for table in tables:
        ths = [th.get_text(strip=True) for th in table.find_all("th")]
        hdrs = [h.title() for h in ths]
        if not hdrs:
            continue
        if "Company" not in hdrs or (("Role" not in hdrs) and ("Position" not in hdrs)):
            continue

        col_idx = {h: i for i, h in enumerate(hdrs)}
        role_key = "Role" if "Role" in col_idx else ("Position" if "Position" in col_idx else None)

        tbody = table.find("tbody") or table
        for tr in tbody.find_all("tr"):
            tds = tr.find_all(["td", "th"])
            if not tds:
                continue

            def td_text(name: str, default: str = "") -> str:
                if name in col_idx and col_idx[name] < len(tds):
                    return tds[col_idx[name]].get_text(" ", strip=True)
                return default

            # Application URL: first link in cell
            app_url = ""
            if "Application" in col_idx and col_idx["Application"] < len(tds):
                first_link = tds[col_idx["Application"]].find("a", href=True)
                if first_link:
                    app_url = first_link["href"].strip()

            row = {
                "Company":     td_text("Company"),
                "Role":        td_text(role_key) if role_key else "",
                "Location":    td_text("Location"),
                "Date Posted": td_text("Date Posted"),
                "Sponsorship": td_text("Sponsorship"),
                "Application": app_url,
                "Age":         td_text("Age"),
            }
            results.append(row)
    return results

def job_id(row: dict) -> str:
    key = "|".join([
        row.get("Company","").strip(),
        row.get("Role","").strip(),
        row.get("Location","").strip(),
        row.get("Application","").strip()
    ])
    return hashlib.sha256(key.encode()).hexdigest()[:16]

def render_rows_html(rows: list[dict]) -> str:
    def a(url: str) -> str:
        return f'<a href="{url}">Apply</a>' if url.startswith("http") else "-"
    trs = []
    for r in rows:
        trs.append(f"""
        <tr>
          <td>{r.get('Company','')}</td>
          <td>{r.get('Role','')}</td>
          <td>{r.get('Location','')}</td>
          <td>{r.get('Date Posted','')}</td>
          <td>{r.get('Sponsorship','')}</td>
          <td>{a(r.get('Application',''))}</td>
          <td>{r.get('Age','')}</td>
        </tr>""")
    return "\n".join(trs) if trs else '<tr><td colspan="7">No rows.</td></tr>'

# ----------------------------
# Templating & email
# ----------------------------
def load_template() -> str:
    p = Path(TEMPLATE_PATH)
    if p.exists():
        return p.read_text(encoding="utf-8")
    # Minimal fallback if your template is missing
    return """
    <!doctype html>
    <html><body style="font-family:Arial,sans-serif;">
      <h2>{{title}}</h2>
      <p><small>Generated: {{generated}}</small></p>
      <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;width:100%;">
        <thead style="background:#f2f2f2;">
          <tr>
            <th>Company</th><th>Role</th><th>Location</th>
            <th>Date Posted</th><th>Sponsorship</th><th>Application</th><th>Age</th>
          </tr>
        </thead>
        <tbody>
          {{rows}}
        </tbody>
      </table>
      <p style="margin-top:12px;color:#777;font-size:12px;">Source: SimplifyJobs/New-Grad-Positions (personal reminder only).</p>
    </body></html>
    """

def render_with_template(title: str, rows_html: str, generated_utc: str) -> str:
    tpl = load_template()
    return (tpl
            .replace("{{title}}", title)
            .replace("{{rows}}", rows_html or '<tr><td colspan="7">No rows.</td></tr>')
            .replace("{{generated}}", generated_utc))

def send_email(subject: str, html_body: str, recipients: list[str]):
    if not SMTP_USER or not SMTP_PASS:
        raise RuntimeError("SMTP_USER/SMTP_PASS are empty. Check GitHub Secrets.")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_USER           # For Gmail, 'From' must match account
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html_body, "html"))

    # Use STARTTLS (587) by default; supports SSL (465) if you change the port
    if SMTP_PORT == 465:
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, context=ssl.create_default_context()) as server:
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, recipients, msg.as_string())
    else:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.ehlo()
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, recipients, msg.as_string())

# ----------------------------
# Main
# ----------------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--manual",
        action="store_true",
        help="Manual run: email the latest 50 newest jobs and save their IDs to state"
    )
    args = parser.parse_args()

    recipients = load_recipients()
    md = fetch_markdown()
    rows = extract_tables_with_links(md)

    # Add parsed age + IDs; sort newest first
    for r in rows:
        r["_age_minutes"] = parse_age_to_minutes(r.get("Age", ""))
        r["ID"] = job_id(r)
    rows.sort(key=lambda r: (r["_age_minutes"], r["Company"], r["Role"]))

    state = load_state()
    now = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    # ----- Manual run: send latest 50 and save their IDs -----
    if args.manual:
        latest_50 = rows[:50]
        html = render_with_template("Latest 50 jobs (manual run)", render_rows_html(latest_50), now)
        send_email("[Jobs Digest] Latest 50 (manual run)", html, recipients)
        new_ids = state.union({r["ID"] for r in latest_50})
        save_state(new_ids)
        print(f"Manual run: emailed latest 50; state now has {len(new_ids)} IDs.")
        return

    # ----- Hourly run: only send NEW jobs vs state -----
    new_rows = [r for r in rows if r["ID"] not in state]
    if not new_rows:
        html = render_with_template("No new jobs since last check", "", now)
        send_email("[Jobs Digest] No new jobs since last check", html, recipients)
        print("Cron run: no new jobs — 'no new' email sent.")
        return

    html = render_with_template(f"New Grad Jobs — {len(new_rows)} new since last run",
                                render_rows_html(new_rows), now)
    send_email(f"[Jobs Digest] {len(new_rows)} new roles from SimplifyJobs", html, recipients)

    new_ids = state.union({r["ID"] for r in new_rows})
    save_state(new_ids)
    print(f"Cron run: sent {len(new_rows)} new jobs; state updated.")

if __name__ == "__main__":
    main()
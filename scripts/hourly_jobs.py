import os, re, json, hashlib, datetime as dt, smtplib, ssl, requests
from pathlib import Path
from bs4 import BeautifulSoup
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ---------- Config ----------
SOURCE_URL     = os.getenv("SOURCE_URL", "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/README.md")
STATE_FILE     = "state/sent.json"  # holds the last 50 IDs only (replaced each run)
LATEST_TPL     = os.getenv("LATEST_TEMPLATE_PATH", "templates/latest_jobs_template.html")  # your new HTML template
NO_NEW_TPL     = os.getenv("NO_NEW_TEMPLATE_PATH", "templates/no_jobs.html")       # optional; fallback inline if missing
MAX_LATEST     = 50

SMTP_SERVER = os.environ["SMTP_SERVER"]
SMTP_PORT   = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER   = os.environ["SMTP_USER"]
SMTP_PASS   = os.environ["SMTP_PASS"]

# ---------- Recipients / State ----------
def load_recipients() -> list[str]:
    p = Path("recipients.json")
    if not p.exists():
        raise RuntimeError("recipients.json not found. Create it like: {\"recipients\":[\"you@example.com\",\"alt@example.com\"]}")
    data = json.loads(p.read_text(encoding="utf-8"))
    recips = [r.strip() for r in data.get("recipients", []) if isinstance(r, str) and r.strip()]
    if not recips:
        raise RuntimeError("No valid recipients in recipients.json")
    return recips

def ensure_state_dir():
    Path("state").mkdir(parents=True, exist_ok=True)

def load_state_ids() -> set:
    """Load the previous run's 50 IDs (or empty set if none)."""
    ensure_state_dir()
    p = Path(STATE_FILE)
    if not p.exists():
        return set()
    return set(json.loads(p.read_text(encoding="utf-8")))

def save_state_ids_replace(ids: list[str]):
    """Replace the state with exactly these IDs (the current latest 50)."""
    ensure_state_dir()
    Path(STATE_FILE).write_text(json.dumps(sorted(list(ids)), indent=2), encoding="utf-8")

# ---------- Fetch / Parse ----------
def fetch_markdown() -> str:
    r = requests.get(SOURCE_URL, timeout=30)
    r.raise_for_status()
    return r.text

def parse_age_to_minutes(age_text: str) -> int:
    """
    Parse Age like: 0d, 2d, 5h, 30m, 1w, 1 mo, 2 months, 'today', 'new', 'just posted'.
    Smaller minutes => newer. Unknown => large number.
    """
    if not age_text:
        return 10**9
    s = age_text.strip().lower()
    if s in {"new", "just posted", "today"}:
        return 0
    s = re.sub(r"[^\w\s]", "", s)
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
        m2 = re.match(r"^(\d+)\s*([wdhm])$", s)
        if m2:
            num = int(m2.group(1)); u = m2.group(2)
            return (num * 60 * 24 * 7) if u == "w" else \
                   (num * 60 * 24)     if u == "d" else \
                   (num * 60)          if u == "h" else \
                   (num)               if u == "m" else 10**9
        return 10**9
    num = int(m.group("num"))
    if m.group("months"):  return num * 30 * 24 * 60
    if m.group("weeks"):   return num * 7 * 24 * 60
    if m.group("days"):    return num * 24 * 60
    if m.group("hours"):   return num * 60
    if m.group("minutes"): return num
    return 10**9

def extract_rows_with_links(md_text: str) -> list[dict]:
    """
    Markdown -> HTML -> parse tables and preserve Application hrefs.
    Only include tables that have Company + (Role or Position).
    """
    try:
        from markdown import markdown
        html = markdown(md_text, output_format="html")
    except Exception:
        html = "<html><body><pre>" + md_text + "</pre></body></html>"

    soup = BeautifulSoup(html, "lxml")
    out = []
    for table in soup.find_all("table"):
        ths = [th.get_text(strip=True).title() for th in table.find_all("th")]
        if not ths:
            continue
        if "Company" not in ths or (("Role" not in ths) and ("Position" not in ths)):
            continue

        idx = {h: i for i, h in enumerate(ths)}
        role_key = "Role" if "Role" in idx else "Position"
        tbody = table.find("tbody") or table

        for tr in tbody.find_all("tr"):
            tds = tr.find_all(["td","th"])
            if not tds:
                continue

            def cell(name: str, default: str = "") -> str:
                return tds[idx[name]].get_text(" ", strip=True) if name in idx and idx[name] < len(tds) else default

            app = ""
            if "Application" in idx and idx["Application"] < len(tds):
                a = tds[idx["Application"]].find("a", href=True)
                if a:
                    app = a["href"].strip()

            row = {
                "Company":     cell("Company"),
                "Role":        cell(role_key),
                "Location":    cell("Location"),
                "Date Posted": cell("Date Posted"),
                "Sponsorship": cell("Sponsorship"),
                "Application": app,
                "Age":         cell("Age"),
            }
            out.append(row)
    return out

def job_id(row: dict) -> str:
    key = "|".join([
        (row.get("Company") or "").strip(),
        (row.get("Role") or "").strip(),
        (row.get("Location") or "").strip(),
        (row.get("Application") or "").strip(),
    ])
    return hashlib.sha256(key.encode()).hexdigest()[:16]

# ---------- Templates & Email ----------
def load_text_file(path: str) -> str | None:
    p = Path(path)
    if p.exists():
        return p.read_text(encoding="utf-8")
    return None

def render_latest_html(rows: list[dict]) -> str:
    """
    Render the 'latest jobs' email using LATEST_TPL if present.
    Placeholders expected: {{title}}, {{generated}}, {{rows}}
    """
    tpl = load_text_file(LATEST_TPL) or """
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

    def link(u: str) -> str:
        return f'<a href="{u}">Apply</a>' if u and u.startswith("http") else "-"

    trs = []
    for r in rows:
        trs.append(f"""
        <tr>
          <td>{r.get('Company','')}</td>
          <td>{r.get('Role','')}</td>
          <td>{r.get('Location','')}</td>
          <td>{r.get('Date Posted','')}</td>
          <td>{r.get('Sponsorship','')}</td>
          <td>{link(r.get('Application',''))}</td>
          <td>{r.get('Age','')}</td>
        </tr>""")
    html_rows = "\n".join(trs) if trs else '<tr><td colspan="7">No rows.</td></tr>'

    return (tpl
            .replace("{{title}}", "New roles in the latest 50")
            .replace("{{generated}}", dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"))
            .replace("{{rows}}", html_rows))

def render_no_new_html() -> str:
    tpl = load_text_file(NO_NEW_TPL)
    if tpl:
        return tpl.replace("{{generated}}", dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"))
    # fallback
    return f"""
    <!doctype html>
    <html><body style="font-family:Arial,sans-serif;">
      <h3>No new jobs since last check</h3>
      <p><small>{dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")}</small></p>
    </body></html>
    """

def send_email(subject: str, html_body: str, recipients: list[str]):
    if not SMTP_USER or not SMTP_PASS:
        raise RuntimeError("SMTP_USER/SMTP_PASS empty.")
    if not recipients:
        raise RuntimeError("No recipients to send to.")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SMTP_USER
    msg["To"]      = ", ".join(recipients)
    msg.attach(MIMEText(html_body, "html"))

    if SMTP_PORT == 465:
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, context=ssl.create_default_context()) as s:
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_USER, recipients, msg.as_string())
    else:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as s:
            s.starttls(context=ssl.create_default_context())
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_USER, recipients, msg.as_string())

# ---------- Main ----------
def main():
    recipients = load_recipients()
    prev_ids   = load_state_ids()

    md   = fetch_markdown()
    rows = extract_rows_with_links(md)

    # compute sortable age + stable ID
    for r in rows:
        r["_age_minutes"] = parse_age_to_minutes(r.get("Age", ""))
        r["ID"] = job_id(r)

    # newest first (smaller age first); fallback tie-breakers
    rows.sort(key=lambda r: (r["_age_minutes"], r.get("Company",""), r.get("Role","")))

    # take only the latest 50
    latest_50 = rows[:MAX_LATEST]

    # new rows = in latest 50 but not in previous state's 50
    new_rows = [r for r in latest_50 if r["ID"] not in prev_ids]

    if not new_rows:
        html = render_no_new_html()
        send_email("[Jobs Digest] No new jobs since last check", html, recipients)
        print("Hourly: no new jobs — 'no new' email sent.")
    else:
        html = render_latest_html(new_rows)
        send_email(f"[Jobs Digest] {len(new_rows)} new roles (latest 50 window)", html, recipients)
        print(f"Hourly: sent {len(new_rows)} new jobs.")

    # IMPORTANT: replace state with the CURRENT 50 IDs
    save_state_ids_replace([r["ID"] for r in latest_50])
    print(f"State updated with {len(latest_50)} latest IDs.")

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
import os, re, json, io, hashlib, datetime as dt, smtplib, ssl, requests
from pathlib import Path
from bs4 import BeautifulSoup
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

SOURCE_URL   = os.getenv("SOURCE_URL", "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/README.md")
STATE_FILE   = "state/sent.json"
TEMPLATE_PATH = os.getenv("TEMPLATE_PATH", "templates/email.html")

SMTP_SERVER  = os.environ["SMTP_SERVER"]
SMTP_PORT    = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER    = os.environ["SMTP_USER"]
SMTP_PASS    = os.environ["SMTP_PASSWORD"]
RECIPIENT    = os.environ["RECIPIENT_JSON"]  # single recipient

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

def fetch_markdown() -> str:
    r = requests.get(SOURCE_URL, timeout=30)
    r.raise_for_status()
    return r.text

def parse_age_to_minutes(s: str) -> int:
    if not s: return 10**9
    s = s.strip().lower()
    if s in {"new","today","just posted"}: return 0
    s = re.sub(r"[^\w\s]", "", s)
    m = re.search(
        r"(?P<num>\d+)\s*("
        r"(?P<months>months?|mos?)|"
        r"(?P<weeks>w|weeks?)|"
        r"(?P<days>d|days?)|"
        r"(?P<hours>h|hrs?|hours?)|"
        r"(?P<minutes>m(?!o)|mins?|minutes?)"
        r")\b", s)
    if not m:
        m2 = re.match(r"^(\d+)\s*([wdhm])$", s)
        if m2:
            num = int(m2.group(1)); u = m2.group(2)
            return num*60*24*7 if u=="w" else num*60*24 if u=="d" else num*60 if u=="h" else num
        return 10**9
    num = int(m.group("num"))
    if m.group("months"): return num*30*24*60
    if m.group("weeks"):  return num*7*24*60
    if m.group("days"):   return num*24*60
    if m.group("hours"):  return num*60
    if m.group("minutes"):return num
    return 10**9

def extract_rows_with_links(md_text: str):
    try:
        from markdown import markdown
        html = markdown(md_text, output_format="html")
    except Exception:
        html = "<html><body><pre>" + md_text + "</pre></body></html>"
    soup = BeautifulSoup(html, "lxml")
    out = []
    for table in soup.find_all("table"):
        ths = [th.get_text(strip=True).title() for th in table.find_all("th")]
        if not ths: continue
        if "Company" not in ths or (("Role" not in ths) and ("Position" not in ths)): continue
        idx = {h:i for i,h in enumerate(ths)}
        role_key = "Role" if "Role" in idx else "Position"
        tbody = table.find("tbody") or table
        for tr in tbody.find_all("tr"):
            tds = tr.find_all(["td","th"])
            if not tds: continue
            def cell(name, default=""):
                return tds[idx[name]].get_text(" ", strip=True) if name in idx and idx[name] < len(tds) else default
            app = ""
            if "Application" in idx and idx["Application"] < len(tds):
                a = tds[idx["Application"]].find("a", href=True)
                if a: app = a["href"].strip()
            row = {
                "Company": cell("Company"),
                "Role": cell(role_key),
                "Location": cell("Location"),
                "Date Posted": cell("Date Posted"),
                "Sponsorship": cell("Sponsorship"),
                "Application": app,
                "Age": cell("Age"),
            }
            out.append(row)
    return out

def job_id(row: dict) -> str:
    key = "|".join([
        row.get("Company","").strip(),
        row.get("Role","").strip(),
        row.get("Location","").strip(),
        row.get("Application","").strip()
    ])
    return hashlib.sha256(key.encode()).hexdigest()[:16]

def render_rows_html(rows: list[dict]) -> str:
    def link(u): return f'<a href="{u}">Apply</a>' if u.startswith("http") else "-"
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
    return "\n".join(trs) if trs else '<tr><td colspan="7">No rows.</td></tr>'

def load_template() -> str:
    p = Path(TEMPLATE_PATH)
    if p.exists(): return p.read_text(encoding="utf-8")
    return """
    <!doctype html><html><body style="font-family:Arial">
      <h2>{{title}}</h2><p><small>Generated: {{generated}}</small></p>
      <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;width:100%">
        <thead><tr>
          <th>Company</th><th>Role</th><th>Location</th>
          <th>Date Posted</th><th>Sponsorship</th><th>Application</th><th>Age</th>
        </tr></thead>
        <tbody>{{rows}}</tbody>
      </table>
    </body></html>
    """

def render_with_template(title: str, rows_html: str, generated_utc: str) -> str:
    return (load_template()
            .replace("{{title}}", title)
            .replace("{{generated}}", generated_utc)
            .replace("{{rows}}", rows_html))

def send_email(subject: str, html_body: str):
    if not SMTP_USER or not SMTP_PASS: raise RuntimeError("SMTP_USER/SMTP_PASS empty.")
    msg = MIMEMultipart("alternative")
    msg["Subject"]= subject
    msg["From"]   = SMTP_USER
    msg["To"]     = RECIPIENT
    msg.attach(MIMEText(html_body, "html"))
    if SMTP_PORT == 465:
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, context=ssl.create_default_context()) as s:
            s.login(SMTP_USER, SMTP_PASS); s.sendmail(SMTP_USER, [RECIPIENT], msg.as_string())
    else:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as s:
            s.starttls(context=ssl.create_default_context())
            s.login(SMTP_USER, SMTP_PASS); s.sendmail(SMTP_USER, [RECIPIENT], msg.as_string())

def main():
    state = load_state()
    md = fetch_markdown()
    rows = extract_rows_with_links(md)

    # Add parsed age + IDs; sort newest first (not strictly needed for diff, but nicer)
    for r in rows:
        r["_age_minutes"] = parse_age_to_minutes(r.get("Age",""))
        r["ID"] = job_id(r)
    rows.sort(key=lambda r: (r["_age_minutes"], r["Company"], r["Role"]))

    # Only truly NEW rows
    new_rows = [r for r in rows if r["ID"] not in state]
    now = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    if not new_rows:
        html = render_with_template("No new jobs since last check", "", now)
        send_email("[Jobs Digest] No new jobs since last check", html)
        print("Hourly: no new jobs — 'no new' email sent.")
        return

    html = render_with_template(f"{len(new_rows)} new jobs since last run",
                                render_rows_html(new_rows), now)
    send_email(f"[Jobs Digest] {len(new_rows)} new roles", html)

    # Update state
    new_ids = state.union({r["ID"] for r in new_rows})
    save_state(new_ids)
    print(f"Hourly: sent {len(new_rows)} new jobs; state updated to {len(new_ids)} IDs.")

if __name__ == "__main__":
    main()

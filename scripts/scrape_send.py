import os, io, re, json, hashlib, datetime as dt, smtplib, ssl, argparse
import requests
from bs4 import BeautifulSoup
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import argparse

SOURCE_URL   = os.getenv("SOURCE_URL", "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/README.md")
STATE_FILE   = "state/sent.json"

SMTP_SERVER  = os.environ["SMTP_SERVER"]
SMTP_PORT    = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER    = os.environ["SMTP_USER"]
SMTP_PASS    = os.environ["SMTP_PASS"]

def load_recipients():
    # recipients.json preferred; fallback to RECIPIENT env for single address
    if os.path.exists("recipients.json"):
        with open("recipients.json", "r") as f:
            data = json.load(f)
            recips = data.get("recipients", [])
            if recips:
                return recips
    recip = os.getenv("RECIPIENT", "").strip()
    if recip:
        return [recip]
    raise RuntimeError("No recipients found. Provide recipients.json or RECIPIENT env.")

def ensure_state_dir():
    os.makedirs("state", exist_ok=True)

def load_state() -> set:
    ensure_state_dir()
    if not os.path.exists(STATE_FILE):
        return set()
    with open(STATE_FILE, "r") as f:
        return set(json.load(f))

def save_state(ids: set):
    ensure_state_dir()
    with open(STATE_FILE, "w") as f:
        json.dump(sorted(list(ids)), f, indent=2)

def fetch_markdown() -> str:
    r = requests.get(SOURCE_URL, timeout=30)
    r.raise_for_status()
    return r.text

def parse_age_to_minutes(age_text: str) -> int:
    """
    Convert strings like '0d', '2d', '5h', '30m', '1w' to minutes.
    Unknown formats get a large number to push them to the end.
    Smaller minutes = newer.
    """
    s = (age_text or "").strip().lower()
    if not s: return 10**9
    m = re.match(r"^(\d+)\s*([wdhm])$", s)
    if not m:
        # sometimes they use '0d' or '1d'; handle emojis/whitespace etc.
        digits = re.findall(r"\d+", s)
        unit   = 'd' if 'd' in s else ('h' if 'h' in s else ('m' if 'm' in s else ('w' if 'w' in s else 'd')))
        if digits:
            m = (int(digits[0]), unit)
        else:
            return 10**9
        val, u = m
    else:
        val, u = int(m.group(1)), m.group(2)

    if u == 'm': return val
    if u == 'h': return val * 60
    if u == 'd': return val * 60 * 24
    if u == 'w': return val * 60 * 24 * 7
    return 10**9

def extract_tables_with_links(md_text: str):
    """
    Convert markdown to HTML, then parse tables and extract rows keeping link hrefs.
    We pick tables where headers include Company & (Role or Position).
    """
    # Let GitHub render-like markdown via a lightweight conversion: the README is already markdown;
    # GitHub's raw is markdown text. We'll push it through a minimal converter by using
    # pandoc-like? Not available here. Instead, GitHub tables in markdown are simple enough to be
    # interpreted by BeautifulSoup if we first create a dummy HTML wrapper and let GitHub-style
    # tables persist? Simpler: use a markdown-to-HTML endpoint? Not allowed. We'll do a quick
    # approach: many tables are already pipe-formatted. We'll rely on a tiny fallback:
    # Use Python-Markdown would be ideal, but we removed it to keep deps slim.
    # We'll include a minimal fallback: if we see "<table" already (sometimes repo has HTML tables),
    # parse directly. Otherwise, we do a very small naive markdown table to HTML converter for links.
    # To avoid complexity here, we’ll actually use BeautifulSoup on the markdown turned to HTML by
    # GitHub-like library; since we don't have it here, we'll include bs4 + lxml and a tiny helper.
    try:
        from markdown import markdown
        html = markdown(md_text, output_format="html")
    except Exception:
        # basic fallback, unlikely to be used in Actions because we can include 'markdown' in requirements
        html = "<html><body><pre>" + md_text + "</pre></body></html>"

    soup = BeautifulSoup(html, "lxml")
    tables = soup.find_all("table")
    results = []
    for table in tables:
        # headers
        ths = [th.get_text(strip=True) for th in table.find_all("th")]
        hdrs = [h.title() for h in ths]
        if not hdrs:
            continue
        if "Company" not in hdrs or (("Role" not in hdrs) and ("Position" not in hdrs)):
            continue

        # normalize header indexes
        col_idx = {h: i for i, h in enumerate(hdrs)}
        # Accept either 'Role' or 'Position' as Role
        role_key = "Role" if "Role" in col_idx else ("Position" if "Position" in col_idx else None)

        # rows
        for tr in table.find("tbody").find_all("tr"):
            tds = tr.find_all(["td", "th"])
            if not tds:
                continue

            def td_text(name, default=""):
                if name in col_idx and col_idx[name] < len(tds):
                    return tds[col_idx[name]].get_text(" ", strip=True)
                return default

            # Application URL: take the first <a href> from that cell, if present
            app_url = ""
            if "Application" in col_idx and col_idx["Application"] < len(tds):
                first_link = tds[col_idx["Application"]].find("a", href=True)
                if first_link:
                    app_url = first_link["href"].strip()

            row = {
                "Company":   td_text("Company"),
                "Role":      td_text(role_key) if role_key else "",
                "Location":  td_text("Location"),
                "Date Posted": td_text("Date Posted"),
                "Sponsorship": td_text("Sponsorship"),
                "Application": app_url,
                "Age":       td_text("Age"),
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

def send_email(subject: str, html_body: str, recipients: list[str]):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html_body, "html"))

    context = ssl.create_default_context()
    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls(context=context)
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, recipients, msg.as_string())

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

    # Sort by "newest" using Age (smaller minutes = newer)
    for r in rows:
        r["_age_minutes"] = parse_age_to_minutes(r.get("Age", ""))
        r["ID"] = job_id(r)
    rows.sort(key=lambda r: (r["_age_minutes"], r["Company"], r["Role"]))

    state = load_state()
    now = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    # ----- Manual run: send latest 50 and save their IDs -----
    if args.manual:
        latest_50 = rows[:50]
        table_rows = render_rows_html(latest_50)
        html = f"""
        <html><body style="font-family:Arial,sans-serif;">
          <h2>Latest 50 jobs (manual run)</h2>
          <p><small>Generated: {now}</small></p>
          <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;width:100%;">
            <thead style="background:#f2f2f2;">
              <tr>
                <th>Company</th><th>Role</th><th>Location</th>
                <th>Date Posted</th><th>Sponsorship</th><th>Application</th><th>Age</th>
              </tr>
            </thead>
            <tbody>
              {table_rows}
            </tbody>
          </table>
          <p style="margin-top:12px;color:#777;font-size:12px;">Source: SimplifyJobs/New-Grad-Positions (personal reminder only).</p>
        </body></html>
        """
        send_email("[Jobs Digest] Latest 50 (manual run)", html, recipients)

        # Save their IDs so hourly runs don't resend them
        new_ids = state.union({r["ID"] for r in latest_50})
        save_state(new_ids)
        print(f"Manual run: emailed latest 50; state now has {len(new_ids)} IDs.")
        return

    # ----- Hourly run: only send NEW jobs vs state -----
    new_rows = [r for r in rows if r["ID"] not in state]

    if not new_rows:
        subject = "[Jobs Digest] No new jobs since last check"
        html = f"""
        <html><body style="font-family:Arial,sans-serif;">
          <h3>No new jobs since last check</h3>
          <p><small>{now}</small></p>
          <p>This is an automated reminder based on SimplifyJobs/New-Grad-Positions (personal use only).</p>
        </body></html>
        """
        send_email(subject, html, recipients)
        print("No new jobs; 'no new' email sent.")
        return

    # Build HTML email with just the new rows (show newest first)
    table_rows = render_rows_html(new_rows)
    html = f"""
    <html>
    <body style="font-family:Arial,sans-serif;">
      <h2>New Grad Jobs — {len(new_rows)} new since last run</h2>
      <p><small>Generated: {now}</small></p>
      <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;width:100%;">
        <thead style="background:#f2f2f2;">
          <tr>
            <th>Company</th><th>Role</th><th>Location</th>
            <th>Date Posted</th><th>Sponsorship</th><th>Application</th><th>Age</th>
          </tr>
        </thead>
        <tbody>
          {table_rows}
        </tbody>
      </table>
      <p style="margin-top:12px;color:#777;font-size:12px;">Source: SimplifyJobs/New-Grad-Positions (personal reminder only).</p>
    </body>
    </html>
    """
    subject = f"[Jobs Digest] {len(new_rows)} new roles from SimplifyJobs"
    send_email(subject, html, recipients)

    # Update state so we won't resend the same rows next time
    new_ids = state.union({r["ID"] for r in new_rows})
    save_state(new_ids)
    print(f"Sent {len(new_rows)} new jobs; state updated.")

if __name__ == "__main__":
    main()
import os, io, json, hashlib, datetime as dt, smtplib, ssl, requests
import pandas as pd
from markdown import markdown
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from bs4 import BeautifulSoup

from pathlib import Path

def load_recipients() -> list[str]:
    p = Path("recipients.json")
    if not p.exists():
        raise RuntimeError("recipients.json not found. Create it like: {\"recipients\":[\"you@example.com\",\"alt@example.com\"]}")
    data = json.loads(p.read_text(encoding="utf-8"))
    recips = [r.strip() for r in data.get("recipients", []) if isinstance(r, str) and r.strip()]
    if not recips:
        raise RuntimeError("No valid recipients in recipients.json")
    return recips

SOURCE_URL = os.getenv("SOURCE_URL", "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/README.md")
STATE_FILE = "state/sent.json"
TEMPLATE_PATH = "templates/email.html"

SMTP_SERVER = os.environ["SMTP_SERVER"]
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ["SMTP_USER"]
SMTP_PASS = os.environ["SMTP_PASS"]



def extract_rows_with_links(md_text: str) -> list[dict]:
    """
    Parse markdown to HTML and walk tables so we can keep <a href> links.
    Returns a list of dict rows with 'Application' as the URL.
    """
    html = markdown(md_text, output_format="html")
    soup = BeautifulSoup(html, "lxml")

    rows = []
    for table in soup.find_all("table"):
        # Normalize headers to Title Case to match your pipeline
        headers = [th.get_text(strip=True).title() for th in table.find_all("th")]
        if not headers:
            continue
        # Must contain Company and (Role or Position)
        if "Company" not in headers or (("Role" not in headers) and ("Position" not in headers)):
            continue

        idx = {h: i for i, h in enumerate(headers)}
        role_key = "Role" if "Role" in idx else "Position"

        tbody = table.find("tbody") or table
        for tr in tbody.find_all("tr"):
            tds = tr.find_all(["td", "th"])
            if not tds:
                continue

            def cell(name: str, default: str = "") -> str:
                return tds[idx[name]].get_text(" ", strip=True) if name in idx and idx[name] < len(tds) else default

            # Application link: first <a href> in that cell, if any
            app_url = ""
            if "Application" in idx and idx["Application"] < len(tds):
                a = tds[idx["Application"]].find("a", href=True)
                if a:
                    app_url = a["href"].strip()

            row = {
                "Company":     cell("Company"),
                "Role":        cell(role_key),
                "Location":    cell("Location"),
                "Date Posted": cell("Date Posted"),
                "Sponsorship": cell("Sponsorship"),
                "Application": app_url,         # <-- preserved URL
            }
            rows.append(row)
    return rows

def load_state() -> set:
    os.makedirs("state", exist_ok=True)
    if not os.path.exists(STATE_FILE):
        return set()
    with open(STATE_FILE, "r") as f:
        return set(json.load(f))

def save_state(ids: set):
    with open(STATE_FILE, "w") as f:
        json.dump(sorted(list(ids)), f, indent=2)

def md_to_tables(md_text: str) -> list[pd.DataFrame]:
    # Convert Markdown -> HTML -> DataFrames
    html = markdown(md_text, output_format="html")
    soup = BeautifulSoup(html, "lxml")
    return pd.read_html(io.StringIO(str(soup)))

def stable_row_id(row: pd.Series) -> str:
    # Deterministic ID based on common columns
    cols = [c for c in ["Company", "Role", "Location", "Application", "Date Posted", "Sponsorship"] if c in row.index]
    key = "|".join(str(row.get(c, "")).strip() for c in cols)
    return hashlib.sha256(key.encode()).hexdigest()[:16]

def fetch_jobs_df() -> pd.DataFrame:
    resp = requests.get(SOURCE_URL, timeout=30)
    resp.raise_for_status()
    md = resp.text

    # Use BeautifulSoup-based extractor to preserve hrefs
    rows = extract_rows_with_links(md)
    if not rows:
        raise RuntimeError("No job rows found; upstream schema may have changed.")

    df = pd.DataFrame(rows)

    # Canonicalize columns you expect
    df.columns = [str(c).strip().title() for c in df.columns]
    if "Position" in df.columns and "Role" not in df.columns:
        df.rename(columns={"Position": "Role"}, inplace=True)
    if "Locations" in df.columns and "Location" not in df.columns:
        df.rename(columns={"Locations": "Location"}, inplace=True)

    # Reorder to your preferred subset if present
    keep_order = ["Company","Role","Location","Application","Date Posted","Sponsorship","Notes"]
    present = [c for c in keep_order if c in df.columns]
    if present:
        df = df[present]

    # Normalize strings
    for c in df.columns:
        df[c] = df[c].astype(str).fillna("").str.strip()

    # Add stable IDs
    df["ID"] = df.apply(stable_row_id, axis=1)
    return df

def render_html(rows_df: pd.DataFrame) -> str:
    if os.path.exists(TEMPLATE_PATH):
        with open(TEMPLATE_PATH, "r") as f:
            template = f.read()
    else:
        # Minimal inline fallback
        template = """
        <html><body>
          <h2>New Grad Jobs — New since last run</h2>
          <p><small>Generated: {{generated}}</small></p>
          <table border="1" cellpadding="6" cellspacing="0">
            <thead>
              <tr>
                <th>Company</th><th>Role</th><th>Location</th>
                <th>Date Posted</th><th>Sponsorship</th><th>Application</th>
              </tr>
            </thead>
            <tbody>
              {{rows}}
            </tbody>
          </table>
          <p><small>Source: SimplifyJobs/New-Grad-Positions (personal reminder only).</small></p>
        </body></html>
        """

    def linkify(u: str) -> str:
        return f'<a href="{u}">Apply</a>' if u.startswith("http") else (u or "-")

    tr = []
    for _, r in rows_df.iterrows():
        tr.append(f"""
        <tr>
          <td>{r.get('Company','')}</td>
          <td>{r.get('Role','')}</td>
          <td>{r.get('Location','')}</td>
          <td>{r.get('Date Posted','')}</td>
          <td>{r.get('Sponsorship','')}</td>
          <td>{linkify(r.get('Application',''))}</td>
        </tr>""")

    html_rows = "\n".join(tr) if tr else '<tr><td colspan="6">No new jobs found.</td></tr>'
    return (template
            .replace("{{generated}}", dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"))
            .replace("{{rows}}", html_rows))

def send_email(subject: str, html_body: str, recipients: list[str]):
    if not recipients:
        raise RuntimeError("No recipients to send to.")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = ", ".join(recipients)  # header
    msg.attach(MIMEText(html_body, "html"))

    context = ssl.create_default_context()
    if SMTP_PORT == 465:
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, context=context) as server:
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, recipients, msg.as_string())
    else:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls(context=context)
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, recipients, msg.as_string())

def main():
    prev_ids = load_state()
    df = fetch_jobs_df()

    # Always take only the first 50 jobs
    latest_df = df.head(50).copy()

    if latest_df.empty:
        print("No jobs found; skipping email.")
        return

    html = render_html(latest_df)
    subject = f"[Jobs Digest] Latest {len(latest_df)} roles from SimplifyJobs"

    recipients = load_recipients()
    print(f"Sending to {len(recipients)} recipients.")
    send_email(subject, html, recipients)

    # Persist their IDs so hourly/diff job can skip them
    new_ids = prev_ids.union(set(latest_df["ID"].tolist()))
    save_state(new_ids)
    print(f"Sent {len(latest_df)} rows; state updated with first 50 job IDs.")

if __name__ == "__main__":
    main()
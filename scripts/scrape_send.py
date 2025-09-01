import os, io, json, hashlib, datetime as dt, smtplib, ssl, requests
import pandas as pd
from markdown import markdown
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from bs4 import BeautifulSoup

SOURCE_URL = os.getenv("SOURCE_URL", "https://github.com/SimplifyJobs/New-Grad-Positions/blob/dev/README.md")
STATE_FILE = "state/sent.json"
TEMPLATE_PATH = "templates/email.html"

SMTP_SERVER = os.environ["SMTP_SERVER"]
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ["SMTP_USER"]
SMTP_PASS = os.environ["SMTP_PASS"]
# If RECIPIENT env var is set → single recipient fallback
# Else load recipients.json from repo
recipients = []
if os.getenv("RECIPIENT"):
    recipients = [os.environ["RECIPIENT"]]
elif os.path.exists("recipients.json"):
    with open("recipients.json", "r") as f:
        data = json.load(f)
        recipients = data.get("recipients", [])

if not recipients:
    raise RuntimeError("No recipients found. Set RECIPIENT env or create recipients.json")

'''FILTER_LOCATION = os.getenv("FILTER_LOCATION", "").strip()  
FILTER_SPONSORSHIP = os.getenv("FILTER_SPONSORSHIP", "").strip()  
MAX_ROWS = int(os.getenv("MAX_ROWS", "80"))'''

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

    tables = md_to_tables(md)
    candidates = []
    for t in tables:
        t.columns = [str(c).strip().title() for c in t.columns]
        cols = set(t.columns)
        if {"Company", "Role"}.issubset(cols) or {"Company", "Position"}.issubset(cols):
            if "Position" in t.columns and "Role" not in t.columns:
                t.rename(columns={"Position": "Role"}, inplace=True)
            if "Locations" in t.columns and "Location" not in t.columns:
                t.rename(columns={"Locations": "Location"}, inplace=True)
            candidates.append(t)

    if not candidates:
        raise RuntimeError("No job tables found; upstream schema may have changed.")

    df = pd.concat(candidates, ignore_index=True)

    # Canonical subset if present
    keep_order = ["Company","Role","Location","Application","Date Posted","Sponsorship","Notes"]
    present = [c for c in keep_order if c in df.columns]
    df = df[present]

    # Normalize to strings
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
    prev_ids = load_state()
    df = fetch_jobs_df()

    # New = rows we haven't emailed before
    new_df = df[~df["ID"].isin(prev_ids)].copy()

    if new_df.empty:
        print("No new jobs; skipping email.")
        return

    html = render_html(new_df)
    subject = f"[Jobs Digest] {len(new_df)} new roles from SimplifyJobs"
    send_email(subject, html)

    # Persist new IDs
    new_ids = prev_ids.union(set(new_df["ID"].tolist()))
    save_state(new_ids)
    print(f"Sent {len(new_df)} rows; state updated.")

if __name__ == "__main__":
    main()
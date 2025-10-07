# Job Snooper

📬 Automated job digester built on top of the [SimplifyJobs/New-Grad-Positions](https://github.com/SimplifyJobs/New-Grad-Positions) repo.  
This workflow scrapes the latest job tables, emails updates using HTML templates, and tracks state to avoid sending duplicates.

---

## 🚀 Features

- **Daily seed run (9 AM UTC or manual)**  
  - Emails the **latest 50 jobs**.  
  - Stores their IDs into `state/sent.json`.  

- **Hourly diff run**  
  - Looks at the **current latest 50 jobs**.  
  - Sends only the jobs that weren’t included in the previous state.  
  - If no new jobs → sends a “No new jobs” email.  
  - Updates `state/sent.json` with the fresh 50 IDs each run.  

- **Custom templates**  
  - `templates/latest_jobs.html` → used for new jobs email.  
  - `templates/no_jobs.html` (optional) → used for “no new jobs” emails.  

- **Multi-recipient support**  
  - Recipients are configured in a JSON file (`recipients.json`) written from a GitHub Actions secret.  

---

## 📂 Project Structure

    .github/
    workflows/
        scraper_jobs.yml           # Daily/manual workflow (emails latest 50)
        latest_jobs.yml    # Hourly workflow (emails only new jobs)
    scripts/
        scrape_and_send_jobs.py          # Script: fetch + send latest 50
        hourly_jobs.py  # Script: hourly diff sender
    state/
        sent.json                     # Stores last 50 job IDs (committed after runs)
    templates/
        email.html
        latest_jobs.html              # HTML template for new jobs
        no_jobs.html                   # (optional) template for no new jobs
    recipients.json                 # Written dynamically from secret

---

## ⚙️ Setup

### 1. Secrets

Add these in **GitHub → Repo → Settings → Secrets and variables → Actions**:

- `SMTP_SERVER` – e.g. `smtp.gmail.com`  
- `SMTP_PORT` – e.g. `587` (TLS) or `465` (SSL)  
- `SMTP_USER` – your SMTP username (email address)  
- `SMTP_PASS` – your SMTP password (or Gmail App Password)  
- `RECIPIENTS_JSON` – JSON string of recipients, e.g.:

json
        {
          "recipients": [
            "you@example.com",
            "friend@example.com"
          ]
        }


⸻

2. Templates
	•	Place your email template at templates/*.
Must contain placeholders:
	•	{{title}}
	•	{{generated}}
	•	{{rows}}


3. Workflows

Daily Seed Workflow (.github/workflows/scraper_jobs.yml)
	•	Runs once a day at 09:00 UTC (or manually from Actions tab).
	•	Fetches the latest 50 jobs, emails them, and saves IDs.

Hourly Diff Workflow (.github/workflows/latest_jobs.yml)
	•	Runs every hour (or manually).
	•	Triggered automatically after the seed workflow completes.
	•	Sends only new jobs among the latest 50.
	•	If no new jobs, sends a “no new” notification.

⸻

📧 Email Behavior
	•	New jobs → styled HTML email with job table and Apply links.
	•	No new jobs → small notification email.
	•	Supports multiple recipients.

⸻

🛠️ Local Development

You can run scripts locally for testing:

### install deps
pip install requests beautifulsoup4 lxml markdown

### set env vars
export SMTP_SERVER="smtp.gmail.com"
export SMTP_PORT=587
export SMTP_USER="your_email@gmail.com"
export SMTP_PASS="your_app_password"

### prepare recipients.json
echo '{"recipients":["you@example.com"]}' > recipients.json

### run seed (latest 50)
python scripts/seed_send_50_jobs.py

### run hourly diff
python scripts/hourly_latest50_diff_send.py


⸻

📜 License

This project is for personal use only.
Jobs data comes from SimplifyJobs/New-Grad-Positions.





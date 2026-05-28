# Customer Journey Analytics Dashboard

A data analytics dashboard that tracks the full customer lifecycle — from first marketing contact through to purchase — across multiple platforms and branches. Built with Flask and Google Sheets as the data source, served as a single-page dashboard with Chart.js visualizations.

---

## What it does

The dashboard connects to three Google Sheets tabs (Shops, Leads_2025, Whatsapp) and gives a unified view of:

- **Who your leads are** and which platform (Instagram, TikTok, Facebook, etc.) and source (Direct, Ad) brought them in
- **Which leads are hot, warm, or cold** based on last activity date
- **Which leads converted** to paying customers, and how long the journey took
- **Revenue, spend, and ROI** per marketing channel
- **Branch-level performance** — leads, conversions, and top customers per branch
- **Individual customer history** — every interaction and purchase for any contact, searchable by name or phone number

---

## Dashboard sections

| Section | What it shows |
|---|---|
| Executive Summary | Total unique leads (Leads_2025 + WhatsApp, deduplicated), conversions, revenue, repeat customers |
| Lead Status | Hot / Warm / Cold breakdown with scrollable per-lead lists |
| Marketing Metrics | Leads and conversions by source |
| WhatsApp Activity | Which activity types drive the most conversions |
| Platform Performance | Leads and conversion rate by platform (Instagram, TikTok, etc.) |
| Marketing Source ROI | Revenue, spend, ROI and cost-per-lead by channel |
| Time to First Purchase | Distribution of days from first contact to sale |
| Branch Performance | Side-by-side branch comparison |
| Conversion Funnel | Stage-by-stage funnel from lead to repeat customer |
| Unit Economics | CAC, CPL, AOV, LTV, overall ROI |
| Top 10 Customers by Branch | Highest-value customers per branch with interaction history |
| Lead → Purchase Journey | Every converted lead with dates and days-to-convert; table shows latest 100, CSV export returns full dataset |
| Unconverted Leads | Leads that haven't purchased, with time-in-funnel buckets |
| Customer History Lookup | Full interaction + purchase history for any contact |

---

## Google Sheets structure

### Shops
| Column | Description |
|---|---|
| Date | Transaction date |
| Phone | Customer phone (primary key — links to leads) |
| Price | Sale amount (Ksh) |
| Location | Branch / shop location |
| META ADS | Meta ad spend for that day |
| TIKTOK ADS | TikTok ad spend for that day |

### Leads_2025
| Column | Description |
|---|---|
| Date | Lead date |
| CONTACT | Phone number (primary key) |
| NAME | Customer name |
| BRANCH | Branch the lead is assigned to |
| Source | How the customer engaged — Direct or via Ad |
| Platform | Where the engagement happened — Instagram, TikTok, Facebook, etc. |

### Whatsapp
| Column | Description |
|---|---|
| DATE | Engagement date |
| CONTACT | Phone number (primary key) |
| NAME | Customer name |
| SOURCE | Original lead source |
| ACTIVITY | WhatsApp activity type |
| BRANCH | Branch |

Phone numbers across all three sheets are normalized to `254XXXXXXXXX` format before matching, so `0712345678`, `+254712345678`, and `254712345678` all resolve to the same contact.

---

## Local setup

**Prerequisites:** Python 3.10+, a Google Cloud service account with Sheets API access, and editor access to the Google Sheet.

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Copy the environment template and fill in your values
cp .env.example .env
```

Set these values in `.env`:

```env
GOOGLE_CREDENTIALS_PATH=C:\path\to\your\service-account.json
SPREADSHEET_ID=your_spreadsheet_id_here
```

```bash
# 3. Start the server
python app.py
# Opens on http://localhost:5005
```

---

## Vercel deployment

The app is deployed on Vercel at:
**https://customer-journey-analytics-ay0vdq4a6-oduor254s-projects.vercel.app**

### Environment variables required in Vercel

| Variable | Value |
|---|---|
| `GOOGLE_CREDENTIALS_B64` | Base64-encoded contents of your service account JSON (see below) |
| `SPREADSHEET_ID` | Your Google Sheet ID |

### Generating the base64 credential string

Run this in PowerShell and paste the output into Vercel:

```powershell
[Convert]::ToBase64String(
  [System.IO.File]::ReadAllBytes("C:\path\to\service-account.json")
) | Set-Clipboard
```

The app checks for credentials in this order:
1. `GOOGLE_CREDENTIALS_B64` — base64-encoded JSON string
2. `GOOGLE_CREDENTIALS_JSON` — raw JSON string
3. `GOOGLE_CREDENTIALS_PATH` — file path (local / Render)

After adding or changing environment variables, go to **Deployments → Redeploy** in the Vercel dashboard for the changes to take effect.

---

## API endpoints

### `GET /api/dashboard-data`

Returns all dashboard metrics. Accepts optional query parameters:

| Parameter | Values |
|---|---|
| `timeFilter` | `all`, `weekly`, `monthly`, `quarterly`, `yearly` |
| `source` | Any source name or `all` |
| `location` | Any location name or `all` |
| `search` | Name or phone number fragment |

### `GET /api/customer-lookup?q=<query>`

Returns full interaction and purchase history for a single customer. `q` can be a name or phone number.

### `GET /api/health`

Returns `{"status": "healthy", "cache_age_seconds": <n>}`. `cache_age_seconds` is `null` until the first data fetch completes.

---

## Project structure

```
├── app.py                  # Flask backend — data processing and API
├── templates/
│   └── Dashboard.html      # Single-page frontend dashboard
├── requirements.txt        # Python dependencies
├── vercel.json             # Vercel deployment config
├── Procfile                # Gunicorn start command (Render / local)
├── .env                    # Local environment variables (not committed)
└── .gitignore
```

---

## Lead classification logic

| Status | Condition |
|---|---|
| Hot | Last activity within 30 days |
| Warm | Last activity 31–90 days ago |
| Cold | No activity in over 90 days |

Future-dated records (data entry errors) are excluded before classification.

---

## Tech stack

- **Backend**: Python 3.12, Flask, pandas, gspread
- **Frontend**: Vanilla JS, Chart.js 3, CSS custom properties (dark theme)
- **Data source**: Google Sheets via service account
- **Hosting**: Vercel (serverless Python)
- **CI/CD**: Auto-deploy on push to `main`

---

## Security notes

- Google credentials are never committed to the repository
- The `.env` file and all `*credentials*.json` / `*service_account*.json` files are in `.gitignore`
- On Vercel, credentials are stored as an encrypted environment variable
- The Google Sheet is shared only with the service account email

---

Built with Flask, Google Sheets API, and Chart.js · © 2026

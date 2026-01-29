# Staff Referral Program

A web application for FirstLine Schools staff to submit referrals and track their status, with an admin view for the Talent team to manage the workflow.

## Features

- **Staff View**: Submit referrals, look up your referral status, track pending/paid bonuses
- **Admin View**: Dashboard with statistics, manage all referrals, update status, track 60-day completion
- **Google Sheets Backend**: Easy data management for HR, no database setup required
- **Google OAuth**: Secure admin authentication

## Tech Stack

- Flask (Python backend)
- Tailwind CSS (frontend styling)
- Google Sheets API (data storage)
- Google OAuth 2.0 (admin authentication)
- Google Cloud Run (deployment)

## Setup

### 1. Create Google Sheet

1. Create a new Google Sheet
2. Rename the first sheet to "Referrals"
3. Add headers in row 1:
   ```
   referral_id | submitted_at | referrer_name | referrer_email | referrer_school | candidate_name | candidate_email | candidate_phone | position | position_type | bonus_amount | relationship | already_applied | notes | status | status_updated_at | status_updated_by | hire_date | sixty_day_date | payout_month | paid_date | admin_notes
   ```
4. Note the Sheet ID from the URL: `https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit`

### 2. Create Service Account

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Create a new project or select existing
3. Enable the Google Sheets API
4. Go to IAM & Admin > Service Accounts
5. Create a service account
6. Create a JSON key and download it
7. Share your Google Sheet with the service account email (Editor access)

### 3. Set Up OAuth (for Admin Login)

1. In Google Cloud Console, go to APIs & Services > OAuth consent screen
2. Configure the consent screen for internal users
3. Go to APIs & Services > Credentials
4. Create an OAuth 2.0 Client ID (Web application)
5. Add authorized redirect URIs:
   - Local: `http://localhost:5000/auth/callback`
   - Production: `https://your-cloud-run-url/auth/callback`

### 4. Environment Variables

Create a `.env` file or set these environment variables:

```bash
# Required
SHEET_ID=your-google-sheet-id
SERVICE_ACCOUNT_JSON='{"type":"service_account",...}'  # Full JSON as string

# For OAuth (admin login)
GOOGLE_CLIENT_ID=your-oauth-client-id
GOOGLE_CLIENT_SECRET=your-oauth-client-secret
SECRET_KEY=a-random-secret-key-for-sessions
```

### 5. Local Development

```bash
# Install dependencies
pip install -r requirements.txt

# Set environment variables
export SHEET_ID=your-sheet-id
export SERVICE_ACCOUNT_JSON='...'
export GOOGLE_CLIENT_ID=...
export GOOGLE_CLIENT_SECRET=...
export SECRET_KEY=dev-secret
export FLASK_DEBUG=true

# Run the app
python app.py
```

Visit http://localhost:5000

## Deployment to Cloud Run

### Using gcloud CLI

```bash
# Build and deploy
gcloud run deploy staff-referral-program \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --set-env-vars SHEET_ID=your-sheet-id \
  --set-env-vars GOOGLE_CLIENT_ID=your-client-id \
  --set-env-vars GOOGLE_CLIENT_SECRET=your-client-secret \
  --set-env-vars SECRET_KEY=your-secret-key \
  --set-env-vars SERVICE_ACCOUNT_JSON='...'
```

### Using Cloud Console

1. Go to Cloud Run in Google Cloud Console
2. Create a new service
3. Build from source (upload this directory)
4. Configure environment variables in the "Variables & Secrets" section
5. Allow unauthenticated invocations
6. Deploy

### After Deployment

1. Note your Cloud Run URL
2. Update OAuth redirect URI to include: `https://your-url/auth/callback`
3. Test the full flow

## Admin Users

The following email addresses have admin access:
- sshirey@firstlineschools.org
- brichardson@firstlineschools.org
- talent@firstlineschools.org
- hr@firstlineschools.org

To modify admin users, edit the `ADMIN_USERS` list in `app.py`.

## Bonus Structure

- **Lead Teacher positions**: $500 bonus
- **All other positions**: $300 bonus
- Bonuses are paid after the hired candidate completes 60 days

## Status Workflow

1. `Submitted` - New referral received
2. `Under Review` - Talent team is reviewing
3. `Candidate Applied` - Candidate is in the ATS
4. `Interviewing` - Candidate is in interview process
5. `Hired` - Hired! 60-day countdown started
6. `Eligible` - 60 days complete, ready for payout
7. `Paid` - Bonus has been paid

Other statuses:
- `Not Hired` - Candidate was not selected
- `Candidate Left` - Left before completing 60 days
- `Ineligible` - Doesn't qualify (with reason in notes)

## Support

For questions, contact talent@firstlineschools.org

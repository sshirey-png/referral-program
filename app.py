"""
Staff Referral Program - Flask Backend
FirstLine Schools
"""

import os
import json
import uuid
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, request, jsonify, send_file, session, redirect, url_for
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix
from google.cloud import bigquery
from authlib.integrations.flask_client import OAuth

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY') or os.urandom(32)
# Trust proxy headers (required for Cloud Run to detect https)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
CORS(app)

# Configuration
GOOGLE_CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID')
GOOGLE_CLIENT_SECRET = os.environ.get('GOOGLE_CLIENT_SECRET')
PROJECT_ID = os.environ.get('GOOGLE_CLOUD_PROJECT', 'talent-demo-482004')
DATASET_ID = 'staff_referral'
TABLE_ID = 'referrals'

# Email Configuration
SMTP_EMAIL = os.environ.get('SMTP_EMAIL', 'talent@firstlineschools.org')
SMTP_PASSWORD = os.environ.get('SMTP_PASSWORD', '')
SMTP_SERVER = 'smtp.gmail.com'
SMTP_PORT = 587
TALENT_TEAM_EMAIL = 'talent@firstlineschools.org'
HR_EMAIL = 'hr@firstlineschools.org'
CPO_EMAIL = 'sshirey@firstlineschools.org'  # CC on new referrals and weekly updates

# Admin users who can access the admin panel
ADMIN_USERS = [
    'sshirey@firstlineschools.org',
    'brichardson@firstlineschools.org',
    'talent@firstlineschools.org',
    'hr@firstlineschools.org',
    'awatts@firstlineschools.org',
    'jlombas@firstlineschools.org',
    'aleibfritz@firstlineschools.org'
]

# Status values and their display order
STATUS_VALUES = [
    'Submitted',
    'Under Review',
    'Candidate Applied',
    'Interviewing',
    'Hired',
    'Eligible',
    'Paid',
    'Not Hired',
    'Withdrawn/Non-responsive',
    'Candidate Left before 60 Days',
    'Ineligible'
]

# BigQuery client
bq_client = bigquery.Client(project=PROJECT_ID)


def ensure_is_archived_column():
    """One-time migration: add is_archived column if it doesn't exist."""
    try:
        full_table = f"{PROJECT_ID}.{DATASET_ID}.{TABLE_ID}"
        table_ref = bq_client.get_table(full_table)
        existing_fields = [f.name for f in table_ref.schema]
        if 'is_archived' not in existing_fields:
            # Step 1: Add the column
            bq_client.query(f"ALTER TABLE `{full_table}` ADD COLUMN is_archived BOOL").result()
            # Step 2: Set default value
            bq_client.query(f"ALTER TABLE `{full_table}` ALTER COLUMN is_archived SET DEFAULT FALSE").result()
            # Step 3: Backfill existing rows
            bq_client.query(f"UPDATE `{full_table}` SET is_archived = FALSE WHERE TRUE").result()
            logger.info("Added is_archived column to referrals table")
    except Exception as e:
        logger.error(f"Migration error (is_archived): {e}")


ensure_is_archived_column()

# OAuth setup
oauth = OAuth(app)
if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
    google = oauth.register(
        name='google',
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
        client_kwargs={'scope': 'openid email profile'}
    )
else:
    google = None


# ============ Email Functions ============

def send_email(to_email, subject, html_body, cc_emails=None):
    """Send an email using Gmail SMTP."""
    if not SMTP_PASSWORD:
        logger.warning("SMTP_PASSWORD not configured, skipping email")
        return False

    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = f"FirstLine Schools Talent <{SMTP_EMAIL}>"
        msg['To'] = to_email
        if cc_emails:
            msg['Cc'] = ', '.join(cc_emails)

        msg.attach(MIMEText(html_body, 'html'))

        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_EMAIL, SMTP_PASSWORD)
            recipients = [to_email] + (cc_emails or [])
            server.sendmail(SMTP_EMAIL, recipients, msg.as_string())

        logger.info(f"Email sent to {to_email}: {subject}")
        return True
    except Exception as e:
        logger.error(f"Failed to send email: {e}")
        return False


def send_referral_confirmation(referral):
    """Send confirmation email to referrer when they submit a referral."""
    subject = f"Referral Submitted - {referral['candidate_name']}"
    html_body = f"""
    <div style="font-family: 'Open Sans', Arial, sans-serif; max-width: 600px; margin: 0 auto;">
        <div style="background-color: #002f60; padding: 20px; text-align: center;">
            <h1 style="color: white; margin: 0;">Staff Referral Program</h1>
        </div>
        <div style="padding: 30px; background-color: #f8f9fa;">
            <h2 style="color: #002f60;">Thank you for your referral!</h2>
            <p>Hi {referral['referrer_name']},</p>
            <p>We've received your referral for <strong>{referral['candidate_name']}</strong> for the <strong>{referral['position']}</strong> position.</p>

            <div style="background-color: white; border-radius: 8px; padding: 20px; margin: 20px 0;">
                <p style="margin: 5px 0;"><strong>Referral ID:</strong> {referral['referral_id']}</p>
                <p style="margin: 5px 0;"><strong>Candidate:</strong> {referral['candidate_name']}</p>
                <p style="margin: 5px 0;"><strong>Position:</strong> {referral['position']}</p>
                <p style="margin: 5px 0;"><strong>Potential Bonus:</strong> <span style="color: #e47727; font-size: 1.2em;">${referral['bonus_amount']}</span></p>
            </div>

            <p><strong>What's next?</strong></p>
            <ul>
                <li>Make sure {referral['candidate_name']} applies and lists you as their referrer</li>
                <li>Our Talent team will review the application</li>
                <li>You'll receive updates as the process moves forward</li>
                <li>If hired, your bonus will be paid after they complete 60 days</li>
            </ul>

            <p>You can check your referral status anytime at the Staff Referral Program portal.</p>

            <p style="color: #666; font-size: 0.9em; margin-top: 30px;">Questions? Reply to this email or contact the Talent team.</p>
        </div>
        <div style="background-color: #002f60; padding: 15px; text-align: center;">
            <p style="color: white; margin: 0; font-size: 0.9em;">FirstLine Schools - Education For Life</p>
        </div>
    </div>
    """
    send_email(referral['referrer_email'], subject, html_body)


def send_new_referral_alert(referral):
    """Send alert to Talent team when a new referral is submitted."""
    subject = f"New Referral: {referral['candidate_name']} for {referral['position']}"
    html_body = f"""
    <div style="font-family: 'Open Sans', Arial, sans-serif; max-width: 600px; margin: 0 auto;">
        <div style="background-color: #002f60; padding: 20px; text-align: center;">
            <h1 style="color: white; margin: 0;">New Staff Referral</h1>
        </div>
        <div style="padding: 30px; background-color: #f8f9fa;">
            <h2 style="color: #e47727;">New referral submitted!</h2>

            <div style="background-color: white; border-radius: 8px; padding: 20px; margin: 20px 0;">
                <h3 style="color: #002f60; margin-top: 0;">Candidate Information</h3>
                <p style="margin: 5px 0;"><strong>Name:</strong> {referral['candidate_name']}</p>
                <p style="margin: 5px 0;"><strong>Email:</strong> {referral['candidate_email']}</p>
                <p style="margin: 5px 0;"><strong>Phone:</strong> {referral.get('candidate_phone', 'Not provided')}</p>
                <p style="margin: 5px 0;"><strong>Position:</strong> {referral['position']} ({referral['position_type']})</p>
                <p style="margin: 5px 0;"><strong>Already Applied:</strong> {referral.get('already_applied', 'Not yet')}</p>
            </div>

            <div style="background-color: white; border-radius: 8px; padding: 20px; margin: 20px 0;">
                <h3 style="color: #002f60; margin-top: 0;">Referrer Information</h3>
                <p style="margin: 5px 0;"><strong>Name:</strong> {referral['referrer_name']}</p>
                <p style="margin: 5px 0;"><strong>Email:</strong> {referral['referrer_email']}</p>
                <p style="margin: 5px 0;"><strong>School:</strong> {referral['referrer_school']}</p>
                <p style="margin: 5px 0;"><strong>Relationship:</strong> {referral['relationship']}</p>
            </div>

            <div style="background-color: #fff3cd; border-radius: 8px; padding: 15px; margin: 20px 0;">
                <p style="margin: 0;"><strong>Referral ID:</strong> {referral['referral_id']}</p>
                <p style="margin: 5px 0 0 0;"><strong>Bonus Amount:</strong> ${referral['bonus_amount']}</p>
            </div>

            {f"<p><strong>Notes:</strong> {referral['notes']}</p>" if referral.get('notes') else ""}
        </div>
    </div>
    """
    send_email(TALENT_TEAM_EMAIL, subject, html_body, cc_emails=[CPO_EMAIL])


def send_status_update(referral, old_status, new_status, updated_by):
    """Send status update email to referrer."""
    status_messages = {
        'Under Review': "Our Talent team is now reviewing your referral.",
        'Candidate Applied': f"{referral['candidate_name']} has applied and is in our system!",
        'Interviewing': f"{referral['candidate_name']} is now in the interview process!",
        'Hired': f"Great news! {referral['candidate_name']} has been hired! Your ${referral['bonus_amount']} bonus will be paid after they complete 60 days.",
        'Eligible': f"Your referral bonus of ${referral['bonus_amount']} is now eligible for payout! It will be processed soon.",
        'Paid': f"Your ${referral['bonus_amount']} referral bonus has been added to payroll and is being processed. Thank you for helping us build a great team!",
        'Not Hired': f"Unfortunately, {referral['candidate_name']} was not selected for this position. Thank you for your referral.",
        'Withdrawn/Non-responsive': f"{referral['candidate_name']} has withdrawn or is no longer responsive. Thank you for your referral.",
        'Candidate Left before 60 Days': f"Unfortunately, {referral['candidate_name']} left before completing 60 days. The referral bonus is no longer eligible.",
        'Ineligible': "This referral has been marked as ineligible. Please contact Talent if you have questions."
    }

    message = status_messages.get(new_status, f"The status has been updated to: {new_status}")

    # Choose color based on status
    if new_status in ['Hired', 'Eligible', 'Paid']:
        status_color = '#22c55e'  # Green
    elif new_status in ['Not Hired', 'Withdrawn/Non-responsive', 'Candidate Left before 60 Days', 'Ineligible']:
        status_color = '#ef4444'  # Red
    else:
        status_color = '#e47727'  # Orange

    subject = f"Referral Update: {referral['candidate_name']} - {new_status}"
    html_body = f"""
    <div style="font-family: 'Open Sans', Arial, sans-serif; max-width: 600px; margin: 0 auto;">
        <div style="background-color: #002f60; padding: 20px; text-align: center;">
            <h1 style="color: white; margin: 0;">Referral Status Update</h1>
        </div>
        <div style="padding: 30px; background-color: #f8f9fa;">
            <p>Hi {referral['referrer_name']},</p>

            <div style="background-color: white; border-radius: 8px; padding: 20px; margin: 20px 0; text-align: center;">
                <p style="margin: 0 0 10px 0;">Your referral for <strong>{referral['candidate_name']}</strong></p>
                <p style="font-size: 1.5em; color: {status_color}; margin: 0; font-weight: bold;">{'Processing' if new_status == 'Paid' else new_status}</p>
            </div>

            <p>{message}</p>

            <div style="background-color: white; border-radius: 8px; padding: 15px; margin: 20px 0;">
                <p style="margin: 5px 0;"><strong>Referral ID:</strong> {referral['referral_id']}</p>
                <p style="margin: 5px 0;"><strong>Position:</strong> {referral['position']}</p>
                <p style="margin: 5px 0;"><strong>Potential Bonus:</strong> ${referral['bonus_amount']}</p>
            </div>

            <p style="color: #666; font-size: 0.9em; margin-top: 30px;">Questions? Reply to this email or contact the Talent team.</p>
        </div>
        <div style="background-color: #002f60; padding: 15px; text-align: center;">
            <p style="color: white; margin: 0; font-size: 0.9em;">FirstLine Schools - Education For Life</p>
        </div>
    </div>
    """
    send_email(referral['referrer_email'], subject, html_body)


def send_eligible_payout_alert(referral):
    """Send alert to HR, Talent, and Payroll Manager when a referral becomes eligible for payout."""
    PAYROLL_MANAGER_EMAIL = 'aleibfritz@firstlineschools.org'
    PAYROLL_EMAIL = 'payroll@firstlineschools.org'
    APP_URL = 'https://staff-referral-program-965913991496.us-central1.run.app'

    subject = f"Referral Bonus Ready for Payout: {referral['candidate_name']} - ${referral['bonus_amount']}"
    html_body = f"""
    <div style="font-family: 'Open Sans', Arial, sans-serif; max-width: 600px; margin: 0 auto;">
        <div style="background-color: #002f60; padding: 20px; text-align: center;">
            <h1 style="color: white; margin: 0;">Referral Bonus Payout</h1>
        </div>
        <div style="padding: 30px; background-color: #f8f9fa;">
            <h2 style="color: #22c55e;">Ready for Payout!</h2>
            <p>A referral bonus is now eligible for payment:</p>

            <div style="background-color: white; border-radius: 8px; padding: 20px; margin: 20px 0;">
                <h3 style="color: #002f60; margin-top: 0;">Payout Details</h3>
                <p style="margin: 5px 0;"><strong>Amount:</strong> <span style="color: #22c55e; font-size: 1.3em; font-weight: bold;">${referral['bonus_amount']}</span></p>
                <p style="margin: 5px 0;"><strong>Pay To:</strong> {referral['referrer_name']}</p>
                <p style="margin: 5px 0;"><strong>Referrer Email:</strong> {referral['referrer_email']}</p>
                <p style="margin: 5px 0;"><strong>School/Dept:</strong> {referral['referrer_school']}</p>
            </div>

            <div style="background-color: white; border-radius: 8px; padding: 20px; margin: 20px 0;">
                <h3 style="color: #002f60; margin-top: 0;">Hired Candidate</h3>
                <p style="margin: 5px 0;"><strong>Name:</strong> {referral['candidate_name']}</p>
                <p style="margin: 5px 0;"><strong>Position:</strong> {referral['position']}</p>
                <p style="margin: 5px 0;"><strong>Hire Date:</strong> {referral.get('hire_date', 'N/A')}</p>
                <p style="margin: 5px 0;"><strong>60-Day Completion:</strong> {referral.get('sixty_day_date', 'N/A')}</p>
            </div>

            <div style="background-color: #fff3cd; border-radius: 8px; padding: 15px; margin: 20px 0;">
                <p style="margin: 0;"><strong>Referral ID:</strong> {referral['referral_id']}</p>
                <p style="margin: 5px 0 0 0;"><strong>Scheduled Payout:</strong> {referral.get('payout_month', 'N/A')}</p>
            </div>

            <p>Once the payout has been processed, please update the status to <strong>"Paid"</strong> using the link below:</p>

            <p style="text-align: center; margin: 25px 0;">
                <a href="{APP_URL}/?admin=true"
                   style="background: #22c55e; color: white; padding: 14px 30px; text-decoration: none; border-radius: 8px; display: inline-block; font-weight: bold; font-size: 1.1em;">
                    Update Status to Paid
                </a>
            </p>
        </div>
        <div style="background-color: #002f60; padding: 15px; text-align: center;">
            <p style="color: white; margin: 0; font-size: 0.9em;">FirstLine Schools - Education For Life</p>
        </div>
    </div>
    """
    # Send directly to Payroll Manager, CC HR and Talent
    send_email(PAYROLL_MANAGER_EMAIL, subject, html_body, cc_emails=[PAYROLL_EMAIL, HR_EMAIL, TALENT_TEAM_EMAIL])


# ============ BigQuery Functions ============

def get_full_table_id():
    """Get the fully qualified table ID."""
    return f"{PROJECT_ID}.{DATASET_ID}.{TABLE_ID}"


def row_to_dict(row):
    """Convert a BigQuery row to a dictionary."""
    return {
        'referral_id': row.referral_id,
        'submitted_at': row.submitted_at.isoformat() if row.submitted_at else '',
        'referrer_name': row.referrer_name or '',
        'referrer_email': row.referrer_email or '',
        'referrer_school': row.referrer_school or '',
        'candidate_name': row.candidate_name or '',
        'candidate_email': row.candidate_email or '',
        'candidate_phone': row.candidate_phone or '',
        'position': row.position or '',
        'position_type': row.position_type or '',
        'role_fit': getattr(row, 'role_fit', '') or '',
        'bonus_amount': row.bonus_amount or 0,
        'relationship': row.relationship or '',
        'already_applied': row.already_applied or '',
        'notes': row.notes or '',
        'status': row.status or '',
        'status_updated_at': row.status_updated_at.isoformat() if row.status_updated_at else '',
        'status_updated_by': row.status_updated_by or '',
        'hire_date': row.hire_date.isoformat() if row.hire_date else '',
        'sixty_day_date': row.sixty_day_date.isoformat() if row.sixty_day_date else '',
        'payout_month': row.payout_month or '',
        'paid_date': row.paid_date.isoformat() if row.paid_date else '',
        'admin_notes': row.admin_notes or '',
        'is_archived': bool(getattr(row, 'is_archived', False) or False)
    }


def read_all_referrals():
    """Read all referrals from BigQuery."""
    try:
        query = f"""
        SELECT * FROM `{get_full_table_id()}`
        ORDER BY submitted_at DESC
        """
        results = bq_client.query(query).result()
        return [row_to_dict(row) for row in results]
    except Exception as e:
        logger.error(f"Error reading referrals: {e}")
        return []


def get_referral_by_id(referral_id):
    """Get a single referral by ID."""
    try:
        query = f"""
        SELECT * FROM `{get_full_table_id()}`
        WHERE referral_id = @referral_id
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("referral_id", "STRING", referral_id)
            ]
        )
        results = bq_client.query(query, job_config=job_config).result()
        for row in results:
            return row_to_dict(row)
        return None
    except Exception as e:
        logger.error(f"Error getting referral: {e}")
        return None


def append_referral(referral_data):
    """Insert a new referral into BigQuery using SQL INSERT (avoids streaming buffer issues)."""
    try:
        # Build parameterized query
        query = f"""
        INSERT INTO `{get_full_table_id()}` (
            referral_id, submitted_at, referrer_name, referrer_email, referrer_school,
            candidate_name, candidate_email, candidate_phone, position, position_type,
            role_fit, bonus_amount, relationship, already_applied, notes, status,
            status_updated_at, status_updated_by, hire_date, sixty_day_date,
            payout_month, paid_date, admin_notes
        ) VALUES (
            @referral_id, @submitted_at, @referrer_name, @referrer_email, @referrer_school,
            @candidate_name, @candidate_email, @candidate_phone, @position, @position_type,
            @role_fit, @bonus_amount, @relationship, @already_applied, @notes, @status,
            @status_updated_at, @status_updated_by, @hire_date, @sixty_day_date,
            @payout_month, @paid_date, @admin_notes
        )
        """

        # Parse dates
        submitted_at = datetime.fromisoformat(referral_data['submitted_at']) if referral_data.get('submitted_at') else datetime.now()
        status_updated_at = datetime.fromisoformat(referral_data['status_updated_at']) if referral_data.get('status_updated_at') else datetime.now()
        hire_date = referral_data.get('hire_date') or None
        sixty_day_date = referral_data.get('sixty_day_date') or None
        paid_date = referral_data.get('paid_date') or None

        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("referral_id", "STRING", referral_data.get('referral_id', '')),
                bigquery.ScalarQueryParameter("submitted_at", "TIMESTAMP", submitted_at),
                bigquery.ScalarQueryParameter("referrer_name", "STRING", referral_data.get('referrer_name', '')),
                bigquery.ScalarQueryParameter("referrer_email", "STRING", referral_data.get('referrer_email', '')),
                bigquery.ScalarQueryParameter("referrer_school", "STRING", referral_data.get('referrer_school', '')),
                bigquery.ScalarQueryParameter("candidate_name", "STRING", referral_data.get('candidate_name', '')),
                bigquery.ScalarQueryParameter("candidate_email", "STRING", referral_data.get('candidate_email', '')),
                bigquery.ScalarQueryParameter("candidate_phone", "STRING", referral_data.get('candidate_phone', '')),
                bigquery.ScalarQueryParameter("position", "STRING", referral_data.get('position', '')),
                bigquery.ScalarQueryParameter("position_type", "STRING", referral_data.get('position_type', '')),
                bigquery.ScalarQueryParameter("role_fit", "STRING", referral_data.get('role_fit', '')),
                bigquery.ScalarQueryParameter("bonus_amount", "INT64", int(referral_data.get('bonus_amount', 0))),
                bigquery.ScalarQueryParameter("relationship", "STRING", referral_data.get('relationship', '')),
                bigquery.ScalarQueryParameter("already_applied", "STRING", referral_data.get('already_applied', '')),
                bigquery.ScalarQueryParameter("notes", "STRING", referral_data.get('notes', '')),
                bigquery.ScalarQueryParameter("status", "STRING", referral_data.get('status', 'Submitted')),
                bigquery.ScalarQueryParameter("status_updated_at", "TIMESTAMP", status_updated_at),
                bigquery.ScalarQueryParameter("status_updated_by", "STRING", referral_data.get('status_updated_by', '')),
                bigquery.ScalarQueryParameter("hire_date", "DATE", hire_date),
                bigquery.ScalarQueryParameter("sixty_day_date", "DATE", sixty_day_date),
                bigquery.ScalarQueryParameter("payout_month", "STRING", referral_data.get('payout_month', '')),
                bigquery.ScalarQueryParameter("paid_date", "DATE", paid_date),
                bigquery.ScalarQueryParameter("admin_notes", "STRING", referral_data.get('admin_notes', '')),
            ]
        )

        bq_client.query(query, job_config=job_config).result()
        return True
    except Exception as e:
        logger.error(f"Error appending referral: {e}")
        return False


def update_referral(referral_id, updates):
    """Update a referral in BigQuery using DML."""
    try:
        # Build SET clause dynamically
        set_clauses = []
        params = [bigquery.ScalarQueryParameter("referral_id", "STRING", referral_id)]

        for field, value in updates.items():
            param_name = f"param_{field}"

            if field in ['hire_date', 'sixty_day_date', 'paid_date']:
                if value:
                    set_clauses.append(f"{field} = @{param_name}")
                    params.append(bigquery.ScalarQueryParameter(param_name, "DATE", value))
                else:
                    set_clauses.append(f"{field} = NULL")
            elif field == 'status_updated_at':
                set_clauses.append(f"{field} = @{param_name}")
                params.append(bigquery.ScalarQueryParameter(param_name, "TIMESTAMP", datetime.fromisoformat(value)))
            elif field == 'bonus_amount':
                set_clauses.append(f"{field} = @{param_name}")
                params.append(bigquery.ScalarQueryParameter(param_name, "INT64", int(value)))
            else:
                set_clauses.append(f"{field} = @{param_name}")
                params.append(bigquery.ScalarQueryParameter(param_name, "STRING", str(value)))

        if not set_clauses:
            return True

        query = f"""
        UPDATE `{get_full_table_id()}`
        SET {', '.join(set_clauses)}
        WHERE referral_id = @referral_id
        """

        job_config = bigquery.QueryJobConfig(query_parameters=params)
        bq_client.query(query, job_config=job_config).result()

        return True
    except Exception as e:
        logger.error(f"Error updating referral: {e}")
        return False


def require_admin(f):
    """Decorator to require admin authentication."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = session.get('user')
        if not user:
            return jsonify({'error': 'Authentication required'}), 401
        if user.get('email', '').lower() not in [e.lower() for e in ADMIN_USERS]:
            return jsonify({'error': 'Admin access required'}), 403
        return f(*args, **kwargs)
    return decorated_function


# ============ Public Routes ============

# Get the directory where this script is located
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__)) if '__file__' in dir() else os.getcwd()

@app.route('/')
def index():
    """Serve the main HTML page."""
    return send_file(os.path.join(SCRIPT_DIR, 'index.html'))


@app.route('/api/referrals', methods=['POST'])
def submit_referral():
    """Submit a new referral."""
    try:
        data = request.json

        # Validate required fields
        required_fields = ['referrer_name', 'referrer_email', 'referrer_school',
                          'candidate_name', 'candidate_email', 'position',
                          'position_type', 'relationship']

        for field in required_fields:
            if not data.get(field):
                return jsonify({'error': f'Missing required field: {field}'}), 400

        # Generate referral ID and timestamps
        referral_id = str(uuid.uuid4())[:8].upper()
        submitted_at = datetime.now().isoformat()

        # Determine bonus amount
        bonus_amount = 500 if data.get('position_type') == 'Lead Teacher' else 300

        # Build referral record
        referral = {
            'referral_id': referral_id,
            'submitted_at': submitted_at,
            'referrer_name': data.get('referrer_name', ''),
            'referrer_email': data.get('referrer_email', '').lower(),
            'referrer_school': data.get('referrer_school', ''),
            'candidate_name': data.get('candidate_name', ''),
            'candidate_email': data.get('candidate_email', '').lower(),
            'candidate_phone': data.get('candidate_phone', ''),
            'position': data.get('position', ''),
            'position_type': data.get('position_type', ''),
            'role_fit': data.get('role_fit', ''),
            'bonus_amount': bonus_amount,
            'relationship': data.get('relationship', ''),
            'already_applied': data.get('already_applied', 'Not yet'),
            'notes': data.get('notes', ''),
            'status': 'Submitted',
            'status_updated_at': submitted_at,
            'status_updated_by': 'System',
            'hire_date': '',
            'sixty_day_date': '',
            'payout_month': '',
            'paid_date': '',
            'admin_notes': ''
        }

        if append_referral(referral):
            # Send email notifications
            send_referral_confirmation(referral)
            send_new_referral_alert(referral)

            return jsonify({
                'success': True,
                'referral_id': referral_id,
                'bonus_amount': bonus_amount
            })
        else:
            return jsonify({'error': 'Failed to save referral'}), 500

    except Exception as e:
        logger.error(f"Error submitting referral: {e}")
        return jsonify({'error': 'Server error'}), 500


@app.route('/api/referrals/lookup', methods=['GET'])
def lookup_referrals():
    """Look up referrals by email."""
    email = request.args.get('email', '').lower().strip()

    if not email:
        return jsonify({'error': 'Email required'}), 400

    all_referrals = read_all_referrals()

    # Filter to referrals by this email
    user_referrals = [
        r for r in all_referrals
        if r.get('referrer_email', '').lower() == email
    ]

    # Calculate totals
    total_pending = sum(
        int(r.get('bonus_amount', 0) or 0)
        for r in user_referrals
        if r.get('status') in ['Submitted', 'Under Review', 'Candidate Applied', 'Interviewing', 'Hired', 'Eligible']
    )

    total_paid = sum(
        int(r.get('bonus_amount', 0) or 0)
        for r in user_referrals
        if r.get('status') == 'Paid'
    )

    # Remove admin-only fields
    for r in user_referrals:
        r.pop('admin_notes', None)

    return jsonify({
        'referrals': user_referrals,
        'total_pending': total_pending,
        'total_paid': total_paid
    })


@app.route('/api/staff/lookup', methods=['GET'])
def lookup_staff():
    """Look up staff info by email (for auto-fill)."""
    email = request.args.get('email', '').lower().strip()

    if not email:
        return jsonify({'error': 'Email required'}), 400

    # Check previous referrals to auto-fill info
    all_referrals = read_all_referrals()

    for r in all_referrals:
        if r.get('referrer_email', '').lower() == email:
            return jsonify({
                'found': True,
                'name': r.get('referrer_name', ''),
                'school': r.get('referrer_school', '')
            })

    return jsonify({'found': False})


# ============ Auth Routes ============

@app.route('/login')
def login():
    """Initiate Google OAuth."""
    if not google:
        return jsonify({'error': 'OAuth not configured'}), 500
    redirect_uri = url_for('auth_callback', _external=True)
    return google.authorize_redirect(redirect_uri)


@app.route('/auth/callback')
def auth_callback():
    """Handle OAuth callback."""
    if not google:
        return jsonify({'error': 'OAuth not configured'}), 500

    try:
        token = google.authorize_access_token()
        user_info = token.get('userinfo')

        if user_info:
            session['user'] = {
                'email': user_info.get('email'),
                'name': user_info.get('name'),
                'picture': user_info.get('picture')
            }

        # Redirect back to the app with admin view
        return redirect('/?admin=true')
    except Exception as e:
        logger.error(f"OAuth error: {e}")
        return redirect('/?error=auth_failed')


@app.route('/logout')
def logout():
    """Clear session."""
    session.clear()
    return redirect('/')


@app.route('/api/auth/status')
def auth_status():
    """Check authentication status."""
    user = session.get('user')
    if user:
        is_admin = user.get('email', '').lower() in [e.lower() for e in ADMIN_USERS]
        return jsonify({
            'authenticated': True,
            'is_admin': is_admin,
            'user': user
        })
    return jsonify({'authenticated': False, 'is_admin': False})


# ============ Admin Routes ============

@app.route('/api/admin/referrals', methods=['GET'])
@require_admin
def get_all_referrals():
    """Get all referrals (admin only)."""
    referrals = read_all_referrals()
    return jsonify({'referrals': referrals})


@app.route('/api/admin/referrals/<referral_id>', methods=['PATCH'])
@require_admin
def update_referral_status(referral_id):
    """Update a referral (admin only)."""
    try:
        data = request.json
        user = session.get('user', {})

        # Get current referral data for email notification
        current_referral = get_referral_by_id(referral_id)
        old_status = current_referral.get('status') if current_referral else None

        updates = {}
        new_status = None

        # Handle status update
        if 'status' in data:
            new_status = data['status']
            if new_status not in STATUS_VALUES:
                return jsonify({'error': 'Invalid status'}), 400

            updates['status'] = new_status
            updates['status_updated_at'] = datetime.now().isoformat()
            updates['status_updated_by'] = user.get('email', 'Unknown')

        # Handle hire date (calculate 60-day date and payout month)
        if 'hire_date' in data:
            hire_date = data['hire_date']
            updates['hire_date'] = hire_date

            if hire_date:
                try:
                    hire_dt = datetime.strptime(hire_date, '%Y-%m-%d')
                    sixty_day_dt = hire_dt + timedelta(days=60)
                    updates['sixty_day_date'] = sixty_day_dt.strftime('%Y-%m-%d')

                    # Payout month is the month after 60-day completion
                    payout_dt = sixty_day_dt.replace(day=1) + timedelta(days=32)
                    payout_dt = payout_dt.replace(day=1)
                    updates['payout_month'] = payout_dt.strftime('%B %Y')
                except ValueError:
                    pass

        # Handle position and bonus fields
        if 'position' in data:
            updates['position'] = data['position']
        if 'position_type' in data:
            updates['position_type'] = data['position_type']
        if 'bonus_amount' in data:
            updates['bonus_amount'] = int(data['bonus_amount'])

        # Handle other fields
        for field in ['paid_date', 'admin_notes']:
            if field in data:
                updates[field] = data[field]

        if update_referral(referral_id, updates):
            # Send status update email if status changed
            if new_status and old_status and new_status != old_status and current_referral:
                send_status_update(current_referral, old_status, new_status, user.get('email', 'Unknown'))

                # Send HR payout alert when status changes to Eligible
                if new_status == 'Eligible':
                    send_eligible_payout_alert(current_referral)

            return jsonify({'success': True})
        else:
            return jsonify({'error': 'Referral not found'}), 404

    except Exception as e:
        logger.error(f"Error updating referral: {e}")
        return jsonify({'error': 'Server error'}), 500


@app.route('/api/admin/referrals/<referral_id>', methods=['DELETE'])
@require_admin
def delete_referral(referral_id):
    """Permanently delete a referral (admin only)."""
    try:
        query = f"""
        DELETE FROM `{get_full_table_id()}`
        WHERE referral_id = @referral_id
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("referral_id", "STRING", referral_id)
            ]
        )
        result = bq_client.query(query, job_config=job_config).result()
        logger.info(f"Deleted referral {referral_id}")
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"Error deleting referral: {e}")
        return jsonify({'error': 'Server error'}), 500


@app.route('/api/admin/referrals/<referral_id>/archive', methods=['PATCH'])
@require_admin
def archive_referral(referral_id):
    """Archive a referral (admin only)."""
    try:
        query = f"""
        UPDATE `{get_full_table_id()}`
        SET is_archived = TRUE
        WHERE referral_id = @referral_id
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("referral_id", "STRING", referral_id)
            ]
        )
        bq_client.query(query, job_config=job_config).result()
        logger.info(f"Archived referral {referral_id}")
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"Error archiving referral: {e}")
        return jsonify({'error': 'Server error'}), 500


@app.route('/api/admin/referrals/<referral_id>/unarchive', methods=['PATCH'])
@require_admin
def unarchive_referral(referral_id):
    """Unarchive a referral (admin only)."""
    try:
        query = f"""
        UPDATE `{get_full_table_id()}`
        SET is_archived = FALSE
        WHERE referral_id = @referral_id
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("referral_id", "STRING", referral_id)
            ]
        )
        bq_client.query(query, job_config=job_config).result()
        logger.info(f"Unarchived referral {referral_id}")
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"Error unarchiving referral: {e}")
        return jsonify({'error': 'Server error'}), 500


@app.route('/api/admin/stats', methods=['GET'])
@require_admin
def get_stats():
    """Get dashboard statistics (admin only)."""
    all_referrals = read_all_referrals()
    # Exclude archived referrals from stats
    referrals = [r for r in all_referrals if not r.get('is_archived')]

    total = len(referrals)

    pending_review = len([r for r in referrals if r.get('status') in ['Submitted', 'Under Review']])

    in_progress = len([r for r in referrals if r.get('status') in ['Candidate Applied', 'Interviewing']])

    hired_pending = len([r for r in referrals if r.get('status') in ['Hired', 'Eligible']])

    paid_count = len([r for r in referrals if r.get('status') == 'Paid'])

    bonuses_paid = sum(
        int(r.get('bonus_amount', 0) or 0)
        for r in referrals
        if r.get('status') == 'Paid'
    )

    bonuses_pending = sum(
        int(r.get('bonus_amount', 0) or 0)
        for r in referrals
        if r.get('status') in ['Hired', 'Eligible']
    )

    not_hired = len([r for r in referrals if r.get('status') in ['Not Hired', 'Candidate Left before 60 Days', 'Ineligible']])

    return jsonify({
        'total': total,
        'pending_review': pending_review,
        'in_progress': in_progress,
        'hired_pending': hired_pending,
        'paid_count': paid_count,
        'bonuses_paid': bonuses_paid,
        'bonuses_pending': bonuses_pending,
        'not_hired': not_hired
    })


@app.route('/api/statuses', methods=['GET'])
def get_statuses():
    """Get list of valid status values."""
    return jsonify({'statuses': STATUS_VALUES})


# ============ Weekly Rollup ============

def send_weekly_rollup():
    """Send weekly rollup email to Talent team."""
    referrals = read_all_referrals()
    now = datetime.now()
    one_week_ago = now - timedelta(days=7)
    two_weeks_ahead = now + timedelta(days=14)

    # Helper to parse datetime and compare (handles timezone-aware datetimes)
    def is_after(dt_str, compare_to):
        if not dt_str:
            return False
        try:
            dt = datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
            # Make compare_to naive if dt is naive, or compare dates
            return dt.replace(tzinfo=None) > compare_to
        except:
            return False

    # New referrals this week
    new_referrals = [
        r for r in referrals
        if is_after(r.get('submitted_at'), one_week_ago)
    ]

    # Referrals needing review (Submitted or Under Review)
    needs_review = [r for r in referrals if r.get('status') in ['Submitted', 'Under Review']]

    # In interview process
    interviewing = [r for r in referrals if r.get('status') == 'Interviewing']

    # Hired, waiting for 60 days
    hired_waiting = [r for r in referrals if r.get('status') == 'Hired']

    # Upcoming 60-day completions (within next 2 weeks)
    upcoming_eligible = []
    for r in referrals:
        if r.get('status') == 'Hired' and r.get('sixty_day_date'):
            try:
                sixty_day = datetime.fromisoformat(r['sixty_day_date'])
                if now.date() <= sixty_day.date() <= two_weeks_ahead.date():
                    upcoming_eligible.append(r)
            except:
                pass

    # Ready for payout
    ready_for_payout = [r for r in referrals if r.get('status') == 'Eligible']

    # Build email
    subject = f"Staff Referral Program - Weekly Summary ({now.strftime('%B %d, %Y')})"

    def referral_row(r):
        return f"""
        <tr>
            <td style="padding: 8px; border-bottom: 1px solid #eee;">{r.get('candidate_name', '')}</td>
            <td style="padding: 8px; border-bottom: 1px solid #eee;">{r.get('position', '')}</td>
            <td style="padding: 8px; border-bottom: 1px solid #eee;">{r.get('referrer_name', '')}</td>
            <td style="padding: 8px; border-bottom: 1px solid #eee;">${r.get('bonus_amount', 0)}</td>
        </tr>
        """

    def build_table(title, items, color="#002f60"):
        if not items:
            return ""
        rows = "".join([referral_row(r) for r in items])
        return f"""
        <div style="margin: 20px 0;">
            <h3 style="color: {color}; margin-bottom: 10px;">{title} ({len(items)})</h3>
            <table style="width: 100%; border-collapse: collapse; background: white; border-radius: 8px;">
                <thead>
                    <tr style="background: #f8f9fa;">
                        <th style="padding: 10px; text-align: left;">Candidate</th>
                        <th style="padding: 10px; text-align: left;">Position</th>
                        <th style="padding: 10px; text-align: left;">Referrer</th>
                        <th style="padding: 10px; text-align: left;">Bonus</th>
                    </tr>
                </thead>
                <tbody>
                    {rows}
                </tbody>
            </table>
        </div>
        """

    # Stats summary
    total_pending_bonus = sum(int(r.get('bonus_amount', 0) or 0) for r in referrals if r.get('status') in ['Hired', 'Eligible'])
    total_paid_bonus = sum(int(r.get('bonus_amount', 0) or 0) for r in referrals if r.get('status') == 'Paid')

    html_body = f"""
    <div style="font-family: 'Open Sans', Arial, sans-serif; max-width: 800px; margin: 0 auto;">
        <div style="background-color: #002f60; padding: 20px; text-align: center;">
            <h1 style="color: white; margin: 0;">Staff Referral Program</h1>
            <p style="color: #f97316; margin: 5px 0 0 0;">Weekly Summary</p>
        </div>
        <div style="padding: 30px; background-color: #f8f9fa;">

            <!-- Stats Cards -->
            <div style="display: flex; gap: 15px; margin-bottom: 25px; flex-wrap: wrap;">
                <div style="background: white; padding: 15px 20px; border-radius: 8px; flex: 1; min-width: 120px; text-align: center;">
                    <div style="font-size: 2em; font-weight: bold; color: #002f60;">{len(referrals)}</div>
                    <div style="color: #666; font-size: 0.9em;">Total Referrals</div>
                </div>
                <div style="background: white; padding: 15px 20px; border-radius: 8px; flex: 1; min-width: 120px; text-align: center;">
                    <div style="font-size: 2em; font-weight: bold; color: #f97316;">{len(needs_review)}</div>
                    <div style="color: #666; font-size: 0.9em;">Need Review</div>
                </div>
                <div style="background: white; padding: 15px 20px; border-radius: 8px; flex: 1; min-width: 120px; text-align: center;">
                    <div style="font-size: 2em; font-weight: bold; color: #22c55e;">{len(ready_for_payout)}</div>
                    <div style="color: #666; font-size: 0.9em;">Ready for Payout</div>
                </div>
                <div style="background: white; padding: 15px 20px; border-radius: 8px; flex: 1; min-width: 120px; text-align: center;">
                    <div style="font-size: 2em; font-weight: bold; color: #22c55e;">${total_pending_bonus:,}</div>
                    <div style="color: #666; font-size: 0.9em;">Pending Bonuses</div>
                </div>
            </div>

            {build_table("🆕 New This Week", new_referrals, "#f97316") if new_referrals else ""}

            {build_table("⏰ Needs Review", needs_review, "#ef4444") if needs_review else ""}

            {build_table("🗓️ 60-Day Completion Coming Up", upcoming_eligible, "#8b5cf6") if upcoming_eligible else ""}

            {build_table("💰 Ready for Payout", ready_for_payout, "#22c55e") if ready_for_payout else ""}

            {build_table("🎤 Currently Interviewing", interviewing, "#3b82f6") if interviewing else ""}

            {build_table("⏳ Hired - Waiting for 60 Days", hired_waiting, "#6b7280") if hired_waiting else ""}

            <div style="margin-top: 30px; padding: 15px; background: #fff3cd; border-radius: 8px;">
                <p style="margin: 0;"><strong>Action Items:</strong></p>
                <ul style="margin: 10px 0 0 0; padding-left: 20px;">
                    {f"<li>Review {len(needs_review)} referral(s) awaiting review</li>" if needs_review else ""}
                    {f"<li>Process {len(ready_for_payout)} payout(s) totaling ${sum(int(r.get('bonus_amount', 0) or 0) for r in ready_for_payout):,}</li>" if ready_for_payout else ""}
                    {f"<li>{len(upcoming_eligible)} referral(s) reaching 60-day mark soon</li>" if upcoming_eligible else ""}
                    {"<li>All caught up! No immediate action items.</li>" if not needs_review and not ready_for_payout and not upcoming_eligible else ""}
                </ul>
            </div>

            <p style="text-align: center; margin-top: 25px;">
                <a href="https://staff-referral-program-965913991496.us-central1.run.app/?admin=true"
                   style="background: #002f60; color: white; padding: 12px 25px; text-decoration: none; border-radius: 5px; display: inline-block;">
                    Open Admin Dashboard
                </a>
            </p>
        </div>
        <div style="background-color: #002f60; padding: 15px; text-align: center;">
            <p style="color: white; margin: 0; font-size: 0.9em;">FirstLine Schools - Education For Life</p>
        </div>
    </div>
    """

    PAYROLL_MANAGER_EMAIL = 'aleibfritz@firstlineschools.org'
    PAYROLL_EMAIL = 'payroll@firstlineschools.org'
    return send_email(TALENT_TEAM_EMAIL, subject, html_body, cc_emails=[CPO_EMAIL, PAYROLL_MANAGER_EMAIL, PAYROLL_EMAIL])


@app.route('/api/weekly-rollup', methods=['POST'])
def trigger_weekly_rollup():
    """Endpoint to trigger weekly rollup email (called by Cloud Scheduler)."""
    # Verify request is from Cloud Scheduler or has valid auth
    auth_header = request.headers.get('Authorization', '')
    scheduler_header = request.headers.get('X-CloudScheduler', '')

    # Allow if from Cloud Scheduler or has a simple shared secret
    expected_secret = os.environ.get('ROLLUP_SECRET', 'weekly-rollup-secret')

    if scheduler_header or auth_header == f'Bearer {expected_secret}':
        if send_weekly_rollup():
            logger.info("Weekly rollup email sent successfully")
            return jsonify({'success': True, 'message': 'Weekly rollup sent'})
        else:
            logger.error("Failed to send weekly rollup email")
            return jsonify({'success': False, 'message': 'Failed to send email'}), 500
    else:
        return jsonify({'error': 'Unauthorized'}), 401


@app.route('/api/admin/test-rollup', methods=['POST'])
@require_admin
def test_rollup():
    """Test endpoint to preview/send weekly rollup (admin only)."""
    if send_weekly_rollup():
        return jsonify({'success': True, 'message': 'Test rollup sent'})
    else:
        return jsonify({'success': False, 'message': 'Failed to send'}), 500


# ============ Health Check ============

@app.route('/health')
def health():
    """Health check endpoint."""
    return jsonify({'status': 'healthy'})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    app.run(host='0.0.0.0', port=port, debug=debug)

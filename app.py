import os
import json
import uuid
import io
import time
import base64
from datetime import datetime
from flask import Flask, render_template, request, jsonify, session, redirect, url_for, flash, send_from_directory
from google import genai
import PyPDF2
import docx
import pyotp
import qrcode
from dotenv import load_dotenv
from authlib.integrations.flask_client import OAuth

from resume_rules import run_rule_checks
from bias_screening import redact_for_blind_screening
from ai_content_detection import detect_ai_generated

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "hr-tool-secret-key-2024")

# Initialize Gemini
client = genai.Client(api_key=os.environ.get("GOOGLE_API_KEY"))

# Optional second key — set GOOGLE_API_KEY_2 in .env to use a backup key when
# the primary key's quota is exhausted (429 RESOURCE_EXHAUSTED).
_backup_key = os.environ.get("GOOGLE_API_KEY_2")
client_backup = genai.Client(api_key=_backup_key) if _backup_key else None

# Initialize Google OAuth (for candidate sign-in)
oauth = OAuth(app)
google_oauth = oauth.register(
    name="google",
    client_id=os.environ.get("GOOGLE_CLIENT_ID"),
    client_secret=os.environ.get("GOOGLE_CLIENT_SECRET"),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

UPLOAD_FOLDER = "uploads"
DATA_FILE = "data.json"
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@hrrecruit.com")
ADMIN_TOTP_SECRET = os.environ.get("ADMIN_TOTP_SECRET")

# Fixed office-email + employee-code pairs for internal job postings login.
# Set in .env as: EMPLOYEES=arsh@company.com:EMP001,sahil@company.com:EMP002
# Each pair is "email:code" separated by commas. Both must match to log in.
def _parse_employee_credentials():
    raw = os.environ.get("EMPLOYEES", "")
    creds = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        email, code = pair.split(":", 1)
        email = email.strip().lower()
        code = code.strip()
        if email and code:
            creds[email] = code
    return creds

EMPLOYEE_CREDENTIALS = _parse_employee_credentials()

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            data = json.load(f)
    else:
        data = {}
    data.setdefault("jobs", [])
    data.setdefault("applications", [])
    data.setdefault("walkin_drives", [])
    data.setdefault("walkin_registrations", [])
    return data


def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)


@app.template_filter('days_ago')
def days_ago_filter(date_str):
    try:
        created = datetime.fromisoformat(date_str)
        delta = datetime.now() - created
        days = delta.days
        if days <= 0:
            return "Posted today"
        elif days == 1:
            return "Posted 1 day ago"
        else:
            return f"Posted {days} days ago"
    except Exception:
        return ""


def extract_text_from_file(filepath):
    ext = filepath.rsplit(".", 1)[-1].lower()
    text = ""
    try:
        if ext == "pdf":
            with open(filepath, "rb") as f:
                reader = PyPDF2.PdfReader(f)
                for page in reader.pages:
                    text += page.extract_text() or ""
        elif ext in ["docx", "doc"]:
            doc = docx.Document(filepath)
            text = "\n".join([p.text for p in doc.paragraphs])
        elif ext == "txt":
            with open(filepath, "r", errors="ignore") as f:
                text = f.read()
    except Exception as e:
        text = f"Error reading file: {str(e)}"
    return text.strip()


# ============ GEMINI RETRY HELPER ============

def gemini_generate_with_retry(prompt, retries=3, delay=5):
    """Gemini API call with automatic retry on 503/overload errors.
    Primary: gemini-2.5-flash — fallback: gemini-1.5-flash.
    Waits 5s, 10s, 15s between attempts before trying next model."""
    models_to_try = ["gemini-2.5-flash", "gemini-1.5-flash"]
    last_exception = None

    for model in models_to_try:
        for attempt in range(retries):
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=prompt
                )
                return response
            except Exception as e:
                last_exception = e
                err_str = str(e)
                is_overload = (
                    "503" in err_str
                    or "UNAVAILABLE" in err_str
                    or "overloaded" in err_str.lower()
                    or "high demand" in err_str.lower()
                )
                if is_overload and attempt < retries - 1:
                    wait = delay * (attempt + 1)  # 5s → 10s → 15s
                    print(f"[Gemini] {model} overloaded, retrying in {wait}s... (attempt {attempt + 1}/{retries})")
                    time.sleep(wait)
                    continue
                elif is_overload:
                    # Last attempt on this model failed — try next model
                    print(f"[Gemini] {model} exhausted all retries, trying fallback model...")
                    break
                else:
                    # Non-overload error (bad prompt, auth, etc.) — raise immediately
                    raise

    raise Exception(
        f"Gemini API unavailable after trying all models. Please try again in a moment. "
        f"(Last error: {last_exception})"
    )


# ============ AI FUNCTIONS ============

def analyze_resume_with_ai(resume_text, job_requirements, job_title):
    prompt = f"""You are an expert HR recruiter. Analyze this resume against the job requirements and provide a structured evaluation.

JOB TITLE: {job_title}

JOB REQUIREMENTS:
{job_requirements}

RESUME CONTENT:
{resume_text[:3000]}

Provide your analysis in this EXACT JSON format (no other text, no markdown, no backticks):
{{
  "match_score": <integer 0-100>,
  "status": "<Shortlisted|Rejected|On Hold>",
  "strengths": ["strength1", "strength2", "strength3"],
  "gaps": ["gap1", "gap2"],
  "summary": "<2-3 sentence professional summary of the candidate>",
  "recommendation": "<brief hiring recommendation>"
}}

Status rules:
- Shortlisted: score >= 70
- On Hold: score 50-69
- Rejected: score < 50"""

    response = gemini_generate_with_retry(prompt)
    response_text = response.text.strip()

    # Clean markdown if present
    if "```json" in response_text:
        response_text = response_text.split("```json")[1].split("```")[0].strip()
    elif "```" in response_text:
        response_text = response_text.split("```")[1].split("```")[0].strip()

    return json.loads(response_text)


def generate_ats_review(resume_text, job_description=""):
    """Resume quality / ATS-friendliness review — separate from the job-match scoring.
    Merges an AI-written review with deterministic rule-based checks (resume_rules.py)."""
    jd_block = f"Target job description: {job_description}\n" if job_description else ""

    prompt = f"""You are reviewing a candidate's resume for quality and ATS-friendliness.
Resume text: {resume_text[:3000]}
{jd_block}Return ONLY valid JSON in this exact shape, no other text:
{{
  "ats_score": <0-100 integer>,
  "strengths": ["short phrase", ...],
  "issues": [
    {{"area": "achievements" | "clarity" | "structure" | "keywords",
     "problem": "what's wrong, one sentence",
     "suggestion": "specific fix, one sentence"}}
  ],
  "summary": "one encouraging sentence, max 20 words"
}}
Be specific and actionable. Do not be harsh — candidate will read this directly."""

    response = gemini_generate_with_retry(prompt)
    response_text = response.text.strip()

    if "```json" in response_text:
        response_text = response_text.split("```json")[1].split("```")[0].strip()
    elif "```" in response_text:
        response_text = response_text.split("```")[1].split("```")[0].strip()

    ats_result = json.loads(response_text)

    # merge in the deterministic, non-AI checks
    rule_result = run_rule_checks(resume_text, job_description)
    ats_result["rule_flags"] = rule_result["rule_flags"]
    ats_result["missing_keywords"] = rule_result.get("missing_keywords", [])

    return ats_result


def generate_interview_questions(resume_text, job_requirements, job_title, gaps, strengths):
    gaps_text = "\n".join(f"- {g}" for g in gaps) if gaps else "None identified"
    strengths_text = "\n".join(f"- {s}" for s in strengths) if strengths else "None identified"

    prompt = f"""You are an expert technical interviewer preparing questions for an upcoming candidate interview.

JOB TITLE: {job_title}

JOB REQUIREMENTS:
{job_requirements}

CANDIDATE'S IDENTIFIED STRENGTHS (from automated resume screening):
{strengths_text}

CANDIDATE'S IDENTIFIED GAPS (from automated resume screening):
{gaps_text}

RESUME EXCERPT:
{resume_text[:2000]}

Generate exactly 5-6 targeted interview questions for THIS SPECIFIC candidate. Mix the question types:
- 2-3 questions that probe the identified gaps, to find out if they are real concerns or not
- 1-2 questions that verify the claimed strengths are genuine and not just resume buzzwords
- 1-2 role-specific scenario or behavioral questions based on the job requirements

Provide your answer in this EXACT JSON format (no other text, no markdown, no backticks):
{{
  "questions": [
    {{"question": "<question text>", "purpose": "<one short phrase: what this question checks for>"}},
    {{"question": "<question text>", "purpose": "<one short phrase: what this question checks for>"}}
  ]
}}"""

    response = gemini_generate_with_retry(prompt)
    response_text = response.text.strip()

    if "```json" in response_text:
        response_text = response_text.split("```json")[1].split("```")[0].strip()
    elif "```" in response_text:
        response_text = response_text.split("```")[1].split("```")[0].strip()

    return json.loads(response_text)["questions"]


def generate_assessment_questions(resume_text, job_requirements, job_title):
    """Generate 15 MCQ assessment questions for the candidate. MCQ-only, fully auto-scored."""
    prompt = f"""You are an expert technical recruiter. Generate a skill-based MCQ assessment for a candidate who applied for the role below.

JOB TITLE: {job_title}

JOB REQUIREMENTS:
{job_requirements}

CANDIDATE RESUME EXCERPT:
{resume_text[:2000]}

Create exactly 15 multiple-choice questions (MCQ) that test the key skills required for this role.
Questions should be scenario-based or concept-based, not trivial, and each must have exactly one correct option.

Return ONLY this exact JSON (no markdown, no backticks):
{{
  "mcq": [
    {{
      "q": "<question text>",
      "options": ["A. ...", "B. ...", "C. ...", "D. ..."],
      "answer": "A"
    }}
  ]
}}"""

    response = gemini_generate_with_retry(prompt)
    response_text = response.text.strip()
    if "```json" in response_text:
        response_text = response_text.split("```json")[1].split("```")[0].strip()
    elif "```" in response_text:
        response_text = response_text.split("```")[1].split("```")[0].strip()
    return json.loads(response_text)


def score_assessment(mcq_questions, mcq_answers, job_title):
    """Score the completed MCQ assessment. Fully automated, no AI evaluation needed."""
    mcq_score = 0
    mcq_total = len(mcq_questions)
    mcq_results = []
    for i, q in enumerate(mcq_questions):
        correct = q["answer"]
        given = mcq_answers.get(str(i), "")
        is_correct = given.strip().upper().startswith(correct.upper())
        if is_correct:
            mcq_score += 1
        mcq_results.append({
            "question": q["q"],
            "options": q["options"],
            "correct_answer": correct,
            "given_answer": given,
            "is_correct": is_correct,
        })

    composite = round((mcq_score / mcq_total * 100)) if mcq_total else 0

    return {
        "mcq_score": mcq_score,
        "mcq_total": mcq_total,
        "mcq_results": mcq_results,
        "composite_score": composite,
        "completed_at": datetime.now().isoformat(),
    }


# ============ ROUTES ============

@app.route("/")
def index():
    data = load_data()
    active_jobs_count = sum(1 for j in data["jobs"] if j.get("active", True))
    return render_template("index.html", active_jobs_count=active_jobs_count)

@app.route("/api/active-jobs-count")
def api_active_jobs_count():
    data = load_data()
    active_jobs_count = sum(1 for j in data["jobs"] if j.get("active", True))
    return jsonify({"active_jobs_count": active_jobs_count})
# ---- CANDIDATE PORTAL ----

@app.route("/candidate")
def candidate():
    data = load_data()
    active_jobs = [j for j in data["jobs"] if j.get("active", True)]
    applicant_counts = {}
    for a in data["applications"]:
        applicant_counts[a["job_id"]] = applicant_counts.get(a["job_id"], 0) + 1
    return render_template("candidate.html", jobs=active_jobs, applicant_counts=applicant_counts)


# ---- CANDIDATE GOOGLE SIGN-IN ----

@app.route("/auth/google/login")
def google_login():
    redirect_uri = url_for("google_callback", _external=True)
    return google_oauth.authorize_redirect(redirect_uri)


@app.route("/auth/google/callback")
def google_callback():
    try:
        token = google_oauth.authorize_access_token()
        user_info = token.get("userinfo")
        if not user_info or not user_info.get("email"):
            flash("Google sign-in failed. Please try again.", "error")
            return redirect(url_for("candidate"))
        session["candidate_email"] = user_info["email"]
        session["candidate_name"] = user_info.get("name", user_info["email"])
        session["candidate_picture"] = user_info.get("picture", "")
        next_url = session.pop("next_url", None)
        if next_url:
            return redirect(next_url)
        return redirect(url_for("candidate_dashboard"))
    except Exception as e:
        flash(f"Google sign-in failed: {str(e)}", "error")
        return redirect(url_for("candidate"))


@app.route("/candidate/logout")
def candidate_logout():
    session.pop("candidate_email", None)
    session.pop("candidate_name", None)
    session.pop("candidate_picture", None)
    return redirect(url_for("candidate"))


@app.route("/candidate/dashboard")
def candidate_dashboard():
    if not session.get("candidate_email"):
        return redirect(url_for("google_login"))
    data = load_data()
    email = session["candidate_email"].strip().lower()
    my_applications = [a for a in data["applications"] if a["email"].strip().lower() == email]
    my_applications = sorted(my_applications, key=lambda a: a["applied_at"], reverse=True)
    return render_template(
        "candidate_dashboard.html",
        applications=my_applications,
        candidate_name=session.get("candidate_name"),
        candidate_email=session.get("candidate_email"),
    )


@app.route("/apply/<job_id>", methods=["GET", "POST"])
def apply(job_id):
    if not session.get("candidate_email"):
        session["next_url"] = url_for("apply", job_id=job_id)
        flash("Please sign in with Google to apply for this job.", "error")
        return redirect(url_for("google_login"))

    data = load_data()
    job = next((j for j in data["jobs"] if j["id"] == job_id), None)
    if not job:
        flash("Job not found.", "error")
        return redirect(url_for("candidate"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()
        phone = request.form.get("phone", "").strip()
        resume_file = request.files.get("resume")

        if not all([name, email, resume_file]):
            flash("Please fill all required fields and upload your resume.", "error")
            return render_template("apply.html", job=job)

        already_applied = any(
            a["job_id"] == job_id and a["email"].strip().lower() == email.lower()
            for a in data["applications"]
        )
        if already_applied:
            flash("You have already applied for this job with this email address.", "error")
            return render_template("apply.html", job=job)

        ext = resume_file.filename.rsplit(".", 1)[-1].lower()
        filename = f"{uuid.uuid4()}.{ext}"
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        resume_file.save(filepath)

        resume_text = extract_text_from_file(filepath)
        if len(resume_text) < 50:
            resume_text = f"Resume uploaded by {name}. File: {resume_file.filename}"

        # --- AI-generated resume detection (runs on raw text, before redaction) ---
        try:
            ai_detection = detect_ai_generated(resume_text)
        except Exception:
            ai_detection = None

        # --- Bias-free / blind screening: redact identity signals before scoring ---
        try:
            blind = redact_for_blind_screening(resume_text)
        except Exception:
            blind = {"redacted_text": resume_text, "redaction_log": {}, "redacted_count": 0}

        try:
            # Score against the REDACTED text so name/gender/college-tier/age/photo
            # can't influence the AI match score — bias-free by default, always on.
            ai_result = analyze_resume_with_ai(blind["redacted_text"], job["requirements"], job["title"])
        except Exception as e:
            ai_result = {
                "match_score": 0,
                "status": "On Hold",
                "strengths": [],
                "gaps": ["Could not analyze resume automatically"],
                "summary": "Manual review required.",
                "recommendation": f"Error: {str(e)}",
            }

        # --- Resume quality / ATS-friendliness review (separate from job-match score) ---
        try:
            ats_review = generate_ats_review(resume_text, job["requirements"])
        except Exception:
            ats_review = None

        application = {
            "id": str(uuid.uuid4()),
            "job_id": job_id,
            "job_title": job["title"],
            # Snapshot of requirements at apply-time. Job posting may get edited or
            # deleted later — this keeps assessment/interview-question generation
            # working even if data["jobs"] no longer has a matching entry.
            "job_requirements": job["requirements"],
            "name": name,
            "email": email,
            "phone": phone,
            "resume_file": filename,
            "resume_text": resume_text[:500],
            "applied_at": datetime.now().isoformat(),
            "ai_analysis": ai_result,
            "ats_review": ats_review,
            "ai_detection": ai_detection,
            "blind_screening": {
                "redaction_log": blind["redaction_log"],
                "redacted_count": blind["redacted_count"],
            },
        }
        data["applications"].append(application)
        save_data(data)

        return render_template("applied.html", application=application, job=job)

    return render_template("apply.html", job=job)


# ---- COMPANY EMPLOYEES LOGIN (internal job postings) ----

@app.route("/company-login", methods=["GET", "POST"])
def company_login():
    if request.method == "POST":
        office_email = request.form.get("office_email", "").strip()
        employee_code = request.form.get("employee_code", "").strip()

        if not office_email or not employee_code:
            flash("Please enter both office email and employee code.", "error")
            return render_template("company_login.html")

        if not EMPLOYEE_CREDENTIALS:
            flash("Employee accounts are not configured on the server.", "error")
            return render_template("company_login.html")

        expected_code = EMPLOYEE_CREDENTIALS.get(office_email.lower())
        if not expected_code or expected_code != employee_code:
            flash("Office email or employee code is incorrect.", "error")
            return render_template("company_login.html")

        session["employee_email"] = office_email
        session["employee_verified"] = True
        return redirect(url_for("internal_jobs"))

    return render_template("company_login.html")


@app.route("/company-logout")
def company_logout():
    session.pop("employee_email", None)
    session.pop("employee_verified", None)
    return redirect(url_for("company_login"))


def employee_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("employee_verified"):
            return redirect(url_for("company_login"))
        return f(*args, **kwargs)
    return decorated


@app.route("/internal-jobs")
@employee_required
def internal_jobs():
    data = load_data()
    active_jobs = [j for j in data["jobs"] if j.get("active", True)]
    return render_template(
        "internal_jobs.html",
        jobs=active_jobs,
        employee_email=session.get("employee_email"),
    )


# ---- ADMIN PORTAL ----

@app.route("/admin")
def admin_dashboard():
    session.pop("admin", None)
    session.pop("admin_email", None)
    session.pop("admin_pw_ok", None)
    session.pop("admin_email_pending", None)
    return redirect(url_for("admin_login"))


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        if email.lower() == ADMIN_EMAIL.lower() and password == ADMIN_PASSWORD:
            session["admin_pw_ok"] = True
            session["admin_email_pending"] = email
            return redirect(url_for("admin_totp"))
        flash("Invalid email or password.", "error")
    return render_template("admin_login.html")


@app.route("/admin/totp", methods=["GET", "POST"])
def admin_totp():
    if not session.get("admin_pw_ok"):
        return redirect(url_for("admin_login"))

    if not ADMIN_TOTP_SECRET:
        flash("ADMIN_TOTP_SECRET not configured on server.", "error")
        return redirect(url_for("admin_login"))

    if request.method == "POST":
        code = request.form.get("code", "").strip()
        totp = pyotp.TOTP(ADMIN_TOTP_SECRET)
        if totp.verify(code, valid_window=1):
            session["admin"] = True
            session["admin_email"] = session.pop("admin_email_pending", ADMIN_EMAIL)
            session.pop("admin_pw_ok", None)
            return redirect(url_for("admin_home"))
        flash("Invalid authenticator code. Try again.", "error")

    return render_template("admin_totp.html")


@app.route("/admin/totp/qrcode")
def admin_totp_qrcode():
    if not session.get("admin_pw_ok"):
        return redirect(url_for("admin_login"))
    if not ADMIN_TOTP_SECRET:
        return "ADMIN_TOTP_SECRET not configured.", 500

    totp = pyotp.TOTP(ADMIN_TOTP_SECRET)
    uri = totp.provisioning_uri(name=ADMIN_EMAIL, issuer_name="HR Recruiter Tool")

    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.getvalue(), 200, {"Content-Type": "image/png"}


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    session.pop("admin_email", None)
    session.pop("admin_pw_ok", None)
    session.pop("admin_email_pending", None)
    return redirect(url_for("admin_login"))


def admin_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return decorated


@app.route("/admin/resume/<filename>")
@admin_required
def serve_resume(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)


@app.route("/admin/home")
@admin_required
def admin_home():
    data = load_data()
    total_apps = len(data["applications"])
    shortlisted = sum(1 for a in data["applications"] if a.get("ai_analysis", {}).get("status") == "Shortlisted")
    on_hold = sum(1 for a in data["applications"] if a.get("ai_analysis", {}).get("status") == "On Hold")
    rejected = sum(1 for a in data["applications"] if a.get("ai_analysis", {}).get("status") == "Rejected")
    stats = {
        "total": total_apps,
        "shortlisted": shortlisted,
        "on_hold": on_hold,
        "rejected": rejected,
        "active_jobs": sum(1 for j in data["jobs"] if j.get("active", True)),
        "active_drives": sum(1 for d in data["walkin_drives"] if d.get("active", True)),
    }

    # Har job ke liye applicant count calculate karo
    applicant_counts = {}
    for a in data["applications"]:
        applicant_counts[a["job_id"]] = applicant_counts.get(a["job_id"], 0) + 1

    # Har job object mein applicant_count field inject karo
    jobs = data["jobs"]
    for job in jobs:
        job["applicant_count"] = applicant_counts.get(job["id"], 0)

    # Har walk-in drive ke liye registration count calculate karo
    reg_counts = {}
    for r in data["walkin_registrations"]:
        reg_counts[r["drive_id"]] = reg_counts.get(r["drive_id"], 0) + 1

    walkin_drives = data["walkin_drives"]
    for drive in walkin_drives:
        drive["registration_count"] = reg_counts.get(drive["id"], 0)

    return render_template("admin_dashboard.html", stats=stats, jobs=jobs, walkin_drives=walkin_drives)


@app.route("/admin/jobs/new", methods=["GET", "POST"])
@admin_required
def new_job():
    if request.method == "POST":
        data = load_data()
        job = {
            "id": str(uuid.uuid4()),
            "title": request.form.get("title", "").strip(),
            "department": request.form.get("department", "").strip(),
            "location": request.form.get("location", "").strip(),
            "job_type": request.form.get("job_type", "Onsite").strip(),
            "role": request.form.get("role", "").strip(),
            "role_category": request.form.get("role_category", "").strip(),
            "industry_type": request.form.get("industry_type", "").strip(),
            "experience_level": request.form.get("experience_level", "Fresher").strip(),
            "education_ug": request.form.get("education_ug", "").strip(),
            "education_pg": request.form.get("education_pg", "").strip(),
            "key_skills": [s.strip() for s in request.form.get("key_skills", "").split(",") if s.strip()],
            "requirements": request.form.get("requirements", "").strip(),
            "description": request.form.get("description", "").strip(),
            "active": True,
            "created_at": datetime.now().isoformat(),
        }
        if not job["title"] or not job["requirements"]:
            flash("Title and requirements are mandatory.", "error")
            return render_template("new_job.html")
        data["jobs"].append(job)
        save_data(data)
        flash(f"Job '{job['title']}' posted successfully!", "success")
        return redirect(url_for("admin_home"))
    return render_template("new_job.html")


# ============ WALK-IN DRIVE — ADMIN ============

@app.route("/admin/walkin/new", methods=["GET", "POST"])
@admin_required
def new_walkin():
    if request.method == "POST":
        data = load_data()
        drive = {
            "id": str(uuid.uuid4()),
            "title": request.form.get("title", "").strip(),
            "department": request.form.get("department", "").strip(),
            "location": request.form.get("location", "").strip(),
            "venue": request.form.get("venue", "").strip(),
            "drive_date": request.form.get("drive_date", "").strip(),
            "drive_time": request.form.get("drive_time", "").strip(),
            "description": request.form.get("description", "").strip(),
            "active": True,
            "created_at": datetime.now().isoformat(),
        }
        if not drive["title"] or not drive["drive_date"]:
            flash("Title and Date are mandatory.", "error")
            return render_template("new_walkin.html")
        data["walkin_drives"].append(drive)
        save_data(data)
        flash(f"Walk-in Drive '{drive['title']}' posted successfully!", "success")
        return redirect(url_for("admin_home"))
    return render_template("new_walkin.html")


@app.route("/admin/walkin/<drive_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_walkin(drive_id):
    data = load_data()
    drive = next((d for d in data["walkin_drives"] if d["id"] == drive_id), None)
    if not drive:
        flash("Walk-in Drive not found.", "error")
        return redirect(url_for("admin_home"))

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        drive_date = request.form.get("drive_date", "").strip()
        if not title or not drive_date:
            flash("Title and Date are mandatory.", "error")
            return render_template("edit_walkin.html", drive=drive)

        drive["title"] = title
        drive["department"] = request.form.get("department", "").strip()
        drive["location"] = request.form.get("location", "").strip()
        drive["venue"] = request.form.get("venue", "").strip()
        drive["drive_date"] = drive_date
        drive["drive_time"] = request.form.get("drive_time", "").strip()
        drive["description"] = request.form.get("description", "").strip()

        save_data(data)
        flash(f"Walk-in Drive '{drive['title']}' updated successfully!", "success")
        return redirect(url_for("admin_home"))

    return render_template("edit_walkin.html", drive=drive)


@app.route("/admin/walkin/<drive_id>/toggle", methods=["POST"])
@admin_required
def toggle_walkin(drive_id):
    data = load_data()
    for d in data["walkin_drives"]:
        if d["id"] == drive_id:
            d["active"] = not d.get("active", True)
            break
    save_data(data)
    return redirect(url_for("admin_home"))


@app.route("/admin/walkin/<drive_id>/delete", methods=["POST"])
@admin_required
def delete_walkin(drive_id):
    data = load_data()
    data["walkin_drives"] = [d for d in data["walkin_drives"] if d["id"] != drive_id]
    data["walkin_registrations"] = [r for r in data["walkin_registrations"] if r["drive_id"] != drive_id]
    save_data(data)
    flash("Walk-in Drive deleted.", "success")
    return redirect(url_for("admin_home"))


@app.route("/admin/walkin/<drive_id>/registrations")
@admin_required
def view_walkin_registrations(drive_id):
    data = load_data()
    drive = next((d for d in data["walkin_drives"] if d["id"] == drive_id), None)
    if not drive:
        flash("Walk-in Drive not found.", "error")
        return redirect(url_for("admin_home"))
    regs = [r for r in data["walkin_registrations"] if r["drive_id"] == drive_id]
    regs = sorted(regs, key=lambda r: r["registered_at"], reverse=True)
    return render_template("admin_walkin_registrations.html", drive=drive, registrations=regs)


@app.route("/admin/walkin/<drive_id>/registrations/delete-all", methods=["POST"])
@admin_required
def delete_all_walkin_registrations(drive_id):
    data = load_data()
    data["walkin_registrations"] = [r for r in data["walkin_registrations"] if r["drive_id"] != drive_id]
    save_data(data)
    flash("All registrations cleared.", "success")
    return redirect(url_for("view_walkin_registrations", drive_id=drive_id))


@app.route("/admin/walkin/registrations/<reg_id>/delete", methods=["POST"])
@admin_required
def delete_walkin_registration(reg_id):
    data = load_data()
    reg = next((r for r in data["walkin_registrations"] if r["id"] == reg_id), None)
    drive_id = reg["drive_id"] if reg else None
    data["walkin_registrations"] = [r for r in data["walkin_registrations"] if r["id"] != reg_id]
    save_data(data)
    flash("Registration deleted.", "success")
    if drive_id:
        return redirect(url_for("view_walkin_registrations", drive_id=drive_id))
    return redirect(url_for("admin_home"))


# ============ WALK-IN DRIVE — CANDIDATE FACING ============

@app.route("/walk-in")
def walk_in():
    data = load_data()
    active_drives = [d for d in data["walkin_drives"] if d.get("active", True)]
    reg_counts = {}
    for r in data["walkin_registrations"]:
        reg_counts[r["drive_id"]] = reg_counts.get(r["drive_id"], 0) + 1
    return render_template("walk_in.html", drives=active_drives, reg_counts=reg_counts)


@app.route("/walk-in/<drive_id>/register", methods=["GET", "POST"])
def register_walkin(drive_id):
    data = load_data()
    drive = next((d for d in data["walkin_drives"] if d["id"] == drive_id), None)
    if not drive:
        flash("Walk-in Drive not found.", "error")
        return redirect(url_for("walk_in"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()
        phone = request.form.get("phone", "").strip()
        slot = request.form.get("slot", "").strip()

        if not name or not email or not phone:
            flash("Please fill all required fields.", "error")
            return render_template("walkin_register.html", drive=drive)

        already = any(
            r["drive_id"] == drive_id and r["email"].strip().lower() == email.lower()
            for r in data["walkin_registrations"]
        )
        if already:
            flash("You have already registered for this drive with this email.", "error")
            return render_template("walkin_register.html", drive=drive)

        registration = {
            "id": str(uuid.uuid4()),
            "drive_id": drive_id,
            "drive_title": drive["title"],
            "name": name,
            "email": email,
            "phone": phone,
            "slot": slot,
            "registered_at": datetime.now().isoformat(),
        }
        data["walkin_registrations"].append(registration)
        save_data(data)
        return render_template("walkin_registered.html", registration=registration, drive=drive)

    return render_template("walkin_register.html", drive=drive)


@app.route("/admin/jobs/<job_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_job(job_id):
    data = load_data()
    job = next((j for j in data["jobs"] if j["id"] == job_id), None)
    if not job:
        flash("Job not found.", "error")
        return redirect(url_for("admin_home"))

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        requirements = request.form.get("requirements", "").strip()

        if not title or not requirements:
            flash("Title and requirements are mandatory.", "error")
            return render_template("edit_job.html", job=job)

        job["title"] = title
        job["location"] = request.form.get("location", "").strip()
        job["job_type"] = request.form.get("job_type", job.get("job_type", "Onsite")).strip()
        job["role_category"] = request.form.get("role_category", "").strip()
        job["industry_type"] = request.form.get("industry_type", "").strip()
        job["employment_type"] = request.form.get("employment_type", "").strip()
        job["education_ug"] = request.form.get("education_ug", "").strip()
        job["education_pg"] = request.form.get("education_pg", "").strip()
        job["description"] = request.form.get("description", "").strip()
        job["requirements"] = requirements

        save_data(data)
        flash(f"Job '{job['title']}' updated successfully!", "success")
        return redirect(url_for("admin_home"))

    return render_template("edit_job.html", job=job)


@app.route("/admin/jobs/<job_id>/toggle", methods=["POST"])
@admin_required
def toggle_job(job_id):
    data = load_data()
    for job in data["jobs"]:
        if job["id"] == job_id:
            job["active"] = not job.get("active", True)
            break
    save_data(data)
    return redirect(url_for("admin_home"))


@app.route("/admin/jobs/<job_id>/delete", methods=["POST"])
@admin_required
def delete_job(job_id):
    data = load_data()
    data["jobs"] = [j for j in data["jobs"] if j["id"] != job_id]
    save_data(data)
    flash("Job deleted.", "success")
    return redirect(url_for("admin_home"))


# NOTE: /delete-all MUST be before /<app_id>/delete — Flask matches top-down
# warna "delete-all" string ko app_id samajh leta hai
@app.route("/admin/applications/delete-all", methods=["POST"])
@admin_required
def delete_all_applications():
    data = load_data()

    job_id = request.form.get("job_id", "").strip()
    status = request.form.get("status", "").strip()
    search = request.form.get("search", "").strip().lower()

    to_delete = data["applications"]
    if job_id:
        to_delete = [a for a in to_delete if a["job_id"] == job_id]
    if status:
        to_delete = [a for a in to_delete if a.get("ai_analysis", {}).get("status") == status]
    if search:
        to_delete = [a for a in to_delete if
                     search in a.get("name", "").lower() or
                     search in a.get("email", "").lower()]

    delete_ids = {a["id"] for a in to_delete}

    for a in to_delete:
        filepath = os.path.join(UPLOAD_FOLDER, a.get("resume_file", ""))
        if os.path.exists(filepath):
            try:
                os.remove(filepath)
            except OSError:
                pass

    data["applications"] = [a for a in data["applications"] if a["id"] not in delete_ids]
    save_data(data)

    count = len(delete_ids)
    flash(f"Deleted {count} application{'s' if count != 1 else ''} successfully.", "success")

    params = {}
    if job_id: params["job_id"] = job_id
    if status: params["status"] = status
    if search: params["search"] = search
    return redirect(url_for("admin_applications", **params))


@app.route("/admin/applications/<app_id>/delete", methods=["POST"])
@admin_required
def delete_application(app_id):
    data = load_data()
    app_data = next((a for a in data["applications"] if a["id"] == app_id), None)
    if app_data:
        filepath = os.path.join(UPLOAD_FOLDER, app_data.get("resume_file", ""))
        if os.path.exists(filepath):
            os.remove(filepath)
    data["applications"] = [a for a in data["applications"] if a["id"] != app_id]
    save_data(data)
    flash("Application deleted.", "success")
    return redirect(url_for("admin_applications"))


@app.route("/admin/applications")
@admin_required
def admin_applications():
    data = load_data()
    job_id = request.args.get("job_id")
    status_filter = request.args.get("status")
    search_query = request.args.get("search", "").strip()
    apps = data["applications"]
    if job_id:
        apps = [a for a in apps if a["job_id"] == job_id]
    if status_filter:
        apps = [a for a in apps if a.get("ai_analysis", {}).get("status") == status_filter]
    if search_query:
        q = search_query.lower()
        apps = [a for a in apps if q in a.get("name", "").lower() or q in a.get("email", "").lower()]
    apps = sorted(apps, key=lambda a: a.get("ai_analysis", {}).get("match_score", 0), reverse=True)
    jobs = {j["id"]: j["title"] for j in data["jobs"]}
    return render_template(
        "admin_applications.html",
        applications=apps,
        jobs=jobs,
        all_jobs=data["jobs"],
        job_id=job_id,
        status_filter=status_filter,
        search_query=search_query,
    )


@app.route("/admin/applications/<app_id>")
@admin_required
def view_application(app_id):
    data = load_data()
    app_data = next((a for a in data["applications"] if a["id"] == app_id), None)
    if not app_data:
        flash("Application not found.", "error")
        return redirect(url_for("admin_applications"))
    job = next((j for j in data["jobs"] if j["id"] == app_data["job_id"]), {})
    return render_template("view_application.html", application=app_data, job=job)


@app.route("/admin/applications/<app_id>/reanalyze", methods=["POST"])
@admin_required
def reanalyze(app_id):
    data = load_data()
    app_data = next((a for a in data["applications"] if a["id"] == app_id), None)
    if not app_data:
        return jsonify({"error": "Not found"}), 404
    job = next((j for j in data["jobs"] if j["id"] == app_data["job_id"]), None)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    filepath = os.path.join(UPLOAD_FOLDER, app_data["resume_file"])
    resume_text = extract_text_from_file(filepath)
    try:
        blind = redact_for_blind_screening(resume_text)
        ai_result = analyze_resume_with_ai(blind["redacted_text"], job["requirements"], job["title"])
        app_data["ai_analysis"] = ai_result
        app_data["blind_screening"] = {
            "redaction_log": blind["redaction_log"],
            "redacted_count": blind["redacted_count"],
        }
        try:
            app_data["ai_detection"] = detect_ai_generated(resume_text)
        except Exception:
            pass
        try:
            app_data["ats_review"] = generate_ats_review(resume_text, job["requirements"])
        except Exception:
            pass
        save_data(data)
        flash("Resume re-analyzed successfully!", "success")
    except Exception as e:
        flash(f"Re-analysis failed: {str(e)}", "error")
    return redirect(url_for("view_application", app_id=app_id))


@app.route("/admin/applications/<app_id>/generate-questions", methods=["POST"])
@admin_required
def generate_questions(app_id):
    data = load_data()
    app_data = next((a for a in data["applications"] if a["id"] == app_id), None)
    if not app_data:
        flash("Application not found.", "error")
        return redirect(url_for("admin_applications"))
    job = next((j for j in data["jobs"] if j["id"] == app_data["job_id"]), None)
    job_title = job["title"] if job else app_data.get("job_title", "Unknown Role")
    job_requirements = job["requirements"] if job else app_data.get("job_requirements", "")

    ai = app_data.get("ai_analysis", {})
    try:
        questions = generate_interview_questions(
            app_data.get("resume_text", ""),
            job_requirements,
            job_title,
            ai.get("gaps", []),
            ai.get("strengths", []),
        )
        app_data["interview_questions"] = questions
        save_data(data)
        flash("Interview questions generated!", "success")
    except Exception as e:
        flash(f"Question generation failed: {str(e)}", "error")
    return redirect(url_for("view_application", app_id=app_id))


# ---- CANDIDATE ASSESSMENT ----

@app.route("/assessment/<app_id>")
def assessment_page(app_id):
    data = load_data()
    app_data = next((a for a in data["applications"] if a["id"] == app_id), None)
    if not app_data:
        flash("Application not found.", "error")
        return redirect(url_for("candidate"))
    if app_data.get("assessment_result"):
        flash("You have already submitted your assessment.", "success")
        return redirect(url_for("candidate_dashboard"))
    if not app_data.get("assessment_questions"):
        job = next((j for j in data["jobs"] if j["id"] == app_data["job_id"]), None)
        # Job posting might have been edited/deleted later — fall back to the
        # title/requirements snapshot saved on the application itself, instead
        # of blocking assessment generation entirely.
        job_title = job["title"] if job else app_data.get("job_title", "Unknown Role")
        job_requirements = job["requirements"] if job else app_data.get("job_requirements", "")
        try:
            questions = generate_assessment_questions(
                app_data.get("resume_text", ""),
                job_requirements,
                job_title,
            )
            app_data["assessment_questions"] = questions
            save_data(data)
        except Exception as e:
            flash(f"Could not generate assessment: {str(e)}", "error")
            return redirect(url_for("candidate_dashboard"))
    return render_template("assessment.html", application=app_data)


@app.route("/assessment/<app_id>/submit", methods=["POST"])
def submit_assessment(app_id):
    data = load_data()
    app_data = next((a for a in data["applications"] if a["id"] == app_id), None)
    if not app_data:
        flash("Application not found.", "error")
        return redirect(url_for("candidate"))
    if app_data.get("assessment_result"):
        flash("Assessment already submitted.", "success")
        return redirect(url_for("candidate_dashboard"))
    questions = app_data.get("assessment_questions", {})
    mcq_questions = questions.get("mcq", [])
    mcq_answers = {str(i): request.form.get(f"mcq_{i}", "") for i in range(len(mcq_questions))}
    try:
        result = score_assessment(mcq_questions, mcq_answers, app_data.get("job_title", ""))
        app_data["assessment_result"] = result
        save_data(data)
        flash("Assessment submitted successfully! Your results are being reviewed.", "success")
    except Exception as e:
        flash(f"Error scoring assessment: {str(e)}", "error")
    return redirect(url_for("candidate_dashboard"))


# ---- ADMIN ASSESSMENT ROUTES ----

@app.route("/admin/applications/send-assessment-all", methods=["POST"])
@admin_required
def send_assessment_all():
    """Ek click mein saare visible candidates ko assessment bhejo.
    Jo already sent hain unhe skip karo — sirf naye wale ko generate karo.
    Status (Shortlisted/On Hold/Rejected) se koi farak nahi padta — sabko
    bheja ja sakta hai. Job posting delete/edit ho jaye tab bhi application
    ke saved job_title/job_requirements se generate ho jata hai, block nahi hota."""
    data = load_data()

    # Current filters padhho — sirf visible apps pe kaam karo
    job_id = request.form.get("job_id", "").strip()
    status = request.form.get("status", "").strip()
    search = request.form.get("search", "").strip().lower()

    apps = data["applications"]
    if job_id:
        apps = [a for a in apps if a["job_id"] == job_id]
    if status:
        apps = [a for a in apps if a.get("ai_analysis", {}).get("status") == status]
    if search:
        apps = [a for a in apps if
                search in a.get("name", "").lower() or
                search in a.get("email", "").lower()]

    sent_count = 0
    skipped_count = 0
    failed_count = 0
    failed_reasons = []

    for app_data in apps:
        # Already questions hain ya result aa gaya — skip
        if app_data.get("assessment_questions") or app_data.get("assessment_result"):
            skipped_count += 1
            continue

        job = next((j for j in data["jobs"] if j["id"] == app_data["job_id"]), None)
        # Job posting na mile (delete/edit ho gaya) to bhi block nahi karna —
        # application ke apne saved title/requirements use karo.
        job_title = job["title"] if job else app_data.get("job_title", "Unknown Role")
        job_requirements = job["requirements"] if job else app_data.get("job_requirements", "")

        try:
            questions = generate_assessment_questions(
                app_data.get("resume_text", ""),
                job_requirements,
                job_title,
            )
            app_data["assessment_questions"] = questions
            sent_count += 1
        except Exception as e:
            failed_count += 1
            failed_reasons.append(f"{app_data.get('name', 'Unknown')}: {str(e)}")
            print(f"[send_assessment_all] FAILED for {app_data.get('name')}: {e}")

    save_data(data)

    # Flash summary
    parts = []
    if sent_count:
        parts.append(f"✅ {sent_count} assessment{'s' if sent_count != 1 else ''} generated")
    if skipped_count:
        parts.append(f"⏭️ {skipped_count} skipped (already sent)")
    if failed_count:
        parts.append(f"❌ {failed_count} failed ({'; '.join(failed_reasons)})")
    flash(" • ".join(parts) if parts else "No assessments to send.", "success")

    # Same filters ke saath wapas redirect
    params = {}
    if job_id: params["job_id"] = job_id
    if status: params["status"] = status
    if search: params["search"] = search
    return redirect(url_for("admin_applications", **params))


@app.route("/admin/applications/<app_id>/send-assessment", methods=["POST"])
@admin_required
def send_assessment(app_id):
    data = load_data()
    app_data = next((a for a in data["applications"] if a["id"] == app_id), None)
    if not app_data:
        flash("Application not found.", "error")
        return redirect(url_for("admin_applications"))

    job = next((j for j in data["jobs"] if j["id"] == app_data["job_id"]), None)
    # Job posting delete/edit ho gaya ho tab bhi assessment generate ho —
    # application mein saved job_title/job_requirements ka fallback use karo.
    job_title = job["title"] if job else app_data.get("job_title", "Unknown Role")
    job_requirements = job["requirements"] if job else app_data.get("job_requirements", "")

    try:
        questions = generate_assessment_questions(
            app_data.get("resume_text", ""),
            job_requirements,
            job_title,
        )
        app_data["assessment_questions"] = questions
        save_data(data)
        flash(f"Assessment generated! Share this link with the candidate: /assessment/{app_id}", "success")
    except Exception as e:
        flash(f"Assessment generation failed: {str(e)}", "error")
    return redirect(url_for("view_application", app_id=app_id))


@app.route("/admin/applications/<app_id>/assessment")
@admin_required
def view_assessment(app_id):
    """Admin: assessment result detail page — MCQ breakdown with scores."""
    data = load_data()
    app_data = next((a for a in data["applications"] if a["id"] == app_id), None)
    if not app_data:
        flash("Application not found.", "error")
        return redirect(url_for("admin_applications"))
    if not app_data.get("assessment_result"):
        flash("No assessment result yet. Candidate hasn't submitted the assessment.", "error")
        return redirect(url_for("view_application", app_id=app_id))
    return render_template("admin_assessment_result.html", application=app_data)


if __name__ == "__main__":
    app.run(debug=True, port=5000)
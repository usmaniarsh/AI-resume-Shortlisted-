# HR RecruitAI 🤖

AI-powered resume shortlisting tool with separate Admin and Candidate portals.

## Features
- **Candidate Portal** → Apply for jobs, upload resume (PDF/DOCX/TXT), get instant AI feedback
- **Admin Portal** → Post jobs, view all applications sorted by AI match score, filter by status/job
- **Gemini AI** → Analyzes each resume against job requirements: match score (0-100), strengths, gaps, recommendation
- **Auto Shortlisting** → Score ≥70 = Shortlisted, 50-69 = On Hold, <50 = Rejected

## Setup

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Set environment variables
```bash
export GOOGLE_API_KEY="your-api-key-here"
export ADMIN_PASSWORD="your-secure-password"  # default: admin123
export FLASK_SECRET_KEY="random-secret-string"
```

### 3. Run the app
```bash
python app.py
```

App runs at: http://localhost:5000

## URLs
| Page | URL |
|------|-----|
| Home | http://localhost:5000 |
| Candidate Portal | http://localhost:5000/candidate |
| Admin Login | http://localhost:5000/admin/login |
| Admin Dashboard | http://localhost:5000/admin |
| Post Job | http://localhost:5000/admin/jobs/new |
| View Applications | http://localhost:5000/admin/applications |

## Project Structure
```
hr_tool/
├── app.py              # Main Flask app
├── requirements.txt    # Python dependencies
├── data.json           # Auto-created: stores jobs & applications
├── uploads/            # Auto-created: stores uploaded resumes
└── templates/
    ├── base.html
    ├── index.html
    ├── candidate.html
    ├── apply.html
    ├── applied.html
    ├── admin_login.html
    ├── admin_dashboard.html
    ├── admin_applications.html
    ├── new_job.html
    └── view_application.html
```

## How It Works
1. HR posts a job with requirements in the Admin panel
2. Candidates visit /candidate, pick a job, upload their resume
3. Flask extracts text from PDF/DOCX/TXT
4. Gemini AI analyzes the resume against requirements → returns JSON with score, strengths, gaps
5. Application saved; candidate sees their result immediately
6. HR sees all applications ranked by AI score in the Admin panel


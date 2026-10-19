# WebMonkie

Recruiting workflow prototype: job-description review, fictional sample candidates, evidence-based candidate briefs, one-page PDFs and interview packs.

## Local preview

Create a Python virtual environment, install requirements.txt, set AI_MAX_DAILY_REQUESTS=0 for no-cost sample testing, and run START_LOCAL.ps1 on Windows. Open http://127.0.0.1:8000/register/ and choose Sample walkthrough when creating a session.

## Deployment status

The Docker Compose files are staging drafts. Container builds, isolation, backups and public routing require environment-specific verification before production use. Keep lifecycle execution disabled until its operational checks pass.

The Plesk proxy example replaces the root route; do not apply it to an existing marketing site. Preserve existing sites and review selective application routes before exposing the backend.

Synthetic results demonstrate the workflow and do not establish live AI accuracy or hiring efficacy. Do not submit real candidate data to an unverified deployment.

No credentials, account data or internal operational ledgers are included.

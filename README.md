# WebMonkie

An evidence-led hiring workflow by AscendWorksNow.

## Current pilot

The automatic pilot accepts a fictional role and three canonical fictional resumes, processes them in the background, and presents candidate briefs, evidence, interview examples and downloadable one-page PDFs. These are clearly labelled fixed demonstration results. Customer-document analysis and live web research are unavailable in this pilot; the sample does not establish AI assessment accuracy.

`START_LOCAL.ps1` starts the loopback-only preview and its sequential dispatcher. Open http://127.0.0.1:8000/register/, verify using the local mail view, sign in and choose **New submission**. Keep the startup terminal open. Ctrl+C stops this preview and its dispatcher. The preview profile disables paid AI, outbound email and retention execution regardless of inherited service settings.

Private local data persists across restarts. Use fictional data in development. Do not place confidential resumes in a synced source folder or commit private data, credentials or environment files.

## Existing small VPS

See [the pilot deployment runbook](deploy/PILOT.md). Use the standalone `deploy/compose.pilot.json`, not the full-stack Compose file. The pilot has a single web worker, sequential dispatcher and PostgreSQL with a combined 544 MiB container ceiling; actual peak memory alongside existing hosting must be verified. Selective application routes preserve the existing marketing homepage and other domains. No new cloud service or AI credit purchase is needed for synthetic testing.

Acknowledgement and completion email support is implemented; production SMTP delivery must be tested. HTTPS, real host resource measurements, protected backup/restore drills and operational approval remain deployment gates. Retention execution and pilot export/restore stay disabled. Real document analysis requires isolated execution and an explicit provider budget. Keep `AI_MAX_DAILY_REQUESTS=0` while testing the synthetic pilot.

## Verification

Dependencies are pinned in `requirements-production.txt`. CI checks source exclusions, migrations, PostgreSQL behavior, browser upload cancellation and current dependency advisories. Passing checks do not establish live assessment efficacy or guarantee the absence of every vulnerability.

Use this tool to support human review. It does not independently authenticate resume claims or make hiring decisions.

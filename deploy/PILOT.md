# Zero-spend synthetic pilot on an existing Plesk VPS

This release demonstrates the automatic submission journey with three fictional resumes. It does not evaluate customer documents, perform live research, or establish assessment accuracy. No new cloud service or AI credit purchase is required. Keep production lifecycle execution off.

## Boundaries

Use `compose.pilot.json` by itself, never merged with `compose.json`. Its separate `webmonkie-pilot` project has one web worker (256 MiB), one sequential dispatcher (96 MiB), and PostgreSQL (192 MiB). These are enforced ceilings, not measured capacity claims. Verify actual peak memory alongside Plesk before inviting testers. There is no local malware scanner or parser because this profile rejects real document intake.

Only `127.0.0.1:8089` is published. PostgreSQL has no published port. Keep the marketing document root and existing `location /` untouched. The application starts at `/dashboard/`; only the listed application paths and `/static/recruiting/` should be routed to this release. Other domains must not be changed.

## Administrator preflight

1. Obtain the reviewed, sanitized release archive and its SHA-256 manifest. Verify the checksum before extraction. Use an immutable `/opt/webmonkie/releases/<release>` directory; never extract over an existing directory. Source history, `.env`, databases and private files must not be included in the archive.
2. Record the current domain document root and generated nginx configuration. Back up this domain's existing additional directives and marketing files to protected, dated storage. Inspect port 8089, existing Docker projects/networks, disk space, host memory and Plesk services. Stop if there is a collision or insufficient headroom. Do not stop other workloads to make room.
3. Confirm `/srv/webmonkie/private` is mode 0700, owned by UID/GID 10001; `/etc/webmonkie` is root-only. Back up the existing protected environment files without printing their contents. The deployment account should stage archives only; do not add it to the Docker group or grant unrestricted sudo.
4. Use reviewed immutable Python/PostgreSQL references from `images.lock.env`. Re-audit before building; an old digest is not proof of current safety. Configure `WEBMONKIE_RELEASE`, `WEBMONKIE_ENV_FILE`, `WEBMONKIE_PRIVATE_DIR`, both distinct PostgreSQL passwords, and image references in a mode-0600 administrator-owned Compose environment file. Preserve application secret/archive keys. Never publish rendered Compose output because it can contain passwords.
5. The application environment must use the exact public host and HTTPS origin, Gmail SMTP configuration, and an exact trusted proxy address determined from the new container network. Do not reuse the old gateway address blindly. Compose forces `config.pilot`, fixtures, zero AI allowance and disabled lifecycle even if the previous application environment used the full profile.

## Build and checks (administrator console, inside the immutable release)

Use the same protected environment path for every invocation:

```sh
docker compose --env-file /etc/webmonkie/compose.env -f deploy/compose.pilot.json config --quiet
docker compose --env-file /etc/webmonkie/compose.env -f deploy/compose.pilot.json build web
docker compose --env-file /etc/webmonkie/compose.env -f deploy/compose.pilot.json up -d database
docker compose --env-file /etc/webmonkie/compose.env -f deploy/compose.pilot.json run --rm --no-deps web python manage.py migrate --noinput
docker compose --env-file /etc/webmonkie/compose.env -f deploy/compose.pilot.json run --rm --no-deps web python manage.py check --deploy
docker compose --env-file /etc/webmonkie/compose.env -f deploy/compose.pilot.json up -d web dispatcher
docker compose --env-file /etc/webmonkie/compose.env -f deploy/compose.pilot.json ps
docker compose --env-file /etc/webmonkie/compose.env -f deploy/compose.pilot.json stats --no-stream
```

Do not switch public traffic if the build exhausts memory, a container restarts, health checks fail, or Plesk is affected. Inspect the actual bridge gateway/connection peer, set the exact trusted address, and recreate only these two application containers after configuration changes. Verify `/health/ready/` from loopback with the configured Host header; it must confirm dispatcher heartbeat and private-storage admission. A live web process alone is insufficient.

Review `plesk-pilot-nginx.conf.example` against this domain's generated configuration. Replace `REVIEWED_RELEASE` with the installed immutable directory. The fragment preserves `/` and adds only app prefixes. Keep request buffering enabled and ensure nginx client-body storage uses host disk. Apply only to this domain after backup and a successful nginx configuration test; do not blindly concatenate conflicting locations. No automatic Plesk configuration script is supplied.

## Acceptance

- Confirm the marketing homepage, its assets, and other domains retain their previous behavior.
- Confirm HTTPS certificate validation, secure cookies, registration, email verification and password recovery. Test a real acknowledgement/completion email only with the owner's designated test address; never log its secret or link token.
- Submit the fictional sample; observe acknowledgement, queued status, three completed briefs, evidence, five interview examples and the one-page PDFs. Reconnect and confirm history remains.
- Confirm one user cannot open another user's submission, status or report. Confirm raw upload/download, legacy execution, export/restore and internal callback routes remain unavailable in this pilot.
- Monitor peak container/host memory during submission and PDF generation. Confirm no OOM/restarts, stable Plesk response, bounded private disk usage, and zero AI calls. Do not infer live assessment quality from fixed sample outputs.

## Rollback

Restore only this domain's saved additional nginx directives, validate configuration, and reload through the administrator's normal Plesk procedure. The marketing files were never overwritten. Stop only the `web` and `dispatcher` services of the `webmonkie-pilot` project if needed. Preserve its database volume and private directory; **never use `down -v`**. For a later compatible application release, return to its previously reviewed image/configuration. Database rollback requires a separately verified protected backup and explicit restore plan; do not assume reversing a migration is safe. Retain the failed release for diagnosis without exposing secrets.

## Operational gates still closed

Public readiness requires measured VPS behavior, a real SMTP delivery check, HTTPS route verification, and protected backup/restore drills. Real resume parsing/research requires a verified isolated execution service and an agreed AI budget. Archive/deletion execution remains disabled pending host drills. Commercial efficacy, pricing and monetization are separate follow-up work.

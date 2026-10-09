# Phase 16: Hardening

**Day:** 7 (Tue Oct 13) · **Depends on:** 15 · **Where:** local, then a redeploy and an AWS review · **PLAN.md sections:** Auth and security (all); Well-Architected (Security)

## Goal

Rate limits, body size limits, CORS, security headers, and a written security review of the deployed system. The SSRF guard (phase 04) and nh3 sanitizing (phase 13) already exist; this phase re-tests them as part of the review.

## Read first

- `CLAUDE.md`, `docs/PROGRESS.md` (extension ID)
- `docs/PLAN.md`: "Auth and security"

## Tasks

1. Rate limits with slowapi: 30 requests per minute per client IP on `GET /api/search` and `GET /ui/search`. The key function uses `request.client.host` (already the viewer IP thanks to `--proxy-headers`, phase 10). For the MCP `search` tool, call the same limiter check with the IP from `ctx.request_context.request`; stdio mode is not limited. Over the limit returns 429 (REST) or a tool error "Rate limit exceeded, try again in a minute" (MCP).
2. Body size: middleware that rejects any request whose `Content-Length` exceeds 20 MB with 413, and counts streamed bytes for requests without it. Keep the 300-page PDF limit from phase 04.
3. CORS: `CORSMiddleware` allowing only `KB_EXTENSION_ORIGIN` (`chrome-extension://<id>` from PROGRESS.md), methods GET, POST, PATCH, DELETE, header `Authorization` and `Content-Type`. Set the variable in `infra/app.yaml` user data via a stack parameter `ExtensionOrigin`. (Extension pages with host permissions are not subject to CORS, so this mainly stops other websites from calling write routes from a browser.)
4. Security headers on HTML responses: `Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' https: data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`. Confirm the UI still works (no inline scripts were allowed in phase 13).
5. Error hygiene: confirm every unhandled exception returns `{"error": "internal"}` with no stack trace, and that logs never contain the `Authorization` header or token values (grep the CloudWatch log group for `Bearer`).
6. Tests: 31st search in a minute gets 429; a 21 MB upload gets 413; CORS preflight from the extension origin succeeds and from `https://evil.example` gets no `Access-Control-Allow-Origin`; CSP header present on `/`; re-run the SSRF and XSS tests.
7. Redeploy, then run the review checklist against AWS and write `docs/SECURITY_REVIEW.md` with one line per item (pass, fail, or note):
   - [ ] App instance has no public IP; no SG allows port 22.
   - [ ] `AlbSg` ingress is only the CloudFront prefix list on 80; `AppSg` only 8000 from `AlbSg`; no other security group or stand-alone ingress rule exists (there is no file system and no port 2049) (`aws ec2 describe-security-groups`).
   - [ ] Launch template requires IMDSv2 (hop limit 2, deviation 1, with the SSRF guard as the compensating control).
   - [ ] S3 default encryption on, versioning on, all four Block Public Access settings on (the bucket holds the originals, the image tarballs under `images/`, the database snapshot under `snapshots/` and the backups under `backups/`). Root volume encryption status recorded (`aws ec2 describe-volumes --filters Name=tag:Project,Values=kb-mcp --query 'Volumes[].Encrypted'`); if the sandbox forced `Encrypted: false` (KMS is list only), list it as a known limitation: the live database sits unencrypted on the instance disk, while every copy in S3 is encrypted.
   - [ ] Token parameter is a SecureString; no secret in git (`git log -p | grep -iE 'token_urlsafe|Bearer [A-Za-z0-9_-]{20,}'` finds nothing real), in the image (`docker history`), or in the launch template user data.
   - [ ] `ingest_url` refuses `http://169.254.169.254/` and `http://10.0.11.10/` on the deployed service.
   - [ ] Uploaded `<script>` content renders inert on the deployed doc page.
   - [ ] Write routes and MCP write tools return 401 / tool error without a token.
   - [ ] Known limitations listed: CloudFront to ALB is plain HTTP; bearer tokens instead of MCP OAuth 2.1; token in `sessionStorage` for the UI.

## Do not

- Change the auth model (OAuth is a named future upgrade, not this phase).

## Acceptance

- [ ] `pytest -q` passes.
- [ ] `docs/SECURITY_REVIEW.md` exists with every checklist item marked.
- [ ] Phase log updated; commit `phase 16: hardening`.

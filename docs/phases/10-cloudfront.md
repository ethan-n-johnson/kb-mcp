# Phase 10: CloudFront edge

**Day:** 5 (Sun Oct 11) · **Depends on:** 09 · **Where:** AWS · **PLAN.md sections:** Summary (Edge row); Auth and security (HTTPS, security group chain, known limitation); Course alignment (Caching content); Risks (CloudFront rows)

**Cut line 4:** if behind schedule, skip this phase, demo over the ALB on HTTP, and name the limitation in the presentation.

## Goal

HTTPS for viewers through CloudFront's default certificate, with API and MCP traffic uncached and the `Authorization` header reaching the app, and the ALB reachable only from CloudFront.

## Read first

- `CLAUDE.md`, `docs/PROGRESS.md` (prefix list ID from phase 00)
- `docs/PLAN.md`: "Auth and security", CloudFront rows in the risk table

## Tasks

1. Add to `infra/app.yaml` a parameter `CloudFrontPrefixListId` (default: the ID recorded in phase 00) and an `AWS::CloudFront::Distribution`:
   - Origin: the ALB DNS name, `CustomOriginConfig` with `OriginProtocolPolicy: http-only`, `OriginReadTimeout: 60`, `OriginKeepaliveTimeout: 5`.
   - Default cache behavior (covers `/api/*`, `/mcp*`, UI pages): `ViewerProtocolPolicy: redirect-to-https`, all 7 allowed methods, cached methods GET and HEAD, `CachePolicyId` = managed **CachingDisabled** (`4135ea2d-6df8-44a3-9df3-4b5a84be39ad`), `OriginRequestPolicyId` = managed **AllViewerExceptHostHeader** (`b689b0a8-53d0-40ab-baf2-68738e2966ac`), `Compress: true`.
   - Extra cache behavior `/static/*`: GET and HEAD only, managed **CachingOptimized** (`658327ea-f89d-4fab-a63d-7e88639e58f6`), redirect-to-https.
   - Also add explicit behaviors for `/api/*` and `/mcp*` identical to the default, so the intent is visible in the template.
   - `PriceClass_100`, `HttpVersion: http2`, default certificate, enabled, comment `kb-mcp`.
   - Output `DistributionDomainName`.
   Verify the managed policy IDs with `aws cloudfront list-cache-policies --type managed` and `aws cloudfront list-origin-request-policies --type managed` before hardcoding them.
2. Lock the ALB: change `AlbSg` ingress to TCP 80 from `SourcePrefixListId: !Ref CloudFrontPrefixListId` only. The prefix list counts as many rules against the security group quota; if the update fails on a quota, record the error and ask the user.
3. Deploy (`infra/deploy.sh app <current tag>`). CloudFront takes several minutes; wait with `aws cloudfront wait distribution-deployed --id <id>`.
4. Check the `Authorization` header end to end. All write routes and `/mcp` are non-GET, which CloudFront forwards with the header under this policy pair. Prove it with a real write. If the app does not receive the header, create a custom cache policy (min/default/max TTL 0 is not allowed with headers in the key, so use min 0, default 0, max 1) that includes `Authorization` in the cache key, use it for `/api/*` and `/mcp*`, and record the deviation.
5. Client IP for rate limits: confirm `request.client.host` in the app is the viewer's IP (uvicorn `--proxy-headers` reads the left-most `X-Forwarded-For`). Add a temporary debug log line or check the access log, then remove it.
6. Record the CloudFront domain in `docs/PROGRESS.md` Outputs.
7. **HUMAN:** replace the ALB-based MCP server in Claude Code: `claude mcp remove kb-alb`, then `claude mcp add --transport http kb https://<DistributionDomainName>/mcp --header "Authorization: Bearer $KB_TOKEN"`. Try one read and one write prompt.

## Do not

- Buy a domain or request an ACM certificate.
- Cache anything under `/api/*` or `/mcp*`.

## Acceptance

- [ ] `curl -s https://<domain>/healthz` returns 200.
- [ ] `curl -sI http://<domain>/healthz` returns a 301 to `https://`.
- [ ] `curl -s --max-time 10 http://<AlbDnsName>/healthz` now times out (direct ALB access blocked).
- [ ] An authenticated `POST https://<domain>/api/docs` succeeds and the audit log shows the token name; the same request without the token gets 401.
- [ ] MCP `tools/list` via `https://<domain>/mcp` returns 200 with 11 tools; the user confirms Claude Code works through it.
- [ ] Two identical `GET /api/search` requests both show `x-cache: Miss from cloudfront` (not cached).
- [ ] Phase log updated; commit `phase 10: CloudFront edge`.

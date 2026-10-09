# Phase 09: MCP layer

**Day:** 4 (Sat Oct 10) · **Depends on:** 08, 06 · **Where:** local, then a redeploy · **PLAN.md sections:** Architecture (MCP mounting paragraph); Interfaces (tool table, tool descriptions, connecting Claude Code)

## Goal

The 11 MCP tools, served over HTTP at exactly `/mcp` (stateless, JSON responses) and over stdio, both calling `service.py`. Then test from Claude Code locally and through the ALB.

## Read first

- `CLAUDE.md` (stdio logging rule), `docs/PROGRESS.md`
- `docs/PLAN.md`: the FastMCP paragraph in "Architecture", the whole "Interfaces" section

## Tasks

1. `kb/mcp_tools.py`: `build_mcp(state) -> FastMCP` using `FastMCP("kb", stateless_http=True, json_response=True)`.
   - Register 11 tools named exactly as PLAN.md's table, with the same arguments and defaults. Return plain dicts in the same shapes as REST (reuse `kb/schemas.py` `.model_dump()`).
   - Tool descriptions say **when** to use the tool, not just what it does. Examples to adapt:
     - `search`: "Search the team knowledge base before answering questions about our systems, AWS setup or runbooks. Check `confidence`: if it is low, tell the user the knowledge base has no good answer instead of guessing."
     - `upload`: "Save a new note or document to the knowledge base. If the result is `possible_duplicate`, show the candidates and ask whether to update, merge, or save anyway with force=true."
     - `gaps`: "List questions the knowledge base could not answer, most frequent first. Use when asked what documentation is missing."
     Write all 11 in this style.
2. Write-token checks for MCP:
   - HTTP: tools take a `ctx: Context` parameter and read `ctx.request_context.request.headers.get("authorization")`; pass through `auth.check_token`.
   - stdio: there is no request; use `KB_TOKEN` from the environment.
   - Build this as one helper `require_writer_mcp(ctx) -> str` that handles both. Read tools never check. A failed check raises an error whose message is "This tool needs a valid write token" (no detail about which part failed).
3. Mount in `kb/main.py`: create the MCP app with `streamable_http_path="/"` (setting or constructor argument, whichever the installed SDK version supports) and `app.mount("/mcp", mcp.streamable_http_app())`; run `mcp.session_manager.run()` inside the FastAPI lifespan.
   - **Requirement:** `POST /mcp` (no trailing slash) must answer directly with 200, **not** a 307 redirect. Behind CloudFront a redirect would point at the ALB's plain-HTTP host and break clients. If the mount redirects, add a tiny ASGI middleware that rewrites path `/mcp` to `/mcp/` before routing, and test both forms.
4. `python -m kb.cli mcp-stdio`: set up logging to **stderr**, open `KB_DB`, load models (respect `KB_MODELS`), backfill and build the index, build the same tool set, `mcp.run(transport="stdio")`. Nothing may print to stdout except the protocol.
5. Tests:
   - HTTP via `TestClient` or `httpx.ASGITransport`: JSON-RPC `initialize`, `tools/list` returns exactly the 11 names, `tools/call search` returns results, `tools/call upload` without a token returns an error result, with `Authorization: Bearer testtoken` succeeds and writes an audit row with the token name. Both `/mcp` and `/mcp/` return 200.
   - stdio: spawn `python -m kb.cli mcp-stdio` with the `mcp` client SDK (`stdio_client`), list tools and call `search` against a temp DB.
6. **HUMAN:** connect Claude Code locally and try it. Give the user these commands (with their own token from the password manager):

   ```bash
   # stdio against a local DB
   claude mcp add kb-local -e KB_DB="$PWD/.localdata/kb.db" -e KB_TOKEN=devtoken -- "$PWD/.venv/bin/python" -m kb.cli mcp-stdio
   # HTTP against the local server
   claude mcp add --transport http kb-dev http://localhost:8000/mcp --header "Authorization: Bearer devtoken"
   ```

   Suggested prompts: "Using the kb, search for <a fixture topic>", "Save a short note to the kb titled Test note", "What questions has the kb failed to answer?"
7. Redeploy: `infra/push-image.sh`, `infra/deploy.sh app <tag>`. **HUMAN:** add the remote server over plain HTTP for now: `claude mcp add --transport http kb-alb http://<AlbDnsName>/mcp --header "Authorization: Bearer $KB_TOKEN"` and repeat one read and one write prompt.

## Do not

- Use sessions or SSE streaming (`stateless_http` and `json_response` stay on).
- Duplicate any business logic from `service.py` in tool functions.

## Acceptance

- [ ] `pytest -q` passes (and `-m models` if any new model tests).
- [ ] `curl -s -X POST localhost:8000/mcp -H 'content-type: application/json' -H 'accept: application/json, text/event-stream' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'` returns 11 tools with HTTP 200 and no redirect (if the SDK requires `initialize` first, show that sequence instead).
- [ ] The user confirms Claude Code called `search` and `upload` through `kb-local` and through `kb-alb`.
- [ ] Phase log updated; commit `phase 09: MCP layer`.

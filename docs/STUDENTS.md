# Using the course knowledge base

A searchable collection of AWS documentation, FastAPI docs, MCP spec pages and course notes. You can search it in the browser or from Claude Code. It runs on AWS for this class only.

Address: **https://dsrig13j20ooy.cloudfront.net**

## Search in the web UI

1. Open the address above in a browser.
2. Type a question in the search box, for example "how does an Auto Scaling group replace an unhealthy instance", and press Enter.
3. Each result shows the document title, the heading path inside it, a short snippet and the source link. Click a result to read the whole document.
4. Look at the badge and the confidence:
   - **Confidence high or medium**: the knowledge base found a good answer.
   - **Confidence low**: there is probably no good answer in the knowledge base. Do not trust the top hit; the question shows up in the "gaps" list for the instructor.

## Add it to Claude Code (read only)

```bash
claude mcp add --transport http kb https://dsrig13j20ooy.cloudfront.net/mcp
```

Then start Claude Code and ask, for example: "Using the kb, how do I mount EFS on Amazon Linux?". Run `/mcp` to check that `kb` is connected. You can search and read; you cannot change anything.

Saving, editing or deleting documents needs a **write token**. Only the instructor hands those out. If you try a write tool without one, Claude tells you "This tool needs a write token". That is expected. (If you were given a token, add it with `--header "Authorization: Bearer <token>"` at the end of the `claude mcp add` command above, after removing the old entry with `claude mcp remove kb`.)

## What "keyword" mode means

Results come in two modes:

- **semantic**: the normal mode. It matches the meaning of your question, even when the words differ.
- **keyword**: a simpler word-matching search. It is used when the class is busy (many people searching at once) or when the instructor turns the fallback on. Keyword answers still work but rank less cleverly, so rephrase with the exact terms (for example "NAT gateway private subnet") if the first result looks off.

## When the class is busy

- A search can take up to about 5 seconds. Wait for it rather than clicking again, because every click is another search.
- You share a limit of 120 searches per minute with everyone on the same network (the campus network counts as one address for each server). If you see **429 or "Rate limit exceeded"**, wait one minute and try again.
- If a page does not load at all, wait a few seconds and reload. The service replaces a failed server by itself within a few minutes.

## Good questions to try

- "how do NAT gateways let private subnets reach the internet"
- "how do I enable versioning on an S3 bucket"
- "what is the Streamable HTTP transport in MCP"
- "keep older copies of files around after someone overwrites them" (a paraphrase with no shared keywords)

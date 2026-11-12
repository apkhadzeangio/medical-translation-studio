# AI workflow development with this n8n Cloud instance

Instance: `https://ramaz97.app.n8n.cloud/`.

n8n's built-in instance MCP server supports searching, creating, editing and executing
workflows on supported versions (authoring starts at n8n 2.13.0). Use its native OAuth
connection for an AI coding client. A model-provider key is not an n8n access credential.

1. In n8n, open **Settings → Instance-level MCP** and enable MCP access.
2. Open **Connect**, select **OAuth**, then select **Codex** as the client when offered.
3. Copy the server URL from n8n and confirm it is this instance's URL. For this instance,
   the standard URL is `https://ramaz97.app.n8n.cloud/mcp-server/http`.
4. In the machine/client that will develop workflows, add the server and complete sign-in:

```sh
codex mcp add n8n --url "https://ramaz97.app.n8n.cloud/mcp-server/http"
codex mcp login n8n
```

5. Enable **Available in MCP** only on the existing workflows the client should edit. Confirm
   the client can list them before asking it to modify anything. n8n's search previews and
   full-workflow access have different scopes.

Do not put either an LLM key or an n8n token in workflow JSON, source control or a prompt.
For workflows calling this Studio, store the Studio's bearer token in an n8n credential.
The public endpoints are under `/api`; upload and translation require that authentication.

The Studio already owns parsing, translation stages, human edits, cost records and job
recovery. n8n can orchestrate that API; keep the durable translation data in the Studio.
Test workflow changes on a copy with synthetic inputs before enabling scheduled triggers.

Connection is not established by committing this guide. No MCP grant or workflow change
has been made on the supplied n8n instance by this branch.

References:
- https://docs.n8n.io/connect/connect-to-n8n-mcp-server/
- https://docs.n8n.io/connect/connect-to-n8n-mcp-server/mcp-client-examples/

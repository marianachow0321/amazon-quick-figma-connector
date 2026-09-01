# Figma MCP Proxy

A proxy server that enables Amazon Quick Suite to connect to Figma's REST API as MCP tools, with each user authenticating as themselves in Figma.

## Why this exists

Amazon Quick cannot reach Figma through any of its built-in paths:

| Path | Blocker |
|---|---|
| Native Figma connector | Not present in the console catalog |
| MCP connector → Figma's own MCP server | Requires the `mcp:connect` scope, which is not grantable to self-created Figma apps — it does not appear in the app's scope list at all. Both managed OAuth and a custom OAuth client are rejected with `OAuth app ... doesn't exist`. |
| OpenAPI / REST custom connector | Quick emits an `Authorization` header containing a control character. The request is rejected with an ALB-level HTTP 400 before reaching the target API. |

Reproduction of the third: `Authorization: Bearer abc\x01def` against `https://api.figma.com/v1/me` returns HTTP 400 with `Server: awselb/2.0`, an HTML body, and no `x-figma-rest-api-request-id` — byte-identical to what Quick's connector produces. Every other malformation (empty token, embedded space, 9 KB junk) returns a normal Figma JSON error.

This proxy routes around all three: it speaks MCP to Quick, REST to Figma, and forwards the caller's own OAuth token.

## Architecture

### MCP requests

```
┌─────────────┐    ┌──────────────┐    ┌──────────────┐    ┌────────────────────┐
│             │───▶│              │───▶│              │───▶│                    │
│ Amazon Quick│    │ API Gateway  │    │    Lambda    │    │ api.figma.com/v1   │
│             │◀───│ (throttled)  │◀───│ MCP ⟷ REST   │◀───│                    │
└─────────────┘    └──────────────┘    └──────────────┘    └────────────────────┘
```

The Lambda implements MCP — `initialize`, `tools/list`, `tools/call` — and translates each tool call into a Figma REST request. It holds no credentials; the caller's `Authorization` header is forwarded.

### OAuth flow

```
Quick ──▶ /oauth/authorize (Lambda injects scope) ──302──▶ figma.com/oauth
                                                               │
                                          user consents, redirect with code
                                                               ▼
                                                        Amazon Quick
                                                               │
                             exchanges code directly with api.figma.com/v1/oauth/token
```

Quick's MCP connector form has no scope field, so it sends no `scope` parameter and Figma rejects the request. `/oauth/authorize` injects the configured scopes and redirects. The token exchange goes directly to Figma — the proxy is not involved.

## Identity model

Each Quick user authenticates against Figma as themselves and their own token is forwarded. Figma's file permissions and audit trail apply per user. No shared service credential exists anywhere in the system.

## Prerequisites

* AWS account
* A Figma OAuth app — [figma.com/developers/apps](https://www.figma.com/developers/apps)

### Figma app setup

1. Create an app and note the **Client ID** and **Client secret**.
2. Add the redirect URI, substituting your region:
   ```
   https://us-east-1.quicksight.aws.amazon.com/sn/oauthcallback
   ```
3. Add the scopes you need, then **submit the app version**. Figma reviews scope requests; until the version is approved, the consent screen returns `Invalid scopes for app`.

Scope-to-tool mapping:

| Tool | Scope needed |
|---|---|
| `figma_get_me` | `current_user:read` |
| `figma_get_file` | read file contents |
| `figma_get_file_comments` | read comments |
| `figma_post_comment` | write comments |

Copy scope identifiers verbatim from Figma's app scope picker — the UI shows prose descriptions, and guessing the identifiers wastes review cycles.

## Deploy

### From AWS CloudShell

The shortest path — no local toolchain, and credentials are already in place.
CloudShell ships git, Node, and Python, and runs as your console identity.

Open CloudShell in the **same region** you want the stack in. The region
selector in the console header sets the deployment region; there is no separate
flag below.

```bash
git clone https://github.com/marianachow0321/amazon-quick-figma-connector.git
cd amazon-quick-figma-connector
npm install
npx cdk bootstrap        # first time only, per account+region
npx cdk deploy --require-approval never \
  -c figmaScopes="current_user:read file_content:read file_comments:read file_comments:write"
```

Copy the `McpEndpoint` output — it is the MCP server endpoint for the Quick
connector.

Notes specific to CloudShell:

* **Home directory persists (1 GB); everything else does not.** Clone into
  `~` so the checkout survives a session timeout. `node_modules` is a few
  hundred MB, which fits, but if you hit the quota run
  `rm -rf ~/.npm ~/amazon-quick-figma-connector/node_modules` and reinstall.
* **Sessions idle out after ~20 minutes.** `cdk deploy` here takes 2-3 minutes
  so this rarely matters, but do not walk away mid-bootstrap.
* **Your console role is the deploying identity.** It needs CloudFormation,
  Lambda, API Gateway, IAM, and S3 permissions. An admin role is simplest;
  a scoped role must be able to create IAM roles, since CDK creates the Lambda
  execution role.
* **No Docker required.** The Lambda asset is plain Python, bundled without
  container builds — which matters because CloudShell has no Docker daemon.

### From a local machine

```bash
npm install
npx cdk bootstrap        # first time only
npx cdk deploy --require-approval never
```

To request more than the default scope:

```bash
npx cdk deploy -c figmaScopes="current_user:read files:read" --require-approval never
```

The MCP endpoint and Quick settings are printed as stack outputs.

## Configure the Amazon Quick MCP connector

Connectors → Create for your team → Model Context Protocol (MCP)

| Field | Value |
|---|---|
| MCP server endpoint | `https://<api-id>.execute-api.<region>.amazonaws.com/prod` |
| Connection type | Public network |
| Authentication method | User authentication |
| Client ID | your Figma app client ID |
| Client secret | your Figma app client secret |
| Authorization URL | `https://<api-id>.execute-api.<region>.amazonaws.com/prod/oauth/authorize` |
| Token URL | `https://api.figma.com/v1/oauth/token` |
| Redirect URL | `https://<region>.quicksight.aws.amazon.com/sn/oauthcallback` |

Then click **Sign in** on the connector and ask *"who am I in Figma"*.

## Tools

| Tool | Figma endpoint |
|---|---|
| `figma_get_me` | `GET /me` |
| `figma_get_file` | `GET /files/{file_key}` — pass `depth` to keep responses small |
| `figma_get_file_comments` | `GET /files/{file_key}/comments` |
| `figma_post_comment` | `POST /files/{file_key}/comments` |

## Notes

* **MCP protocol version** is pinned to `2025-03-26`. Quick has lagged behind newer revisions; if no tools are discovered, suspect the protocol version before the configuration.
* **The endpoint validates nothing itself.** It forwards whatever token it receives and relies on Figma to reject bad ones, so it holds no secrets — but it is internet-reachable and therefore throttled at 100 rps with a 200 burst.
* **`figma_get_file` responses can be very large.** The tool description instructs the agent to pass a small `depth`. If context bloat becomes a problem, shape the response in the Lambda rather than returning Figma's payload verbatim.
* **Debug logging** is off by default and never logs token values. Enable with `-c logDebug=true`.

## Teardown

```bash
npx cdk destroy
```

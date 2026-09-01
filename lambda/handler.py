"""
Figma MCP Proxy for Amazon Quick Suite.

Amazon Quick's MCP connector cannot reach Figma's own MCP server: the required
`mcp:connect` scope is not grantable to self-created Figma apps (it does not
appear in the app's scope list at all), so both dynamic client registration and
a custom OAuth client are rejected with "OAuth app ... doesn't exist".

Quick's OpenAPI and REST custom connectors are also unusable -- they emit an
Authorization header containing a control character, which is rejected with an
ALB-level HTTP 400 before the request reaches the target API.

This Lambda therefore does two things:

  1. Acts as an OAuth shim, so Quick authenticates directly against Figma.
     Quick does not send a `scope` parameter, so /oauth/authorize injects it.
  2. Implements MCP (JSON-RPC) and translates tool calls into Figma REST calls,
     forwarding the caller's own OAuth token.

Because the caller's token is forwarded rather than a stored service credential,
each Quick user acts as themselves in Figma.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request

FIGMA_API_BASE = "https://api.figma.com/v1"
FIGMA_AUTHORIZE_URL = "https://www.figma.com/oauth"
FIGMA_TOKEN_URL = "https://api.figma.com/v1/oauth/token"

# Space-separated, per Figma's OAuth. Only scopes approved on the Figma app
# version will be granted; the rest fail at the consent screen.
FIGMA_SCOPES = os.environ.get("FIGMA_SCOPES", "current_user:read")

# Quick has lagged behind newer MCP revisions. 2025-03-26 is known to work.
MCP_PROTOCOL_VERSION = "2025-03-26"

LOG_DEBUG = os.environ.get("LOG_DEBUG", "false").lower() == "true"


def log(msg):
    if LOG_DEBUG:
        print(msg)


# ---------------------------------------------------------------- tool catalog

TOOLS = [
    {
        "name": "figma_get_me",
        "description": (
            "Get the Figma profile of the signed-in user, including handle, "
            "email, and profile image. Use for questions like 'who am I in Figma'."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "_method": "GET",
        "_path": "/me",
        "_scope": "current_user:read",
    },
    {
        "name": "figma_get_file",
        "description": (
            "Get a Figma design file's structure: pages, frames, and layer tree. "
            "The file key is the identifier in a Figma URL -- in "
            "figma.com/design/ABC123/My-Design the key is ABC123. "
            "Always pass a small depth (1 or 2) unless the full tree is needed, "
            "because full files can be very large."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_key": {
                    "type": "string",
                    "description": "The Figma file key from the file's URL.",
                },
                "depth": {
                    "type": "integer",
                    "description": "Levels of the layer tree to return. Use 1 or 2 to keep the response small.",
                },
            },
            "required": ["file_key"],
        },
        "_method": "GET",
        "_path": "/files/{file_key}",
        "_query": ["depth"],
        "_scope": "file_content:read",
    },
    {
        "name": "figma_get_file_comments",
        "description": "List comments on a Figma file, with author and timestamp.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_key": {
                    "type": "string",
                    "description": "The Figma file key from the file's URL.",
                }
            },
            "required": ["file_key"],
        },
        "_method": "GET",
        "_path": "/files/{file_key}/comments",
        "_scope": "file_comments:read",
    },
    {
        "name": "figma_post_comment",
        "description": "Post a comment on a Figma file.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_key": {
                    "type": "string",
                    "description": "The Figma file key from the file's URL.",
                },
                "message": {"type": "string", "description": "The comment text."},
            },
            "required": ["file_key", "message"],
        },
        "_method": "POST",
        "_path": "/files/{file_key}/comments",
        "_body": ["message"],
        "_scope": "file_comments:write",
    },
    {
        "name": "figma_get_file_metadata",
        "description": (
            "Get a Figma file's metadata only -- name, thumbnail URL, last "
            "modified time, editor type -- without loading the layer tree. "
            "Much cheaper than figma_get_file. Prefer this when the question is "
            "about a file's name, owner, or when it last changed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_key": {
                    "type": "string",
                    "description": "The Figma file key from the file's URL.",
                }
            },
            "required": ["file_key"],
        },
        "_method": "GET",
        "_path": "/files/{file_key}/meta",
        "_scope": "file_metadata:read",
    },
    {
        "name": "figma_list_projects",
        "description": (
            "List the projects (folders) in a Figma team. Use this first when the "
            "user names a design by description rather than giving a file key, "
            "then call figma_list_project_files to find the file. The team ID is "
            "in a team URL -- in figma.com/files/team/12345/My-Team the team ID "
            "is 12345."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "team_id": {
                    "type": "string",
                    "description": "The Figma team ID, from a team URL.",
                }
            },
            "required": ["team_id"],
        },
        "_method": "GET",
        "_path": "/teams/{team_id}/projects",
        "_scope": "folders:read",
    },
    {
        "name": "figma_list_project_files",
        "description": (
            "List the files in a Figma project (folder), returning each file's "
            "key and name. This is how to find a file key when the user does not "
            "have one. Get the project ID from figma_list_projects."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "The Figma project ID, from figma_list_projects.",
                }
            },
            "required": ["project_id"],
        },
        "_method": "GET",
        "_path": "/projects/{project_id}/files",
        "_scope": "folders:read",
    },
    {
        "name": "figma_get_file_versions",
        "description": (
            "List a Figma file's version history, with timestamps, labels, and "
            "the user who made each version. Use for questions about what "
            "changed and when."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_key": {
                    "type": "string",
                    "description": "The Figma file key from the file's URL.",
                }
            },
            "required": ["file_key"],
        },
        "_method": "GET",
        "_path": "/files/{file_key}/versions",
        "_scope": "file_versions:read",
    },
]

PUBLIC_TOOLS = [
    {k: v for k, v in t.items() if not k.startswith("_")} for t in TOOLS
]
TOOLS_BY_NAME = {t["name"]: t for t in TOOLS}


# ------------------------------------------------------------- oauth metadata


def _proxy_url():
    return os.environ.get("PROXY_URL", "")


def get_oauth_protected_resource():
    proxy = _proxy_url()
    return {
        "resource": proxy,
        "authorization_servers": [proxy],
        "scopes_supported": FIGMA_SCOPES.split(),
        "bearer_methods_supported": ["header"],
    }


def get_auth_server_metadata():
    proxy = _proxy_url()
    return {
        "issuer": proxy,
        "authorization_endpoint": f"{proxy}/oauth/authorize",
        "token_endpoint": f"{proxy}/oauth/token",
        "scopes_supported": FIGMA_SCOPES.split(),
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
    }


def handle_oauth_authorize(event):
    """Inject the scope Quick omits, then redirect to Figma."""
    params = dict(event.get("queryStringParameters") or {})
    params["scope"] = FIGMA_SCOPES
    params.setdefault("response_type", "code")
    url = FIGMA_AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)
    log(f"oauth/authorize -> Figma with scope: {FIGMA_SCOPES}")
    return {
        "statusCode": 302,
        "headers": {"Location": url, "Cache-Control": "no-cache, no-store"},
        "body": "",
    }


# ----------------------------------------------------------------- figma call


def handle_oauth_token(event):
    """
    Proxy the token exchange so failures are visible and the client
    authentication style can be adapted to what Figma expects.

    Quick posts client_id/client_secret in the form body. If Figma rejects
    that with invalid_client, retry with HTTP Basic auth instead. Never logs
    secrets or tokens -- only parameter names, status codes, and error codes.
    """
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        import base64
        body = base64.b64decode(body).decode()

    params = dict(urllib.parse.parse_qsl(body, keep_blank_values=True))
    log(f"oauth/token params: {sorted(params.keys())} grant_type={params.get('grant_type')}")

    client_id = params.get("client_id", "")
    client_secret = params.pop("client_secret", "")

    def post(form, basic=False):
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if basic:
            import base64 as b64
            creds = b64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
            headers["Authorization"] = f"Basic {creds}"
        req = urllib.request.Request(
            FIGMA_TOKEN_URL,
            data=urllib.parse.urlencode(form).encode(),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, (e.read().decode() if e.fp else "")
        except Exception as e:
            return 502, json.dumps({"error": "proxy_error", "error_description": str(e)})

    # Attempt 1: credentials in the body, as Quick sends them.
    with_secret = dict(params)
    with_secret["client_secret"] = client_secret
    status, text = post(with_secret)
    log(f"oauth/token attempt=body status={status} error={_err_code(text)}")

    # Attempt 2: HTTP Basic, in case Figma requires it.
    if status >= 400 and _err_code(text) in ("invalid_client", "unauthorized_client"):
        status, text = post(params, basic=True)
        log(f"oauth/token attempt=basic status={status} error={_err_code(text)}")

    if status >= 400:
        log(f"oauth/token FAILED status={status} body={text[:300]}")

    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
        "body": text,
    }


def _err_code(text):
    try:
        return json.loads(text).get("error", "")
    except Exception:
        return ""


def call_figma(method, path, token, query=None, payload=None):
    """Call Figma REST. Returns (status, parsed_body_or_text)."""
    url = FIGMA_API_BASE + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    data = json.dumps(payload).encode() if payload else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            # Figma OAuth access tokens go in Authorization: Bearer.
            # Personal access tokens (figd_) must use X-Figma-Token instead --
            # Figma rejects those in Authorization with an explicit message.
            "Authorization": token,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=50) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode() if e.fp else ""
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"error": True, "status": e.code, "message": raw[:500]}
    except Exception as e:  # network, timeout, DNS
        return 502, {"error": True, "status": 502, "message": str(e)}


# ------------------------------------------------------------------ mcp layer


def _result(rid, payload):
    return {"jsonrpc": "2.0", "id": rid, "result": payload}


def _error(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _tool_text(text, is_error=False):
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def handle_mcp(rpc, token):
    method = rpc.get("method")
    rid = rpc.get("id")
    params = rpc.get("params") or {}

    if method == "initialize":
        return _result(
            rid,
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "figma-mcp-proxy", "version": "1.0.0"},
            },
        )

    if method in ("notifications/initialized", "initialized"):
        return None  # notification: no response

    if method == "ping":
        return _result(rid, {})

    if method == "tools/list":
        return _result(rid, {"tools": PUBLIC_TOOLS})

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        tool = TOOLS_BY_NAME.get(name)
        if not tool:
            return _result(rid, _tool_text(f"Unknown tool: {name}", True))

        # Required arguments
        for req in tool["inputSchema"].get("required", []):
            if not args.get(req):
                return _result(
                    rid, _tool_text(f"Missing required argument: {req}", True)
                )

        # Path substitution
        path = tool["_path"]
        for key, value in args.items():
            path = path.replace("{" + key + "}", urllib.parse.quote(str(value), safe=""))

        query = {k: args[k] for k in tool.get("_query", []) if args.get(k) is not None}
        body = {k: args[k] for k in tool.get("_body", []) if args.get(k) is not None}

        status, parsed = call_figma(
            tool["_method"], path, token, query=query or None, payload=body or None
        )

        if status >= 400:
            msg = parsed.get("err") or parsed.get("message") or json.dumps(parsed)
            hint = ""
            if status in (401, 403):
                needed = tool.get("_scope")
                if needed:
                    granted = needed in FIGMA_SCOPES.split()
                    if granted:
                        hint = (
                            f" -- this tool needs the '{needed}' scope, which IS in "
                            "this deployment's scope string, so the likely cause is "
                            "that the Figma app version requesting it has not been "
                            "approved yet, or the signed-in user lacks access to "
                            "this file. Scopes never override per-user file access."
                        )
                    else:
                        hint = (
                            f" -- this tool needs the '{needed}' scope, which is NOT "
                            "in this deployment's scope string. Add it to the Figma "
                            "app, submit the app version for review, then redeploy "
                            f"with -c figmaScopes=\"{FIGMA_SCOPES} {needed}\"."
                        )
                else:
                    hint = (
                        " -- the Figma OAuth app may not have this scope approved. "
                        "Check the app's scope list and submitted version."
                    )
            return _result(rid, _tool_text(f"Figma returned {status}: {msg}{hint}", True))

        return _result(rid, _tool_text(json.dumps(parsed)))

    return _error(rid, -32601, f"Method not found: {method}")


# -------------------------------------------------------------------- handler


def lambda_handler(event, context):
    method = event.get("httpMethod", "")
    path = event.get("path", "/")
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    log(f"{method} {path}")

    if path == "/.well-known/oauth-protected-resource":
        return _json(200, get_oauth_protected_resource())

    if path == "/.well-known/oauth-authorization-server":
        return _json(200, get_auth_server_metadata())

    if path == "/oauth/authorize":
        return handle_oauth_authorize(event)

    if path == "/oauth/token":
        return handle_oauth_token(event)

    if method == "POST":
        token = headers.get("authorization")
        if not token:
            return _unauthorized()
        try:
            rpc = json.loads(event.get("body") or "{}")
        except ValueError:
            return _json(400, {"jsonrpc": "2.0", "id": None,
                               "error": {"code": -32700, "message": "Parse error"}})
        response = handle_mcp(rpc, token)
        if response is None:
            return {"statusCode": 202, "headers": {"Content-Type": "application/json"},
                    "body": ""}
        return _json(200, response)

    if method == "GET":
        return _unauthorized()

    return _json(405, {"title": "Method Not Allowed", "status": 405})


def _json(status, payload):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload),
    }


def _unauthorized():
    proxy = _proxy_url()
    return {
        "statusCode": 401,
        "headers": {
            "Content-Type": "application/json",
            "WWW-Authenticate": (
                f'Bearer resource_metadata="{proxy}/.well-known/oauth-protected-resource", '
                f'scope="{FIGMA_SCOPES}"'
            ),
        },
        "body": json.dumps({"title": "Authentication Required", "status": 401}),
    }

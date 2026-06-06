"""
hiclas repo maintenance agent.

Usage:
    python hiclas_agent.py
    python hiclas_agent.py "deploy the dev stack"
    python hiclas_agent.py "what lambdas exist in the repo?"

Environment variables required:
    ANTHROPIC_API_KEY   — Anthropic API key
    GITHUB_TOKEN        — GitHub personal access token (repo + workflow scopes)

Optional:
    HICLAS_REPO_DIR     — path to local clone of vipinable/hiclas
                          (defaults to the directory containing this script's parent)
"""

import json
import os
import sys
import time
from pathlib import Path

import anthropic
import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

REPO_OWNER = "vipinable"
REPO_NAME = "hiclas"
REPO_FULL = f"{REPO_OWNER}/{REPO_NAME}"
GITHUB_API = "https://api.github.com"

_script_dir = Path(__file__).resolve().parent
REPO_DIR = Path(os.environ.get("HICLAS_REPO_DIR", str(_script_dir.parent)))

MODEL = "claude-opus-4-8"

SYSTEM_PROMPT = f"""You are a maintenance agent for the **{REPO_FULL}** monorepo.
You have full knowledge of the project structure and can read files, trigger deployments,
check workflow status, and edit source code.

Repository layout (monorepo):
  apps/
    classifieds/             — CDK application (TypeScript) — the active dev stack
      bin/app.ts             — CDK entry point
      lib/classifieds-stack.ts — main stack (S3, Cognito, Lambda, SES, CloudFront)
      lambda/                — Python Lambda handlers
        handler.py           — listings API (CRUD)
        create_auth.py       — Cognito CUSTOM_AUTH OTP sender
        define_auth.py       — Cognito CUSTOM_AUTH challenge definer
        verify_auth.py       — Cognito CUSTOM_AUTH verifier
        email_ingest.py      — SES inbound email processor (stores to S3 by Cognito username)
      web/index.html         — SPA front-end
    legacy/                  — Original CDK stack (deployed by main.yml from main branch)
      src/                   — Python Lambdas, Jinja2 templates, static assets
  agent/
    hiclas_agent.py          — this file
  .github/workflows/
    deploy-classifieds.yml   — deploys apps/classifieds on push to dev or manual trigger
    setup-ses-email.yml      — one-shot SES identity verify + redeploy for classifieds
    main.yml                 — deploys apps/legacy on push to main

Key CDK context variables (apps/classifieds):
  sesFromEmail       — OTP sender address (e.g. noreply@highlyclassifieds.com)
  sesReceiveDomain   — domain for inbound SES email receiving (e.g. mail.highlyclassifieds.com)
  cfDomain           — custom CloudFront domain (e.g. dev.highlyclassifieds.com)
  cfCertArn          — ACM certificate ARN for cfDomain (must be in us-east-1)

Repo variables (Settings > Variables > Actions):
  SES_FROM_EMAIL, SES_RECEIVE_DOMAIN, CF_DOMAIN, CF_CERT_ARN
  All four are picked up automatically on every deploy-classifieds run.

Deployment:
  "deploy-classifieds" deploys apps/classifieds. Triggers on push to dev or manual.
  "setup-ses-email" verifies an SES identity and redeploys apps/classifieds.
  Both use AWS creds from secrets A_KEY / S_KEY, environment "all".

When you receive a task, use your tools to accomplish it completely, checking your work where possible.
"""


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _github_headers() -> dict:
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise RuntimeError("GITHUB_TOKEN environment variable is not set")
    return {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}


def list_files(directory: str = "") -> str:
    """List files in the local repo directory."""
    target = REPO_DIR / directory if directory else REPO_DIR
    if not target.exists():
        return f"ERROR: path does not exist: {target}"
    if target.is_file():
        return f"ERROR: {target} is a file, not a directory"
    entries = []
    for p in sorted(target.rglob("*")):
        rel = p.relative_to(REPO_DIR)
        parts = rel.parts
        if any(part in {"node_modules", "cdk.out", ".git", "__pycache__"} for part in parts):
            continue
        if p.is_file():
            entries.append(str(rel))
    return "\n".join(entries) if entries else "(empty directory)"


def read_file(path: str) -> str:
    """Read a file from the local repo."""
    target = REPO_DIR / path
    if not target.exists():
        return f"ERROR: file not found: {path}"
    if not target.is_file():
        return f"ERROR: not a file: {path}"
    try:
        return target.read_text(encoding="utf-8")
    except Exception as exc:
        return f"ERROR reading {path}: {exc}"


def write_file(path: str, content: str) -> str:
    """Write (or overwrite) a file in the local repo."""
    target = REPO_DIR / path
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"OK: wrote {len(content)} bytes to {path}"
    except Exception as exc:
        return f"ERROR writing {path}: {exc}"


def trigger_workflow(workflow_file: str, inputs: dict | None = None, ref: str = "dev") -> str:
    """Trigger a GitHub Actions workflow_dispatch event."""
    url = f"{GITHUB_API}/repos/{REPO_FULL}/actions/workflows/{workflow_file}/dispatches"
    payload: dict = {"ref": ref}
    if inputs:
        payload["inputs"] = inputs
    try:
        resp = requests.post(url, headers=_github_headers(), json=payload, timeout=30)
        if resp.status_code == 204:
            return f"OK: workflow '{workflow_file}' triggered on ref '{ref}'"
        return f"ERROR {resp.status_code}: {resp.text}"
    except Exception as exc:
        return f"ERROR: {exc}"


def list_workflow_runs(workflow_file: str | None = None, limit: int = 5) -> str:
    """List recent GitHub Actions workflow runs."""
    if workflow_file:
        url = f"{GITHUB_API}/repos/{REPO_FULL}/actions/workflows/{workflow_file}/runs"
    else:
        url = f"{GITHUB_API}/repos/{REPO_FULL}/actions/runs"
    try:
        resp = requests.get(
            url,
            headers=_github_headers(),
            params={"per_page": limit},
            timeout=30,
        )
        if resp.status_code != 200:
            return f"ERROR {resp.status_code}: {resp.text}"
        runs = resp.json().get("workflow_runs", [])
        lines = []
        for r in runs:
            lines.append(
                f"id={r['id']} workflow={r['name']} status={r['status']} "
                f"conclusion={r.get('conclusion','—')} branch={r['head_branch']} "
                f"started={r['created_at']}"
            )
        return "\n".join(lines) if lines else "(no runs found)"
    except Exception as exc:
        return f"ERROR: {exc}"


def get_workflow_run_logs(run_id: int) -> str:
    """Get a summary of a workflow run's job statuses."""
    url = f"{GITHUB_API}/repos/{REPO_FULL}/actions/runs/{run_id}/jobs"
    try:
        resp = requests.get(url, headers=_github_headers(), timeout=30)
        if resp.status_code != 200:
            return f"ERROR {resp.status_code}: {resp.text}"
        jobs = resp.json().get("jobs", [])
        lines = []
        for j in jobs:
            lines.append(
                f"job={j['name']} status={j['status']} conclusion={j.get('conclusion','—')}"
            )
            for step in j.get("steps", []):
                lines.append(
                    f"  step={step['name']} status={step['status']} "
                    f"conclusion={step.get('conclusion','—')}"
                )
        return "\n".join(lines) if lines else "(no jobs found)"
    except Exception as exc:
        return f"ERROR: {exc}"


def search_code(pattern: str, directory: str = "") -> str:
    """Search for a text pattern across repo files (grep-style)."""
    import subprocess

    target = str(REPO_DIR / directory) if directory else str(REPO_DIR)
    excludes = [
        "--exclude-dir=node_modules",
        "--exclude-dir=cdk.out",
        "--exclude-dir=.git",
        "--exclude-dir=__pycache__",
        "--exclude=*.js",
        "--exclude=*.d.ts",
        "--exclude=*.js.map",
    ]
    cmd = ["grep", "-r", "-n", "--include=*", *excludes, pattern, target]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        output = result.stdout.strip()
        if not output:
            return f"(no matches for '{pattern}')"
        # Shorten absolute paths to repo-relative
        output = output.replace(str(REPO_DIR) + "/", "")
        lines = output.splitlines()
        if len(lines) > 50:
            lines = lines[:50] + [f"... ({len(lines) - 50} more lines truncated)"]
        return "\n".join(lines)
    except subprocess.TimeoutExpired:
        return "ERROR: search timed out"
    except Exception as exc:
        return f"ERROR: {exc}"


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "list_files",
        "description": "List all tracked files in the local hiclas repository. "
                       "Pass a relative subdirectory to narrow the listing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "directory": {
                    "type": "string",
                    "description": "Relative path within the repo to list (e.g. 'app/lambda'). "
                                   "Leave empty to list the whole repo.",
                }
            },
            "required": [],
        },
    },
    {
        "name": "read_file",
        "description": "Read the full contents of a file in the local hiclas repository.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Repo-relative path to the file, e.g. 'app/lambda/email_ingest.py'.",
                }
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write (create or overwrite) a file in the local hiclas repository. "
                       "After writing, remind the user to commit and push the changes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Repo-relative path, e.g. 'app/lambda/email_ingest.py'.",
                },
                "content": {
                    "type": "string",
                    "description": "Full new file content (UTF-8).",
                },
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "search_code",
        "description": "Search for a text pattern across repo source files (grep). "
                       "Returns file paths, line numbers, and matching lines.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Text or regex pattern to search for.",
                },
                "directory": {
                    "type": "string",
                    "description": "Repo-relative subdirectory to restrict the search. "
                                   "Leave empty to search the whole repo.",
                },
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "trigger_workflow",
        "description": "Trigger a GitHub Actions workflow_dispatch event in the hiclas repo. "
                       "Use this to kick off deployments or one-shot setup workflows.",
        "input_schema": {
            "type": "object",
            "properties": {
                "workflow_file": {
                    "type": "string",
                    "description": "Workflow filename, e.g. 'deploy-dev.yml' or 'setup-ses-email.yml'.",
                },
                "inputs": {
                    "type": "object",
                    "description": "Optional key/value inputs for the workflow_dispatch trigger "
                                   "(must match the workflow's declared inputs).",
                    "additionalProperties": {"type": "string"},
                },
                "ref": {
                    "type": "string",
                    "description": "Branch or tag to run the workflow on. Defaults to 'dev'.",
                },
            },
            "required": ["workflow_file"],
        },
    },
    {
        "name": "list_workflow_runs",
        "description": "List recent GitHub Actions workflow runs for the hiclas repo. "
                       "Shows run id, status, conclusion, branch, and start time.",
        "input_schema": {
            "type": "object",
            "properties": {
                "workflow_file": {
                    "type": "string",
                    "description": "Filter to a specific workflow file, e.g. 'deploy-dev.yml'. "
                                   "Leave empty to list runs for all workflows.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of runs to return (default 5, max 20).",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_workflow_run_logs",
        "description": "Get a step-by-step status summary for a specific GitHub Actions workflow run.",
        "input_schema": {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "integer",
                    "description": "Numeric GitHub Actions run ID (from list_workflow_runs).",
                }
            },
            "required": ["run_id"],
        },
    },
]

TOOL_FNS = {
    "list_files": lambda inp: list_files(inp.get("directory", "")),
    "read_file": lambda inp: read_file(inp["path"]),
    "write_file": lambda inp: write_file(inp["path"], inp["content"]),
    "search_code": lambda inp: search_code(inp["pattern"], inp.get("directory", "")),
    "trigger_workflow": lambda inp: trigger_workflow(
        inp["workflow_file"],
        inp.get("inputs"),
        inp.get("ref", "dev"),
    ),
    "list_workflow_runs": lambda inp: list_workflow_runs(
        inp.get("workflow_file"),
        min(int(inp.get("limit", 5)), 20),
    ),
    "get_workflow_run_logs": lambda inp: get_workflow_run_logs(int(inp["run_id"])),
}


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

def run_agent(user_message: str) -> None:
    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": user_message}]

    print(f"\nUser: {user_message}\n")

    while True:
        response = client.messages.create(
            model=MODEL,
            max_tokens=8096,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
            thinking={"type": "adaptive"},
        )

        # Collect text output and tool uses from this response
        tool_uses = []
        text_parts = []

        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_uses.append(block)

        if text_parts:
            print("Agent:", " ".join(text_parts))

        # Append assistant message
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn" or not tool_uses:
            break

        # Execute tools and build tool_result blocks
        tool_results = []
        for tu in tool_uses:
            fn = TOOL_FNS.get(tu.name)
            if fn is None:
                result_text = f"ERROR: unknown tool '{tu.name}'"
            else:
                print(f"\n[tool] {tu.name}({json.dumps(tu.input, ensure_ascii=False)[:200]})")
                try:
                    result_text = fn(tu.input)
                except Exception as exc:
                    result_text = f"ERROR: {exc}"
                # Truncate very long tool outputs to avoid blowing the context window
                if len(result_text) > 8000:
                    result_text = result_text[:8000] + "\n...(truncated)"
                print(f"[tool result] {result_text[:300]}{'...' if len(result_text) > 300 else ''}")

            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": result_text,
                }
            )

        messages.append({"role": "user", "content": tool_results})


# ---------------------------------------------------------------------------
# Interactive REPL
# ---------------------------------------------------------------------------

def repl() -> None:
    print("hiclas maintenance agent  (type 'exit' or Ctrl-D to quit)")
    print(f"Repo: {REPO_DIR}")
    print("=" * 60)
    while True:
        try:
            user_input = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break
        if not user_input:
            continue
        if user_input.lower() in {"exit", "quit", "q"}:
            print("Bye.")
            break
        run_agent(user_input)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) > 1:
        run_agent(" ".join(sys.argv[1:]))
    else:
        repl()

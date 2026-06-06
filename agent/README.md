# hiclas maintenance agent

A Claude-powered agent for maintaining and operating the hiclas repo.

## Setup

```bash
cd agent
pip install -r requirements.txt

export ANTHROPIC_API_KEY=sk-ant-...
export GITHUB_TOKEN=ghp_...        # needs repo + workflow scopes
```

## Usage

**Interactive REPL:**
```bash
python hiclas_agent.py
```

**One-shot command:**
```bash
python hiclas_agent.py "deploy the dev stack"
python hiclas_agent.py "what does email_ingest.py do?"
python hiclas_agent.py "trigger setup-ses-email with ses_receive_domain=mail.highlyclassifieds.com"
python hiclas_agent.py "show me the last 3 deploy-dev runs"
```

## Capabilities

| Tool | What it does |
|---|---|
| `list_files` | List all source files in the repo |
| `read_file` | Read any file in the repo |
| `write_file` | Create or overwrite a file |
| `search_code` | Grep across source files |
| `trigger_workflow` | Fire a `workflow_dispatch` event (deploy-dev, setup-ses-email) |
| `list_workflow_runs` | Show recent CI runs and their status |
| `get_workflow_run_logs` | Step-by-step job status for a specific run |

## Example prompts

- "Deploy the dev stack now"
- "Set up SES email receiving for mail.highlyclassifieds.com"
- "Add a new Lambda function that processes listings nightly"
- "Why did the last deployment fail?"
- "Show me all files that reference the Cognito user pool"
- "What environment variables does the email_ingest Lambda need?"

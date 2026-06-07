# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Overview

Nx monorepo containing AWS CDK infrastructure for a classifieds application, deployed independently to three environments:
- `classifieds` — dev environment (us-east-1), deployed on push to `dev` branch
- `classifieds-eu` — EU production (eu-west-1), deployed on tag `rel-eu*`
- `classifieds-us` — US production (us-east-1), deployed on tag `rel-us*`
- `legacy` — older reference implementation, not actively deployed

## Common Commands

```bash
# Type-check all apps
npm run build

# Type-check a single app
nx run classifieds:build       # or classifieds-eu, classifieds-us

# CDK synth (generate CloudFormation templates)
nx run classifieds:synth

# Deploy (requires AWS credentials in environment)
npm run deploy:classifieds     # dev
npm run deploy:eu              # EU production
npm run deploy:us              # US production
npm run deploy:production      # EU + US together
```

## Architecture

Each CDK app (`classifieds`, `classifieds-eu`, `classifieds-us`) is structurally identical and self-contained:

```
apps/<app>/
├── bin/app.ts          # CDK App entry — sets stack ID from APP_NAME + ENV_NAME env vars
├── lib/classifieds-stack.ts   # All AWS resources defined here
├── lambda/             # Python Lambda functions (independent copy per region)
│   ├── handler.py      # API Lambda (DynamoDB CRUD for listings)
│   ├── email_ingest.py # SES inbound email processing
│   ├── create_auth.py  # Cognito custom auth triggers
│   ├── define_auth.py
│   └── verify_auth.py
├── web/index.html      # Main SPA frontend (independent copy per region)
└── pages/              # Static pages: about, faq, terms (independent copy per region)
```

### Stack ID and Resource Naming

`bin/app.ts` constructs the stack ID as `${APP_NAME}-${ENV_NAME}` (e.g. `classifieds-dev`, `classifieds-eu`, `classifieds-us`). Inside `lib/classifieds-stack.ts`, use `this.stackName` for any physical AWS resource names that must be unique per region/account (e.g. SES ReceiptRuleSet names, CloudFront distribution comments). This prevents `CREATE_FAILED` conflicts when multiple stacks deploy to the same region.

### Key AWS Resources (per stack)

- **S3**: website bucket + assets bucket, both with CloudFront OAC
- **CloudFront**: distribution serving web/, pages/, and API via Lambda Function URL
- **DynamoDB**: listings table
- **Lambda**: main API handler + email ingest + Cognito auth triggers
- **SES**: receipt rule set (`${stackName}-inbound`) + receipt rule routing to Lambda
- **Cognito**: user pool + client with custom auth flow
- **SSM**: parameter storing CloudFront domain name

### CDK Deployment Context

Context variables passed at deploy time (via `--context` flags or GitHub Actions environment variables):
- `sesFromEmail` — verified SES sender address
- `sesReceiveDomain` — domain for SES inbound receiving
- `cfDomain` — custom CloudFront domain (optional)
- `cfCertArn` — ACM certificate ARN for custom domain (optional)

## CI/CD

Three workflows in `.github/workflows/`:
- `deploy-classifieds.yml` — triggers on push to `dev`; deploys `classifieds` stack
- `deploy-production.yml` — triggers on `rel-eu*`/`rel-us*` tags or manual dispatch; uses `nx run classifieds-eu:deploy` / `nx run classifieds-us:deploy`
- `setup-ses-email.yml` — manual one-shot workflow for SES domain/email verification

GitHub environments (`dev`, `eu`, `us`) hold scoped secrets (`A_KEY`, `S_KEY`) and variables (`APP_NAME`, `ENV_NAME`, `SES_FROM_EMAIL`, `SES_RECEIVE_DOMAIN`, `CF_DOMAIN`, `CF_CERT_ARN`).

## Regional Independence

`lambda/`, `web/`, and `pages/` are physically copied into each app directory so regions can diverge independently. When making a change intended for all regions, apply it to all three copies. When making a region-specific change, modify only the relevant app's copy.

## Agent Tooling

`agent/hiclas_agent.py` is a Claude-powered maintenance agent for interactive repo operations (read/write files, trigger workflows, search code, view logs). See `agent/README.md` for setup and usage.

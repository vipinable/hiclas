# Classifieds (fresh minimal build)

A clean, self-contained classifieds application built with the same tech
stack as `hiclas`: **AWS CDK + CloudFront + Lambda + S3 + DynamoDB**.

## API

| Method | Path                 | Description              |
|--------|----------------------|--------------------------|
| GET    | `/api/listings`      | List listings (newest)   |
| POST   | `/api/listings`      | Create a listing         |
| GET    | `/api/listings/{id}` | Fetch one listing        |

## Deploy

Deployment uses the same method as the main pipeline: a GitHub Actions
workflow (`.github/workflows/deploy-dev.yml`) runs `cdk deploy` on every
push to the `dev` branch.

Local deploy:

```bash
cd app
npm install
npx cdk deploy
```

The stack name is `classifieds-dev`, kept separate from the existing
`hiclas-dev` stack so the two can coexist in the same account.

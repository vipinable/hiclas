# vocabtrainer

A tiny web app for learning vocabulary by playing a word-match game.

## Architecture

```
Browser ──▶ S3 static site (index.html)
        ──▶ Lambda Function URL ──▶ Lambda (handler.py)
                                       │
                                       ▼
                               S3 vocab bucket
                               (word,meaning CSVs)
```

- **Vocab bucket** — holds CSV files in `word,meaning` format. A seed
  `default.csv` is uploaded on first deploy from `seed/`.
- **Game Lambda** — exposes a Function URL with three routes: `/lists`,
  `/round`, `/answer`. It loads a CSV from the vocab bucket, randomly
  picks a target word, and builds a multiple-choice round.
- **Site bucket** — a public S3 website hosting `web/index.html`, the
  single-page game UI.

## Deploy

```
APP_NAME=vocabtrainer ENV_NAME=dev npx nx run vocabtrainer:deploy
```

The CDK outputs include:
- `SiteUrl` — open this to play.
- `ApiUrl` — paste this into the page when prompted.
- `VocabBucketName` — upload more `word,meaning` CSVs here to add lists.

## Adding word lists

Upload any `*.csv` into the vocab bucket with the shape:

```
word,meaning
ephemeral,lasting for a very short time
ubiquitous,present everywhere at the same time
```

The list will appear in the dropdown on the next page load.

## API

- `GET /lists` — returns available CSV keys.
- `GET /round?list=<key>&n=4` — returns a target word + `n` meaning
  choices (one correct, the rest distractors from the same list).
- `POST /answer` — body `{round_id, chosen_meaning}`. Returns
  `{is_correct, correct_meaning, points_awarded}`.

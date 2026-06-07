# vocabtrainer

A tiny web app for learning vocabulary by playing a word-match game.

## Architecture

```
Browser ──▶ Lambda Function URL ──▶ Lambda (handler.py)
                                       │  GET /              -> index.html
                                       │  GET /lists         -> available CSV keys
                                       │  GET /board?list=…  -> N words + N shuffled meanings
                                       │  POST /answer       -> grade {list,word,chosen_meaning}
                                       ▼
                                S3 vocab bucket
                                (word,meaning CSVs)
```

The Lambda is the entire app: it serves the HTML page on `GET /` and the
JSON API on the other routes. S3 holds only the CSV word lists. A seed
`default.csv` is uploaded on the first deploy from `seed/`.

## How the game works

The page shows two columns — **Words** on the left, **Meanings** on the
right, each shuffled independently. Click a word to select it, then
click the meaning you think matches. Correct matches lock the pair in
place; wrong picks flash red and deselect. Score = number of correct
matches; attempts counts every meaning-click.

## Deploy

```
APP_NAME=vocabtrainer ENV_NAME=dev npx nx run vocabtrainer:deploy
```

Outputs:
- `AppUrl` — Lambda Function URL; open it in a browser to play.
- `VocabBucketName` — upload more `word,meaning` CSVs here to add lists.

## Adding word lists

Upload any `*.csv` into the vocab bucket with the shape:

```
word,meaning
ephemeral,lasting for a very short time
ubiquitous,present everywhere at the same time
```

The list appears in the dropdown on the next page load.

## API

- `GET /lists` — returns available CSV keys.
- `GET /board?list=<key>&n=6` — returns `{list, words[], meanings[]}`,
  N pairs sampled from the list with the meanings shuffled independently
  from the words.
- `POST /answer` — body `{list, word, chosen_meaning}`. Returns
  `{is_correct, correct_meaning, points_awarded}`.

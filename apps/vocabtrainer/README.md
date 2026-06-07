# vocabtrainer

A tiny web app for learning vocabulary by playing a word-match game.

## Architecture

```
Browser ──▶ Lambda Function URL ──▶ Lambda (handler.py)
                                       │  GET /              -> index.html
                                       │  GET /lists         -> available CSV keys
                                       │  GET /board?list=…  -> N words + N shuffled meanings
                                       │  POST /answer       -> grade {list,word,chosen_meaning}
                                       │  POST /words        -> append {list,word,meaning} to CSV
                                       ▼
                                S3 vocab bucket
                                (word,meaning CSVs)
```

The Lambda is the entire app: it serves the HTML page on `GET /` and the
JSON API on the other routes. S3 holds only the CSV word lists. A seed
`default.csv` is uploaded on the first deploy from `seed/`.

## How the game works

Open the URL and the settings panel appears: pick a word list and how
many pairs you want on the board (2–50), then click **Start game**.

The board shows two columns — **Words** on the left, **Meanings** on
the right, each shuffled independently. Click a word to select it,
then click the meaning you think matches.

- **Correct** picks pulse green and are removed; the server hands back
  a new word+meaning that hasn't been on the board yet. The new word
  slots into the freed word position, and its meaning is inserted at a
  random position in the meanings column. Play continues indefinitely.
- **Wrong** picks flash red and deselect the word.
- The board shrinks once the word list is exhausted; the game ends
  automatically when the last pair is matched.
- **End game** stops the round at any time and shows your final score,
  accuracy, and best streak. You can change the board size on the
  settings panel and start a new game whenever you like.

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

## Adding words from the UI

Below the game settings there's an **Add a word** panel. Pick the list
in the settings dropdown, type the word and its meaning, and click
**Add word**. The Lambda re-serialises the CSV (with proper quoting,
so commas in meanings survive) and writes it back to the same key in
S3. Duplicate words (case-insensitive) are rejected with a 409.

## API

- `GET /lists` — returns available CSV keys.
- `GET /board?list=<key>&n=6` — returns `{list, words[], meanings[]}`,
  N pairs sampled from the list with the meanings shuffled independently
  from the words.
- `POST /answer` — body `{list, word, chosen_meaning, current_words?}`.
  Returns `{is_correct, correct_meaning, points_awarded, replacement}`.
  When the pick is correct and `current_words` lists everything still
  on the board, `replacement` is a `{word, meaning}` from the rest of
  the list (or `null` once the list is exhausted).
- `POST /words` — body `{list, word, meaning}`. Appends a new row to
  the CSV in S3 (creating it if missing). Returns
  `{added, list, total}`. Rejects duplicates with 409.

"""VocabTrainer game API + page server.

The Lambda is fronted by a single Function URL and serves both the web UI
and the JSON API:

    GET  /                          -> index.html (the game UI)
    GET  /lists                     -> list available CSV keys in the vocab bucket
    GET  /round?list=<key>&n=4      -> one match round: a target word + n meaning choices
    POST /answer                    -> grade the user's pick, return is_correct + the right meaning

Round shape:
    {
        "list": "default.csv",
        "round_id": "default.csv|ephemeral",
        "word": "ephemeral",
        "choices": [
            {"id": "0", "meaning": "lasting for a very short time"},
            {"id": "1", "meaning": "present everywhere"},
            ...
        ]
    }

The round_id is `<list>|<word>` — the client echoes it back with the
chosen meaning so we can grade without server-side session state.
"""

import csv
import io
import json
import os
import random
from urllib.parse import parse_qs, urlparse

import boto3

s3 = boto3.client("s3")

BUCKET = os.environ["VOCAB_BUCKET"]
DEFAULT_KEY = os.environ.get("DEFAULT_VOCAB_KEY", "default.csv")

# Page is shipped alongside the handler in the Lambda bundle.
_PAGE_PATH = os.path.join(os.path.dirname(__file__), "index.html")
with open(_PAGE_PATH, "r", encoding="utf-8") as _f:
    INDEX_HTML = _f.read()


def _json(status, body):
    return {
        "statusCode": status,
        "headers": {
            "content-type": "application/json",
            "access-control-allow-origin": "*",
            "access-control-allow-headers": "*",
            "access-control-allow-methods": "GET,POST,OPTIONS",
        },
        "body": json.dumps(body),
    }


def _html(status, body):
    return {
        "statusCode": status,
        "headers": {
            "content-type": "text/html; charset=utf-8",
            "cache-control": "no-store",
        },
        "body": body,
    }


def _load_vocab(key):
    """Load a CSV from S3 into a list of (word, meaning) tuples."""
    obj = s3.get_object(Bucket=BUCKET, Key=key)
    text = obj["Body"].read().decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text))
    pairs = []
    for row in reader:
        if len(row) < 2:
            continue
        word = row[0].strip()
        meaning = row[1].strip()
        if not word or not meaning:
            continue
        if word.lower() == "word" and meaning.lower() == "meaning":
            continue
        pairs.append((word, meaning))
    return pairs


def _list_vocab_keys():
    keys = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BUCKET):
        for item in page.get("Contents", []) or []:
            k = item["Key"]
            if k.lower().endswith(".csv"):
                keys.append(k)
    return sorted(keys)


def _pick_round(pairs, n_choices):
    if len(pairs) < 2:
        raise ValueError("vocab list needs at least 2 entries")
    n_choices = max(2, min(n_choices, len(pairs)))
    target_idx = random.randrange(len(pairs))
    word, correct_meaning = pairs[target_idx]

    distractor_pool = list({m for i, (_, m) in enumerate(pairs) if i != target_idx and m != correct_meaning})
    random.shuffle(distractor_pool)
    distractors = distractor_pool[: n_choices - 1]

    choices_meanings = distractors + [correct_meaning]
    random.shuffle(choices_meanings)
    choices = [{"id": str(i), "meaning": m} for i, m in enumerate(choices_meanings)]
    return word, correct_meaning, choices


def _route(method, path, qs, body):
    # Strip optional /api prefix so both `/api/round` and `/round` work.
    if path.startswith("/api/"):
        path = path[4:]
    elif path == "/api":
        path = "/"

    if method == "OPTIONS":
        return _json(204, {})

    if method == "GET" and path in ("/", "/index.html"):
        return _html(200, INDEX_HTML)

    if method == "GET" and path == "/lists":
        return _json(200, {"lists": _list_vocab_keys()})

    if method == "GET" and path == "/round":
        key = (qs.get("list") or [DEFAULT_KEY])[0]
        try:
            n = int((qs.get("n") or ["4"])[0])
        except ValueError:
            n = 4
        pairs = _load_vocab(key)
        if len(pairs) < 2:
            return _json(400, {"error": f"vocab list '{key}' needs at least 2 entries"})
        word, correct_meaning, choices = _pick_round(pairs, n)
        return _json(200, {
            "list": key,
            "round_id": f"{key}|{word}",
            "word": word,
            "choices": choices,
            "correct_meaning": correct_meaning,
        })

    if method == "POST" and path == "/answer":
        try:
            data = json.loads(body or "{}")
        except json.JSONDecodeError:
            return _json(400, {"error": "invalid JSON body"})
        round_id = data.get("round_id") or ""
        chosen_meaning = (data.get("chosen_meaning") or "").strip()
        if "|" not in round_id or not chosen_meaning:
            return _json(400, {"error": "round_id and chosen_meaning are required"})
        key, word = round_id.split("|", 1)
        pairs = _load_vocab(key)
        correct = next((m for w, m in pairs if w == word), None)
        if correct is None:
            return _json(404, {"error": f"word '{word}' not found in '{key}'"})
        is_correct = chosen_meaning == correct
        return _json(200, {
            "is_correct": is_correct,
            "correct_meaning": correct,
            "points_awarded": 1 if is_correct else 0,
        })

    return _json(404, {"error": f"no route for {method} {path}"})


def handler(event, _context):
    method = (
        event.get("requestContext", {})
        .get("http", {})
        .get("method")
        or event.get("httpMethod")
        or "GET"
    )
    raw_path = (
        event.get("rawPath")
        or event.get("path")
        or "/"
    )
    parsed = urlparse(raw_path)
    path = parsed.path or "/"

    raw_qs = event.get("rawQueryString") or parsed.query or ""
    qs = parse_qs(raw_qs)

    body = event.get("body") or ""
    if event.get("isBase64Encoded") and body:
        import base64
        body = base64.b64decode(body).decode("utf-8")

    try:
        return _route(method, path, qs, body)
    except s3.exceptions.NoSuchKey:
        return _json(404, {"error": "vocab list not found"})
    except Exception as exc:  # noqa: BLE001
        return _json(500, {"error": str(exc)})

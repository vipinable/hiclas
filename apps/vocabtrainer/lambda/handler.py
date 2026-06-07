"""VocabTrainer game API + page server.

The Lambda is fronted by a single Function URL and serves both the web UI
and the JSON API:

    GET  /                            -> index.html (the game UI)
    GET  /lists                       -> list available CSV keys
    GET  /board?list=<key>&n=6        -> a match-board: N words + N meanings
                                         shuffled independently
    POST /answer                      -> grade a {list, word, chosen_meaning}
                                         pick. When correct and the request
                                         includes current_words[], the
                                         response also carries a replacement
                                         {word, meaning} drawn from the rest
                                         of the list so the board can refill
                                         in place and play continues
                                         indefinitely.

Board shape:
    {
        "list": "default.csv",
        "words":   ["ephemeral", "ubiquitous", ...],     # display order
        "meanings": ["present everywhere", "lasting...", ...]  # shuffled
    }
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


def _pick_board(pairs, n):
    """Pick n distinct pairs and return (words_in_display_order, meanings_shuffled)."""
    if len(pairs) < 2:
        raise ValueError("vocab list needs at least 2 entries")
    n = max(2, min(n, len(pairs)))
    sample = random.sample(pairs, n)
    random.shuffle(sample)  # words display order
    words = [w for w, _ in sample]
    meanings = [m for _, m in sample]
    random.shuffle(meanings)  # independently shuffled column
    return words, meanings


def _route(method, path, qs, body):
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

    if method == "GET" and path == "/board":
        key = (qs.get("list") or [DEFAULT_KEY])[0]
        try:
            n = int((qs.get("n") or ["6"])[0])
        except ValueError:
            n = 6
        pairs = _load_vocab(key)
        if len(pairs) < 2:
            return _json(400, {"error": f"vocab list '{key}' needs at least 2 entries"})
        words, meanings = _pick_board(pairs, n)
        return _json(200, {
            "list": key,
            "words": words,
            "meanings": meanings,
        })

    if method == "POST" and path == "/answer":
        try:
            data = json.loads(body or "{}")
        except json.JSONDecodeError:
            return _json(400, {"error": "invalid JSON body"})
        key = (data.get("list") or "").strip() or DEFAULT_KEY
        word = (data.get("word") or "").strip()
        chosen_meaning = (data.get("chosen_meaning") or "").strip()
        current_words = data.get("current_words") or []
        if not word or not chosen_meaning:
            return _json(400, {"error": "word and chosen_meaning are required"})
        pairs = _load_vocab(key)
        correct = next((m for w, m in pairs if w == word), None)
        if correct is None:
            return _json(404, {"error": f"word '{word}' not found in '{key}'"})
        is_correct = chosen_meaning == correct
        response = {
            "is_correct": is_correct,
            "correct_meaning": correct,
            "points_awarded": 1 if is_correct else 0,
            "replacement": None,
        }
        if is_correct:
            excluded = set(current_words)
            candidates = [(w, m) for w, m in pairs if w not in excluded]
            if candidates:
                rw, rm = random.choice(candidates)
                response["replacement"] = {"word": rw, "meaning": rm}
        return _json(200, response)

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

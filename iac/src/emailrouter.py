"""Routes incoming SES emails into emails/<user>/ in the uploads bucket.

The SES receipt rule writes the raw email to s3://<BUCKET_STORE>/emails/incoming/<messageId>
via an S3 action, then invokes this function via a Lambda action. The handler copies
the staged object to emails/<user>/<messageId> (one copy per recipient) and removes the
staging object.
"""

import os
import re

import boto3
from botocore.exceptions import ClientError

s3 = boto3.client("s3")

BUCKET = os.environ["BUCKET_STORE"]
STAGING_PREFIX = os.environ.get("STAGING_PREFIX", "emails/incoming/")
DEST_PREFIX = os.environ.get("DEST_PREFIX", "emails/")

_UNSAFE_USER_CHARS = re.compile(r"[^a-z0-9._-]")


def sanitize_user(local_part: str) -> str:
    user = _UNSAFE_USER_CHARS.sub("-", local_part.lower().strip())
    user = user.strip(".-_")
    return (user or "unknown")[:64]


def handler(event, _context):
    record = event["Records"][0]
    ses = record["ses"]
    message_id = ses["mail"]["messageId"]
    recipients = ses["receipt"]["recipients"]

    src_key = f"{STAGING_PREFIX}{message_id}"

    try:
        s3.head_object(Bucket=BUCKET, Key=src_key)
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey"):
            print(f"Source {src_key} not found, skipping (already routed?)")
            return {"disposition": "CONTINUE"}
        raise

    for rcpt in recipients:
        user = sanitize_user(rcpt.split("@", 1)[0])
        dst_key = f"{DEST_PREFIX}{user}/{message_id}"
        s3.copy_object(
            Bucket=BUCKET,
            Key=dst_key,
            CopySource={"Bucket": BUCKET, "Key": src_key},
            MetadataDirective="COPY",
        )
        print(f"Routed {src_key} -> {dst_key}")

    s3.delete_object(Bucket=BUCKET, Key=src_key)
    return {"disposition": "CONTINUE"}

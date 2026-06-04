"""SES inbound email processor — stores plain-text body to emails/<cognito-username>/ in S3.

Flow:
  1. SES S3 action writes raw email to emails/raw/<messageId>
  2. This Lambda reads it, parses the text/plain part, and saves
     emails/<cognito-username>/<messageId>.txt (falls back to emails/_unknown/)
  3. The raw staging copy is deleted.

Recipient routing:
  - <sub-uuid>@<SES_RECEIVE_DOMAIN>  → sub UUID used to look up Cognito username directly
    (these are reply-to addresses minted by the interest relay in handler.py)
  - any-other-address                → look up Cognito user whose email attribute matches
"""
import email
import email.policy
import os

import boto3

s3 = boto3.client('s3')
cognito_idp = boto3.client('cognito-idp')

UPLOAD_BUCKET = os.environ.get('UPLOAD_BUCKET', '')
USER_POOL_ID = os.environ.get('USER_POOL_ID', '')
SES_RECEIVE_DOMAIN = os.environ.get('SES_RECEIVE_DOMAIN', '').lower()
RAW_PREFIX = 'emails/raw/'


def _username_for_email(address):
    """Return the Cognito username whose email attribute matches, or None."""
    if not USER_POOL_ID or not address:
        return None
    try:
        resp = cognito_idp.list_users(
            UserPoolId=USER_POOL_ID,
            Filter=f'email = "{address}"',
            Limit=1,
        )
        users = resp.get('Users', [])
        if users:
            return users[0]['Username']
    except Exception as exc:
        print(f'[EmailIngest] WARNING: Cognito email lookup failed for {address}: {exc}')
    return None


def _username_for_sub(sub):
    """Return the Cognito username whose sub attribute matches, or None.

    Used to resolve the masked reply-to addresses minted by the interest relay:
      <cognito-sub>@<SES_RECEIVE_DOMAIN>
    """
    if not USER_POOL_ID or not sub:
        return None
    try:
        resp = cognito_idp.list_users(
            UserPoolId=USER_POOL_ID,
            Filter=f'sub = "{sub}"',
            Limit=1,
        )
        users = resp.get('Users', [])
        if users:
            return users[0]['Username']
    except Exception as exc:
        print(f'[EmailIngest] WARNING: Cognito sub lookup failed for {sub}: {exc}')
    return None


def _resolve_folder(recipient):
    """Map a recipient address to the S3 folder (Cognito username) for storage.

    Masked relay addresses (<sub>@<SES_RECEIVE_DOMAIN>) are resolved by sub.
    All other addresses are resolved by matching the Cognito email attribute.
    Falls back to '_unknown' if no user is found.
    """
    local, _, domain = recipient.rpartition('@')
    if SES_RECEIVE_DOMAIN and domain == SES_RECEIVE_DOMAIN:
        username = _username_for_sub(local)
        print(f'[EmailIngest] relay address {recipient} -> sub={local} -> cognito={username or "(none)"}')
    else:
        username = _username_for_email(recipient)
        print(f'[EmailIngest] address {recipient} -> cognito={username or "(none)"}')
    return username if username else '_unknown'


def _extract_text(raw_bytes):
    """Return the text/plain body of the email, or a fallback representation."""
    msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)

    headers = '\n'.join(
        f'{h}: {msg[h]}'
        for h in ('Date', 'From', 'To', 'Subject')
        if msg[h]
    )

    plain = None
    for part in msg.walk():
        if part.get_content_type() == 'text/plain' and not part.get_filename():
            plain = part.get_content()
            break

    if plain is None:
        plain = msg.get_body()
        plain = plain.get_content() if plain else '(no readable body)'

    return f'{headers}\n\n{plain.strip()}\n'


def handler(event, context):
    for record in event.get('Records', []):
        ses_msg = record.get('ses', {})
        mail = ses_msg.get('mail', {})
        receipt = ses_msg.get('receipt', {})

        message_id = mail.get('messageId', '')
        recipients = receipt.get('recipients', [])
        raw_key = RAW_PREFIX + message_id

        print(f'[EmailIngest] messageId={message_id} recipients={recipients}')

        if not message_id or not recipients:
            print('[EmailIngest] WARNING: missing messageId or recipients — skipping')
            continue

        try:
            raw_obj = s3.get_object(Bucket=UPLOAD_BUCKET, Key=raw_key)
            raw_bytes = raw_obj['Body'].read()
        except Exception as exc:
            print(f'[EmailIngest] ERROR: could not fetch raw email {raw_key}: {exc}')
            raise

        text_body = _extract_text(raw_bytes)

        for recipient in recipients:
            folder = _resolve_folder(recipient.lower())
            dest_key = f'emails/{folder}/{message_id}.txt'
            print(f'[EmailIngest] storing -> {dest_key}')
            try:
                s3.put_object(
                    Bucket=UPLOAD_BUCKET,
                    Key=dest_key,
                    Body=text_body.encode('utf-8'),
                    ContentType='text/plain; charset=utf-8',
                )
                print(f'[EmailIngest] stored -> {dest_key}')
            except Exception as exc:
                print(f'[EmailIngest] ERROR writing {dest_key}: {exc}')
                raise

        try:
            s3.delete_object(Bucket=UPLOAD_BUCKET, Key=raw_key)
            print(f'[EmailIngest] cleaned up raw/{message_id}')
        except Exception as exc:
            print(f'[EmailIngest] WARNING: could not delete raw key {raw_key}: {exc}')

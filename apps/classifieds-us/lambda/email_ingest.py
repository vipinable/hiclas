"""SES inbound email processor.

For each inbound message:
  1. Fetch the raw email from emails/raw/<messageId>
  2. Resolve the recipient masked address (<sub>@<SES_RECEIVE_DOMAIN>) to the
     real Cognito user and store the email under emails/<sub>/<messageId>.txt
  3. Forward the email to the recipient's real inbox, replacing the sender's
     real address with their own masked address so the conversation stays private.
     Reply-To is set to <sender-sub>@<SES_RECEIVE_DOMAIN> so the recipient can
     reply back through the relay.
  4. Delete the raw staging copy.

S3 path:  emails/<cognito-sub-uuid>/<messageId>.txt
"""
import email
import email.policy
import email.utils
import os

import boto3

s3 = boto3.client('s3')
cognito_idp = boto3.client('cognito-idp')
ses = boto3.client('ses')

UPLOAD_BUCKET = os.environ.get('UPLOAD_BUCKET', '')
USER_POOL_ID = os.environ.get('USER_POOL_ID', '')
SES_RECEIVE_DOMAIN = os.environ.get('SES_RECEIVE_DOMAIN', '').lower()
SES_FROM_EMAIL = os.environ.get('SES_FROM_EMAIL', '')
RAW_PREFIX = 'emails/raw/'


# ---------------------------------------------------------------------------
# Cognito helpers
# ---------------------------------------------------------------------------

def _lookup_by_sub(sub):
    """Return (username, email) for the user with the given Cognito sub, or (None, None)."""
    if not USER_POOL_ID or not sub:
        return None, None
    try:
        resp = cognito_idp.list_users(
            UserPoolId=USER_POOL_ID,
            Filter=f'sub = "{sub}"',
            Limit=1,
        )
        users = resp.get('Users', [])
        if not users:
            return None, None
        u = users[0]
        username = u['Username']
        email_val = next(
            (a['Value'] for a in u.get('Attributes', []) if a['Name'] == 'email'),
            username,  # username IS the email in this pool
        )
        return username, email_val
    except Exception as exc:
        print(f'[EmailIngest] WARNING: lookup by sub {sub!r} failed: {exc}')
    return None, None


def _lookup_by_email(address):
    """Return (username, sub) for the user with the given email attribute, or (None, None)."""
    if not USER_POOL_ID or not address:
        return None, None
    try:
        resp = cognito_idp.list_users(
            UserPoolId=USER_POOL_ID,
            Filter=f'email = "{address}"',
            Limit=1,
        )
        users = resp.get('Users', [])
        if not users:
            return None, None
        u = users[0]
        username = u['Username']
        sub = next(
            (a['Value'] for a in u.get('Attributes', []) if a['Name'] == 'sub'),
            None,
        )
        return username, sub
    except Exception as exc:
        print(f'[EmailIngest] WARNING: lookup by email {address!r} failed: {exc}')
    return None, None


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def _resolve(recipient):
    """Map a recipient address to (folder, real_email).

    folder      — Cognito sub UUID used as the S3 path component
    real_email  — the user's real inbox address for forwarding; None if unknown

    Masked relay addresses (<sub>@<SES_RECEIVE_DOMAIN>) are routed by extracting
    the sub directly from the local part.  All other addresses fall back to a
    Cognito email-attribute lookup.
    """
    local, _, domain = recipient.rpartition('@')
    if SES_RECEIVE_DOMAIN and domain == SES_RECEIVE_DOMAIN:
        sub = local
        _, real_email = _lookup_by_sub(sub)
        print(f'[EmailIngest] relay {recipient} → sub={sub} email={real_email or "(none)"}')
        return sub, real_email
    else:
        _, sub = _lookup_by_email(recipient)
        if sub:
            print(f'[EmailIngest] email {recipient} → sub={sub}')
            return sub, recipient
        print(f'[EmailIngest] email {recipient} → no Cognito user → _unknown')
        return '_unknown', None


# ---------------------------------------------------------------------------
# Email parsing
# ---------------------------------------------------------------------------

def _extract_text(msg):
    """Return the plain-text representation of a parsed email message."""
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
        body = msg.get_body()
        plain = body.get_content() if body else '(no readable body)'
    return f'{headers}\n\n{plain.strip()}\n'


# ---------------------------------------------------------------------------
# Forwarding
# ---------------------------------------------------------------------------

def _forward(text_body, msg, recipient_real_email):
    """Forward an inbound relay email to the recipient's real inbox.

    - From:     HighlyClassifieds <SES_FROM_EMAIL>  (verified SES sender)
    - Reply-To: <sender-sub>@<SES_RECEIVE_DOMAIN>   (sender's masked address)
    - To:       recipient's real email address
    - Body:     original text with the sender's real email replaced by their
                masked address, so no real addresses are leaked in the body.
    """
    if not SES_FROM_EMAIL or not SES_RECEIVE_DOMAIN or not recipient_real_email:
        return

    # Resolve the sender's real email and their masked reply-to address
    from_raw = str(msg.get('From', ''))
    _, sender_real = email.utils.parseaddr(from_raw)
    sender_real = sender_real.lower().strip()

    _, sender_sub = _lookup_by_email(sender_real) if sender_real else (None, None)
    sender_masked = f'{sender_sub}@{SES_RECEIVE_DOMAIN}' if sender_sub else SES_FROM_EMAIL

    # Mask the sender's real email in the stored body text before forwarding
    fwd_body = text_body
    if sender_real and sender_sub:
        fwd_body = fwd_body.replace(sender_real, sender_masked)

    subject = str(msg.get('Subject', '(no subject)'))

    try:
        ses.send_email(
            Source=f'HighlyClassifieds <{SES_FROM_EMAIL}>',
            Destination={'ToAddresses': [recipient_real_email]},
            ReplyToAddresses=[sender_masked],
            Message={
                'Subject': {'Data': subject, 'Charset': 'UTF-8'},
                'Body': {'Text': {'Data': fwd_body, 'Charset': 'UTF-8'}},
            },
        )
        print(f'[EmailIngest] forwarded → {recipient_real_email} (reply_to={sender_masked})')
    except Exception as exc:
        print(f'[EmailIngest] WARNING: forward to {recipient_real_email} failed: {exc}')


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------

def handler(event, context):
    for record in event.get('Records', []):
        ses_event = record.get('ses', {})
        mail = ses_event.get('mail', {})
        receipt = ses_event.get('receipt', {})

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
            print(f'[EmailIngest] ERROR: could not fetch {raw_key}: {exc}')
            raise

        msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)
        text_body = _extract_text(msg)

        for recipient in recipients:
            folder, real_email = _resolve(recipient.lower())
            dest_key = f'emails/{folder}/{message_id}.txt'

            try:
                s3.put_object(
                    Bucket=UPLOAD_BUCKET,
                    Key=dest_key,
                    Body=text_body.encode('utf-8'),
                    ContentType='text/plain; charset=utf-8',
                )
                print(f'[EmailIngest] stored → {dest_key}')
            except Exception as exc:
                print(f'[EmailIngest] ERROR writing {dest_key}: {exc}')
                raise

            # Forward to real inbox with sender's email masked
            if folder != '_unknown' and real_email:
                _forward(text_body, msg, real_email)

        try:
            s3.delete_object(Bucket=UPLOAD_BUCKET, Key=raw_key)
            print(f'[EmailIngest] cleaned up raw/{message_id}')
        except Exception as exc:
            print(f'[EmailIngest] WARNING: could not delete {raw_key}: {exc}')

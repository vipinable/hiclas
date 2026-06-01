"""SES inbound email processor — copies raw emails to emails/<cognito-username>/ in S3.

The inbound recipient address is resolved to its Cognito username (via the
user pool's email attribute). If no matching user is found, the email is
filed under emails/_unknown/ so nothing is silently dropped.
"""
import os
import boto3

s3 = boto3.client('s3')
cognito_idp = boto3.client('cognito-idp')

UPLOAD_BUCKET = os.environ.get('UPLOAD_BUCKET', '')
USER_POOL_ID = os.environ.get('USER_POOL_ID', '')
RAW_PREFIX = 'emails/raw/'


def _username_for_email(email):
    """Return the Cognito username owning this email, or None if not found."""
    if not USER_POOL_ID or not email:
        return None
    try:
        resp = cognito_idp.list_users(
            UserPoolId=USER_POOL_ID,
            Filter=f'email = "{email}"',
            Limit=1,
        )
        users = resp.get('Users', [])
        if users:
            return users[0]['Username']
    except Exception as exc:
        print(f'[EmailIngest] WARNING: Cognito lookup failed for {email}: {exc}')
    return None


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

        for recipient in recipients:
            recipient = recipient.lower()
            username = _username_for_email(recipient)
            folder = username if username else '_unknown'
            dest_key = f'emails/{folder}/{message_id}.eml'
            print(f'[EmailIngest] {recipient} -> cognito username={username or "(none)"}')
            try:
                s3.copy_object(
                    CopySource={'Bucket': UPLOAD_BUCKET, 'Key': raw_key},
                    Bucket=UPLOAD_BUCKET,
                    Key=dest_key,
                    MetadataDirective='COPY',
                )
                print(f'[EmailIngest] stored -> {dest_key}')
            except Exception as exc:
                print(f'[EmailIngest] ERROR copying to {dest_key}: {exc}')
                raise

        try:
            s3.delete_object(Bucket=UPLOAD_BUCKET, Key=raw_key)
            print(f'[EmailIngest] cleaned up raw/{message_id}')
        except Exception as exc:
            # Non-fatal: raw copy will just remain
            print(f'[EmailIngest] WARNING: could not delete raw key {raw_key}: {exc}')

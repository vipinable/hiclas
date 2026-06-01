"""SES inbound email processor — copies raw emails to emails/<recipient>/ in S3."""
import os
import boto3

s3 = boto3.client('s3')
UPLOAD_BUCKET = os.environ.get('UPLOAD_BUCKET', '')
RAW_PREFIX = 'emails/raw/'


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
            dest_key = f'emails/{recipient.lower()}/{message_id}.eml'
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

"""Cognito CreateAuthChallenge trigger — generates a 6-digit OTP and emails it via SES."""
import os
import random

import boto3
from botocore.exceptions import ClientError

ses = boto3.client('ses')
OTP_SENDER = os.environ.get('OTP_SENDER_EMAIL', '')


def handler(event, context):
    user = event.get('userName', 'unknown')
    print(f'[CreateAuth] invoked for user={user}')

    if not OTP_SENDER:
        print('[CreateAuth] ERROR: OTP_SENDER_EMAIL env var is not set')
        raise RuntimeError(
            'OTP_SENDER_EMAIL is not configured on this Lambda. '
            'Run the "Setup SES Sender Email" workflow to set it.'
        )

    recipient = event['request']['userAttributes'].get('email', '')
    if not recipient:
        print('[CreateAuth] ERROR: no email attribute on user')
        raise RuntimeError('User record has no email attribute.')

    otp = str(random.randint(100000, 999999))
    print(f'[CreateAuth] sending OTP to {recipient} from {OTP_SENDER}')

    try:
        resp = ses.send_email(
            Source=OTP_SENDER,
            Destination={'ToAddresses': [recipient]},
            Message={
                'Subject': {'Data': 'Your HighlyClassifieds login code'},
                'Body': {
                    'Text': {
                        'Data': (
                            f'Your one-time login code is:\n\n'
                            f'  {otp}\n\n'
                            f'This code expires in 10 minutes.\n\n'
                            f'If you did not request this, you can safely ignore this email.'
                        )
                    },
                    'Html': {
                        'Data': (
                            f'<div style="font-family:sans-serif;max-width:420px;margin:auto;padding:2rem">'
                            f'<h2 style="color:#1d4ed8;margin-bottom:0.25rem">HighlyClassifieds</h2>'
                            f'<p style="color:#6b7280;margin-top:0">Your one-time login code</p>'
                            f'<p style="font-size:2.5rem;font-weight:800;letter-spacing:0.35em;'
                            f'color:#111827;background:#f3f4f6;padding:1rem;border-radius:0.5rem;'
                            f'text-align:center;margin:1.5rem 0">{otp}</p>'
                            f'<p style="color:#6b7280;font-size:0.9rem">'
                            f'This code expires in <strong>10 minutes</strong>.</p>'
                            f'<hr style="border:none;border-top:1px solid #e5e7eb;margin:1.5rem 0">'
                            f'<p style="color:#9ca3af;font-size:0.78rem">'
                            f'If you did not request this login code you can safely ignore this email.</p>'
                            f'</div>'
                        )
                    },
                },
            },
        )
        print(f'[CreateAuth] SES accepted message id={resp["MessageId"]}')
    except ClientError as exc:
        code = exc.response['Error']['Code']
        msg  = exc.response['Error']['Message']
        print(f'[CreateAuth] SES error {code}: {msg}')
        # Re-raise so Cognito surfaces the real error to the caller
        raise RuntimeError(f'SES error ({code}): {msg}') from exc

    event['response']['publicChallengeParameters'] = {
        'hint': 'Enter the 6-digit code sent to your email',
    }
    event['response']['privateChallengeParameters'] = {'answer': otp}
    event['response']['challengeMetadata'] = 'OTP_CHALLENGE'
    print('[CreateAuth] challenge parameters set — returning to Cognito')
    return event

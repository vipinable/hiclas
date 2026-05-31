"""Cognito CreateAuthChallenge trigger — generates a 6-digit OTP and emails it via SES."""
import os
import random

import boto3

ses = boto3.client('ses')
OTP_SENDER = os.environ.get('OTP_SENDER_EMAIL', 'noreply@example.com')


def handler(event, context):
    otp = str(random.randint(100000, 999999))
    email = event['request']['userAttributes']['email']

    ses.send_email(
        Source=OTP_SENDER,
        Destination={'ToAddresses': [email]},
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
                        f'<div style="font-family:sans-serif;max-width:400px;margin:auto;padding:2rem">'
                        f'<h2 style="color:#1d4ed8">HighlyClassifieds</h2>'
                        f'<p>Your one-time login code is:</p>'
                        f'<p style="font-size:2.5rem;font-weight:800;letter-spacing:0.3em;color:#111827">{otp}</p>'
                        f'<p style="color:#6b7280;font-size:0.9rem">This code expires in 10 minutes.</p>'
                        f'<hr style="border:none;border-top:1px solid #e5e7eb;margin:1.5rem 0">'
                        f'<p style="color:#9ca3af;font-size:0.8rem">If you did not request this, you can safely ignore this email.</p>'
                        f'</div>'
                    )
                },
            },
        },
    )

    event['response']['publicChallengeParameters'] = {
        'hint': 'Enter the 6-digit code sent to your email',
    }
    event['response']['privateChallengeParameters'] = {'answer': otp}
    event['response']['challengeMetadata'] = 'OTP_CHALLENGE'
    return event

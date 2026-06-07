"""Cognito DefineAuthChallenge trigger — drives the OTP custom auth flow."""


def handler(event, context):
    session = event['request']['session']

    if not session:
        # First call: issue the OTP challenge
        event['response']['challengeName'] = 'CUSTOM_CHALLENGE'
        event['response']['issueTokens'] = False
        event['response']['failAuthentication'] = False
    elif (
        len(session) == 1
        and session[0].get('challengeName') == 'CUSTOM_CHALLENGE'
        and session[0].get('challengeResult') is True
    ):
        # OTP was correct → issue tokens
        event['response']['issueTokens'] = True
        event['response']['failAuthentication'] = False
    else:
        # Wrong answer or too many attempts
        event['response']['issueTokens'] = False
        event['response']['failAuthentication'] = True

    return event

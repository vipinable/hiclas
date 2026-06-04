"""Cognito VerifyAuthChallengeResponse trigger — checks the OTP the user submitted."""


def handler(event, context):
    expected = event['request']['privateChallengeParameters']['answer']
    given = event['request']['challengeAnswer']
    event['response']['answerCorrect'] = (given.strip() == expected)
    return event

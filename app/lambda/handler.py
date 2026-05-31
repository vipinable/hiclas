"""Classifieds API — listings CRUD + S3 presign + email OTP auth.

Public routes:
    GET  /api/listings           list all listings (newest first)
    GET  /api/listings/{id}      single listing
    POST /api/auth/init          start email-OTP login (creates Cognito user if new)
    POST /api/auth/verify        submit OTP, receive access token

Protected routes (require Authorization: Bearer <accessToken>):
    POST /api/listings           create a listing
    POST /api/presign            mint presigned S3 POSTs for direct image upload
"""
import json
import os
import time
import uuid

import boto3
from botocore.exceptions import ClientError

table = boto3.resource('dynamodb').Table(os.environ['TABLE_NAME'])
s3 = boto3.client('s3')
cognito_idp = boto3.client('cognito-idp')

UPLOAD_BUCKET = os.environ.get('UPLOAD_BUCKET', '')
MAX_IMAGES = int(os.environ.get('MAX_IMAGES', '10'))
ALLOWED_CONTENT_TYPES = ('image/jpeg', 'image/png', 'image/webp', 'image/gif')
MAX_BYTES = 10 * 1024 * 1024

USER_POOL_ID = os.environ.get('USER_POOL_ID', '')
USER_POOL_CLIENT_ID = os.environ.get('USER_POOL_CLIENT_ID', '')

CORS = {
    'Content-Type': 'application/json',
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET,POST,OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type,Authorization',
}


def _resp(status, body):
    return {
        'statusCode': status,
        'headers': CORS,
        'body': json.dumps(body, default=str),
    }


def _ext_for(content_type):
    return {
        'image/jpeg': 'jpg',
        'image/png': 'png',
        'image/webp': 'webp',
        'image/gif': 'gif',
    }.get(content_type, 'jpg')


def _require_auth(event):
    """Validate Bearer access token via Cognito GetUser.

    Returns (email, None) on success or (None, error_response) on failure.
    GetUser authenticates itself using the access token — no IAM grant needed.
    """
    auth_header = (event.get('headers') or {}).get('authorization', '')
    if not auth_header.lower().startswith('bearer '):
        return None, _resp(401, {'message': 'Authentication required'})
    token = auth_header[7:].strip()
    try:
        result = cognito_idp.get_user(AccessToken=token)
        return result['Username'], None
    except ClientError as e:
        code = e.response['Error']['Code']
        if code in ('NotAuthorizedException', 'UserNotFoundException', 'TokenExpiredException'):
            return None, _resp(401, {'message': 'Invalid or expired token'})
        raise


def handler(event, context):
    method = event.get('requestContext', {}).get('http', {}).get('method', 'GET')
    path = event.get('rawPath', '/')
    parts = [p for p in path.strip('/').split('/') if p]

    if method == 'OPTIONS':
        return _resp(200, {})

    # ── /api/auth/init ────────────────────────────────────────────────────────
    # Body: { "email": "user@example.com" }
    # Returns: { "session": "..." }
    if parts == ['api', 'auth', 'init'] and method == 'POST':
        try:
            data = json.loads(event.get('body') or '{}')
        except ValueError:
            return _resp(400, {'message': 'Invalid JSON'})

        email = data.get('email', '').strip().lower()
        if not email or '@' not in email:
            return _resp(400, {'message': 'A valid email address is required'})

        # Auto-create the Cognito user if this is their first login
        try:
            cognito_idp.admin_get_user(UserPoolId=USER_POOL_ID, Username=email)
        except ClientError as e:
            if e.response['Error']['Code'] == 'UserNotFoundException':
                cognito_idp.admin_create_user(
                    UserPoolId=USER_POOL_ID,
                    Username=email,
                    MessageAction='SUPPRESS',
                    UserAttributes=[
                        {'Name': 'email', 'Value': email},
                        {'Name': 'email_verified', 'Value': 'true'},
                    ],
                )
                # Set a random permanent password to move the user to CONFIRMED status
                cognito_idp.admin_set_user_password(
                    UserPoolId=USER_POOL_ID,
                    Username=email,
                    Password=str(uuid.uuid4()).replace('-', '') + 'Aa1!',
                    Permanent=True,
                )
            else:
                raise

        try:
            response = cognito_idp.admin_initiate_auth(
                UserPoolId=USER_POOL_ID,
                ClientId=USER_POOL_CLIENT_ID,
                AuthFlow='CUSTOM_AUTH',
                AuthParameters={'USERNAME': email},
            )
        except ClientError as e:
            return _resp(500, {'message': 'Could not initiate auth: ' + e.response['Error']['Message']})

        return _resp(200, {
            'session': response['Session'],
            'challengeName': response.get('ChallengeName', 'CUSTOM_CHALLENGE'),
        })

    # ── /api/auth/verify ──────────────────────────────────────────────────────
    # Body: { "email": "...", "session": "...", "code": "123456" }
    # Returns: { "accessToken": "...", "idToken": "...", "expiresIn": 3600 }
    if parts == ['api', 'auth', 'verify'] and method == 'POST':
        try:
            data = json.loads(event.get('body') or '{}')
        except ValueError:
            return _resp(400, {'message': 'Invalid JSON'})

        email = data.get('email', '').strip().lower()
        session = data.get('session', '')
        code = data.get('code', '').strip()

        if not email or not session or not code:
            return _resp(400, {'message': 'email, session, and code are required'})

        try:
            response = cognito_idp.admin_respond_to_auth_challenge(
                UserPoolId=USER_POOL_ID,
                ClientId=USER_POOL_CLIENT_ID,
                ChallengeName='CUSTOM_CHALLENGE',
                Session=session,
                ChallengeResponses={
                    'USERNAME': email,
                    'ANSWER': code,
                },
            )
        except ClientError as e:
            err_code = e.response['Error']['Code']
            if err_code in ('NotAuthorizedException', 'CodeMismatchException'):
                return _resp(401, {'message': 'Incorrect or expired code'})
            return _resp(500, {'message': 'Verification failed: ' + e.response['Error']['Message']})

        auth_result = response.get('AuthenticationResult')
        if not auth_result:
            return _resp(401, {'message': 'Authentication failed — please try again'})

        return _resp(200, {
            'accessToken': auth_result['AccessToken'],
            'idToken': auth_result.get('IdToken', ''),
            'expiresIn': auth_result.get('ExpiresIn', 3600),
        })

    # ── /api/presign ──────────────────────────────────────────────────────────
    # Protected. Body: { "files": [{ "contentType": "image/jpeg" }, ...] }
    if parts == ['api', 'presign'] and method == 'POST':
        email, err = _require_auth(event)
        if err:
            return err

        if not UPLOAD_BUCKET:
            return _resp(500, {'message': 'Uploads not configured'})
        try:
            data = json.loads(event.get('body') or '{}')
        except ValueError:
            return _resp(400, {'message': 'Invalid JSON'})

        files = data.get('files', [])
        if not isinstance(files, list) or len(files) == 0:
            return _resp(400, {'message': 'No files requested'})
        if len(files) > MAX_IMAGES:
            return _resp(400, {'message': f'Maximum {MAX_IMAGES} images allowed'})

        listing_id = str(uuid.uuid4())
        uploads = []
        for index, f in enumerate(files):
            content_type = (f or {}).get('contentType', 'image/jpeg')
            if content_type not in ALLOWED_CONTENT_TYPES:
                return _resp(400, {'message': f'Unsupported content type: {content_type}'})
            key = f'uploads/{listing_id}/{index}.{_ext_for(content_type)}'
            presigned = s3.generate_presigned_post(
                Bucket=UPLOAD_BUCKET,
                Key=key,
                Fields={'Content-Type': content_type},
                Conditions=[
                    {'Content-Type': content_type},
                    ['content-length-range', 1, MAX_BYTES],
                ],
                ExpiresIn=300,
            )
            uploads.append({
                'url': presigned['url'],
                'fields': presigned['fields'],
                'key': key,
                'imagePath': f'/{key}',
            })

        return _resp(200, {'listingId': listing_id, 'uploads': uploads})

    # ── /api/listings ─────────────────────────────────────────────────────────
    if parts == ['api', 'listings']:
        if method == 'GET':
            items = table.scan().get('Items', [])
            items.sort(key=lambda x: x.get('createdAt', 0), reverse=True)
            return _resp(200, items)

        if method == 'POST':
            email, err = _require_auth(event)
            if err:
                return err

            try:
                data = json.loads(event.get('body') or '{}')
            except ValueError:
                return _resp(400, {'message': 'Invalid JSON'})

            images = data.get('images', [])
            if not isinstance(images, list):
                images = []
            images = images[:MAX_IMAGES]
            if not images and data.get('imageUrl'):
                images = [data['imageUrl']]

            item = {
                'id': data.get('id') or str(uuid.uuid4()),
                'title': data.get('title', 'Untitled'),
                'description': data.get('description', ''),
                'price': str(data.get('price', '')),
                'category': data.get('category', 'general'),
                'location': data.get('location', ''),
                'images': images,
                'imageUrl': images[0] if images else data.get('imageUrl', ''),
                'postedBy': email,
                'createdAt': int(time.time() * 1000),
            }
            table.put_item(Item=item)
            return _resp(201, item)

    # ── /api/listings/{id} ────────────────────────────────────────────────────
    if len(parts) == 3 and parts[:2] == ['api', 'listings'] and method == 'GET':
        result = table.get_item(Key={'id': parts[2]})
        item = result.get('Item')
        if not item:
            return _resp(404, {'message': 'Not found'})
        return _resp(200, item)

    return _resp(404, {'message': 'Not found'})

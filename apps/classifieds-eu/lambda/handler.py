"""Classifieds API — listings CRUD + S3 presign + email OTP auth + interest relay.

Public routes:
    GET  /api/listings              list all listings (newest first)
    GET  /api/listings/{id}         single listing

    POST /api/auth/init             start email-OTP login (creates Cognito user if new)
    POST /api/auth/verify           submit OTP, receive access token

Protected routes (require Authorization: Bearer <accessToken>):
    POST /api/listings              create a listing
    POST /api/presign               mint presigned S3 POSTs for direct image upload
    POST /api/listings/{id}/interest  send masked interest notification to listing owner
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
ses = boto3.client('ses')

UPLOAD_BUCKET = os.environ.get('UPLOAD_BUCKET', '')
MAX_IMAGES = int(os.environ.get('MAX_IMAGES', '10'))
ALLOWED_CONTENT_TYPES = ('image/jpeg', 'image/png', 'image/webp', 'image/gif')
MAX_BYTES = 10 * 1024 * 1024

USER_POOL_ID = os.environ.get('USER_POOL_ID', '')
USER_POOL_CLIENT_ID = os.environ.get('USER_POOL_CLIENT_ID', '')
SES_FROM_EMAIL = os.environ.get('SES_FROM_EMAIL', '')
SES_RECEIVE_DOMAIN = os.environ.get('SES_RECEIVE_DOMAIN', '')

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
        'image/png':  'png',
        'image/webp': 'webp',
        'image/gif':  'gif',
    }.get(content_type, 'jpg')


def _cognito_trigger_message(err_msg: str) -> str:
    """Extract the real inner error from a Cognito trigger-failure message."""
    marker = 'failed with error '
    if marker in err_msg:
        return err_msg.split(marker, 1)[1].strip()
    return err_msg


def _require_auth(event):
    """Validate Bearer access token via Cognito GetUser.

    Returns (username, sub, None) on success or (None, None, error_response) on failure.
    username — Cognito Username (the user's email address in this pool)
    sub      — Cognito sub UUID, used as the masked reply-to local part
    """
    auth_header = (event.get('headers') or {}).get('authorization', '')
    if not auth_header.lower().startswith('bearer '):
        return None, None, _resp(401, {'message': 'Authentication required'})
    token = auth_header[7:].strip()
    try:
        result = cognito_idp.get_user(AccessToken=token)
        username = result['Username']
        sub = next(
            (a['Value'] for a in result.get('UserAttributes', []) if a['Name'] == 'sub'),
            None,
        )
        return username, sub, None
    except ClientError as e:
        code = e.response['Error']['Code']
        if code in ('NotAuthorizedException', 'UserNotFoundException', 'TokenExpiredException'):
            return None, None, _resp(401, {'message': 'Invalid or expired token'})
        raise


def _cognito_email_for_user(username: str) -> str | None:
    """Return the email attribute for a Cognito user, or None on failure."""
    if not USER_POOL_ID or not username:
        return None
    try:
        result = cognito_idp.admin_get_user(UserPoolId=USER_POOL_ID, Username=username)
        for attr in result.get('UserAttributes', []):
            if attr['Name'] == 'email':
                return attr['Value']
    except Exception as exc:
        print(f'[Handler] WARNING: could not get email for {username}: {exc}')
    return None


def handler(event, context):
    method = event.get('requestContext', {}).get('http', {}).get('method', 'GET')
    path   = event.get('rawPath', '/')
    parts  = [p for p in path.strip('/').split('/') if p]

    if method == 'OPTIONS':
        return _resp(200, {})

    # ── /api/auth/init ────────────────────────────────────────────────────────
    if parts == ['api', 'auth', 'init'] and method == 'POST':
        try:
            data = json.loads(event.get('body') or '{}')
        except ValueError:
            return _resp(400, {'message': 'Invalid JSON'})

        email = data.get('email', '').strip().lower()
        if not email or '@' not in email:
            return _resp(400, {'message': 'A valid email address is required'})

        try:
            cognito_idp.admin_get_user(UserPoolId=USER_POOL_ID, Username=email)
        except ClientError as e:
            if e.response['Error']['Code'] == 'UserNotFoundException':
                cognito_idp.admin_create_user(
                    UserPoolId=USER_POOL_ID,
                    Username=email,
                    MessageAction='SUPPRESS',
                    UserAttributes=[
                        {'Name': 'email',          'Value': email},
                        {'Name': 'email_verified', 'Value': 'true'},
                    ],
                )
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
            err_code = e.response['Error']['Code']
            err_msg  = e.response['Error']['Message']
            inner = _cognito_trigger_message(err_msg)
            if err_code == 'UserLambdaValidationException':
                return _resp(500, {'message': f'OTP could not be sent: {inner}'})
            return _resp(500, {'message': f'Auth error: {inner}'})

        return _resp(200, {
            'session':       response['Session'],
            'challengeName': response.get('ChallengeName', 'CUSTOM_CHALLENGE'),
        })

    # ── /api/auth/verify ──────────────────────────────────────────────────────
    if parts == ['api', 'auth', 'verify'] and method == 'POST':
        try:
            data = json.loads(event.get('body') or '{}')
        except ValueError:
            return _resp(400, {'message': 'Invalid JSON'})

        email   = data.get('email',   '').strip().lower()
        session = data.get('session', '')
        code    = data.get('code',    '').strip()

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
                    'ANSWER':   code,
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
            'idToken':     auth_result.get('IdToken', ''),
            'expiresIn':   auth_result.get('ExpiresIn', 3600),
        })

    # ── /api/presign ──────────────────────────────────────────────────────────
    if parts == ['api', 'presign'] and method == 'POST':
        username, _, err = _require_auth(event)
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
                'url':       presigned['url'],
                'fields':    presigned['fields'],
                'key':       key,
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
            username, _, err = _require_auth(event)
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
                'id':          data.get('id') or str(uuid.uuid4()),
                'title':       data.get('title', 'Untitled'),
                'description': data.get('description', ''),
                'price':       str(data.get('price', '')),
                'category':    data.get('category', 'general'),
                'location':    data.get('location', ''),
                'images':      images,
                'imageUrl':    images[0] if images else data.get('imageUrl', ''),
                'postedBy':    username,
                'createdAt':   int(time.time() * 1000),
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

    # ── /api/listings/{id}/interest ───────────────────────────────────────────
    # Sends a masked email to the listing owner. Reply-To is set to
    # <requester-sub>@<SES_RECEIVE_DOMAIN> so replies route back via SES ingest
    # without exposing either party's real email address.
    if len(parts) == 4 and parts[:2] == ['api', 'listings'] and parts[3] == 'interest' and method == 'POST':
        username, sub, err = _require_auth(event)
        if err:
            return err

        listing_id = parts[2]
        result = table.get_item(Key={'id': listing_id})
        listing = result.get('Item')
        if not listing:
            return _resp(404, {'message': 'Listing not found'})

        owner = listing.get('postedBy', '')
        if not owner:
            return _resp(400, {'message': 'This listing has no owner on record'})

        if owner == username:
            return _resp(400, {'message': 'You cannot express interest in your own listing'})

        if not SES_FROM_EMAIL or not SES_RECEIVE_DOMAIN:
            return _resp(503, {'message': 'Email relay not configured on this server'})

        if not sub:
            return _resp(500, {'message': 'Could not determine your account identifier'})

        owner_email = _cognito_email_for_user(owner)
        if not owner_email:
            return _resp(500, {'message': 'Could not reach the listing owner'})

        title    = listing.get('title', 'Untitled')
        price    = listing.get('price', '')
        location = listing.get('location', '')
        reply_to = f'{sub}@{SES_RECEIVE_DOMAIN}'

        detail_lines = [f'  Title: {title}']
        if price:
            detail_lines.append(f'  Price: ${price}')
        if location:
            detail_lines.append(f'  Location: {location}')

        body = (
            f'Hello,\n\n'
            f'Someone has expressed interest in your listing: "{title}"\n\n'
            f'To respond, simply reply to this email. Your reply will be delivered '
            f'privately — no real email addresses are shared between parties.\n\n'
            f'Listing details:\n'
            + '\n'.join(detail_lines)
            + '\n\n— The HighlyClassifieds Team\n'
        )

        try:
            ses.send_email(
                Source=SES_FROM_EMAIL,
                Destination={'ToAddresses': [owner_email]},
                ReplyToAddresses=[reply_to],
                Message={
                    'Subject': {
                        'Data': f'[HighlyClassifieds] Interest in your listing: {title}',
                        'Charset': 'UTF-8',
                    },
                    'Body': {'Text': {'Data': body, 'Charset': 'UTF-8'}},
                },
            )
        except Exception as exc:
            print(f'[Handler] ERROR sending interest email to {owner_email}: {exc}')
            return _resp(500, {'message': 'Failed to send interest notification'})

        print(f'[Handler] interest: listing={listing_id} owner={owner} reply_to={reply_to}')
        return _resp(200, {'message': 'Interest notification sent'})

    return _resp(404, {'message': 'Not found'})

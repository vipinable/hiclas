import logging
import boto3
import json
import os
from botocore.exceptions import ClientError
from datetime import datetime, timezone
import uuid
import jinja2
import urllib

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Create service clients
session = boto3.session.Session()
s3 = session.client('s3')
sts = session.client('sts')
s3c = session.client('s3control', region_name='us-east-1')
dynamodb = session.resource('dynamodb')

TABLE_CLASSIFIEDS = os.getenv('TABLE_CLASSIFIEDS')
BUCKET_STORE = os.getenv('BUCKET_STORE')


def _log(request_id, level, msg, **kwargs):
    """Emit a structured log line that includes the Lambda request ID."""
    extra = ' '.join(f'{k}={v}' for k, v in kwargs.items())
    line = f'[{request_id}] {msg}' + (f' | {extra}' if extra else '')
    getattr(logger, level)(line)


def handler(event, context):
    request_id = context.aws_request_id
    headers = event.get('headers', {})
    method = event.get('requestContext', {}).get('http', {}).get('method', 'UNKNOWN')
    raw_path = event.get('rawPath', '/')
    client_ip = headers.get('x-forwarded-for', 'unknown')
    via = headers.get('via', '')

    _log(request_id, 'info', 'Request received',
         method=method, path=raw_path, client_ip=client_ip, via=via)

    # IP allowlist — must arrive via CloudFront and from a known IP
    allowed_ips = ['143.178.81.116', '147.161.173.114', '170.85.78.100', '77.165.18.64']
    via_ok = 'via' in headers and 'cloudfront.net' in via
    ip_ok = client_ip in allowed_ips

    if not via_ok or not ip_ok:
        _log(request_id, 'warning', 'Request denied',
             reason='not via CloudFront' if not via_ok else 'IP not in allowlist',
             client_ip=client_ip, via_ok=via_ok, ip_ok=ip_ok)
        return {
            'statusCode': '403',
            'body': {'message': 'Forbidden'},
            'headers': {'Content-Type': 'application/json'},
        }

    _log(request_id, 'info', 'IP allowlist passed', client_ip=client_ip)

    path_parts = raw_path.strip('/').split('/')

    # --- GET /api/details/{id} ---
    if path_parts[0] == 'api' and len(path_parts) == 3 and path_parts[1] == 'details':
        listing_id = path_parts[2]
        _log(request_id, 'info', 'Route: GET /api/details', listing_id=listing_id)
        try:
            table = dynamodb.Table(TABLE_CLASSIFIEDS)
            response = table.query(
                KeyConditionExpression=boto3.dynamodb.conditions.Key('id').eq(listing_id)
            )
        except ClientError as e:
            _log(request_id, 'error', 'DynamoDB query failed',
                 listing_id=listing_id, error=str(e))
            return {
                'statusCode': '500',
                'body': {'message': 'Internal Server Error'},
                'headers': {'Content-Type': 'application/json'},
            }

        items = response.get('Items', [])
        if not items:
            _log(request_id, 'warning', 'Listing not found', listing_id=listing_id)
            return {
                'statusCode': '404',
                'body': {'message': 'Not Found'},
                'headers': {'Content-Type': 'application/json'},
            }

        item = items[0]
        _log(request_id, 'info', 'Listing found',
             listing_id=listing_id, image_count=len(item.get('images', [])))
        result = {
            'images': item.get('images', []),
            'id': item['id'],
            'title': item['title'],
            'description': item['description'],
            'price': item['price'],
            'createdAt': item['createdAt'],
            'updatedAt': item['updatedAt'],
            'category': item['category'],
            'condition': item['condition'],
            'location': item['location'],
            'status': item['status'],
        }
        _log(request_id, 'info', 'Response 200', route='GET /api/details')
        return {
            'statusCode': '200',
            'body': result,
            'headers': {'Content-Type': 'application/json'},
        }

    # --- GET /api/listings ---
    elif path_parts[0] == 'api' and len(path_parts) == 2 and path_parts[1] == 'listings':
        _log(request_id, 'info', 'Route: GET /api/listings')
        try:
            items = query_data(TABLE_CLASSIFIEDS, request_id)
        except Exception as e:
            _log(request_id, 'error', 'Failed to query listings', error=str(e))
            return {
                'statusCode': '500',
                'body': {'message': 'Internal Server Error'},
                'headers': {'Content-Type': 'application/json'},
            }
        _log(request_id, 'info', 'Response 200', route='GET /api/listings', count=len(items))
        return {
            'statusCode': '200',
            'body': items,
            'headers': {'Content-Type': 'application/json'},
        }

    # --- POST /api/presign ---
    elif path_parts[0] == 'api' and len(path_parts) == 2 and path_parts[1] == 'presign':
        _log(request_id, 'info', 'Route: POST /api/presign')
        if method != 'POST' or headers.get('user-agent', '') != 'Amazon CloudFront':
            _log(request_id, 'warning', 'Response 400',
                 route='POST /api/presign', reason='method or user-agent mismatch',
                 method=method)
            return {
                'statusCode': '400',
                'body': {'message': 'Bad Request'},
                'headers': {'Content-Type': 'application/json'},
            }

        try:
            req_count = json.loads(event['body'])['count']
        except (KeyError, ValueError, TypeError) as e:
            _log(request_id, 'error', 'Failed to parse presign request body', error=str(e))
            return {
                'statusCode': '400',
                'body': {'message': 'Bad Request'},
                'headers': {'Content-Type': 'application/json'},
            }

        item_id = str(uuid.uuid4())
        _log(request_id, 'info', 'Generating presigned URLs',
             item_id=item_id, count=req_count)

        result = {'uuid': item_id, 'urls': []}
        for index in range(req_count):
            object_key = f'uploads/{item_id}/{index}.jpg'
            presigned_post = create_presigned_post(BUCKET_STORE, object_key, 60, request_id)
            if presigned_post and 'url' in presigned_post:
                result['urls'].append(presigned_post)
                _log(request_id, 'info', 'Presigned URL generated',
                     item_id=item_id, index=index, key=object_key)
            else:
                result['urls'].append(None)
                _log(request_id, 'error', 'Presigned URL generation failed',
                     item_id=item_id, index=index, key=object_key)

        _log(request_id, 'info', 'Response 200', route='POST /api/presign',
             item_id=item_id, urls_generated=len([u for u in result['urls'] if u]))
        return {
            'statusCode': '200',
            'body': result,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': '*',
                'Access-Control-Allow-Methods': 'GET,POST,PUT,OPTIONS',
                'Access-Control-Allow-Headers': 'Content-Type,Authorization',
            },
        }

    # --- GET/POST /items (legacy) ---
    elif len(path_parts) == 1 and path_parts[0] == 'items':
        _log(request_id, 'info', 'Route: GET /items (legacy)')
        try:
            items = query_data(TABLE_CLASSIFIEDS, request_id)
        except Exception as e:
            _log(request_id, 'error', 'Failed to query items', error=str(e))
            return {
                'statusCode': '500',
                'body': {'message': 'Internal Server Error'},
                'headers': {'Content-Type': 'application/json'},
            }
        _log(request_id, 'info', 'Response 200', route='GET /items', count=len(items))
        return {
            'statusCode': '200',
            'body': items,
            'headers': {'Content-Type': 'application/json'},
        }

    # --- POST /post ---
    elif len(path_parts) == 1 and path_parts[0] == 'post':
        _log(request_id, 'info', 'Route: POST /post')
        if method != 'POST' or headers.get('user-agent', '') != 'Amazon CloudFront':
            _log(request_id, 'warning', 'Response 400',
                 route='POST /post', reason='method or user-agent mismatch', method=method)
            return {
                'statusCode': '400',
                'body': {'message': 'Bad Request'},
                'headers': {'Content-Type': 'application/json'},
            }

        try:
            body = json.loads(event['body'])
        except (KeyError, ValueError, TypeError) as e:
            _log(request_id, 'error', 'Failed to parse POST /post body', error=str(e))
            return {
                'statusCode': '400',
                'body': {'message': 'Bad Request'},
                'headers': {'Content-Type': 'application/json'},
            }

        raw_origin = headers.get('origin') or headers.get('referer', '')
        parsed = urllib.parse.urlparse(raw_origin)
        origin = f'{parsed.scheme}://{parsed.netloc}'
        _log(request_id, 'info', 'Writing listing',
             origin=origin, image_count=len(body.get('images', [])),
             title=body.get('title', ''))

        try:
            response = write_data(body, origin, request_id)
        except Exception as e:
            _log(request_id, 'error', 'Failed to write listing', error=str(e))
            return {
                'statusCode': '500',
                'body': {'message': 'Internal Server Error'},
                'headers': {'Content-Type': 'application/json'},
            }

        _log(request_id, 'info', 'Response 200', route='POST /post')
        return {
            'statusCode': '200',
            'body': response,
            'headers': {'Content-Type': 'application/json'},
        }

    # --- Default: serve index.html ---
    else:
        _log(request_id, 'info', 'Route: serve index.html', path=raw_path)
        try:
            body = get_index(BUCKET_STORE, request_id)
        except Exception as e:
            _log(request_id, 'error', 'Failed to fetch index.html', error=str(e))
            return {
                'statusCode': '500',
                'body': 'Internal Server Error',
                'headers': {'Content-Type': 'text/plain'},
            }
        _log(request_id, 'info', 'Response 200', route='index.html')
        return {
            'statusCode': '200',
            'body': body,
            'headers': {'Content-Type': 'text/html'},
        }


def create_presigned_post(bucket_name, object_name, expiration, request_id,
                          fields=None, conditions=None):
    s3role = os.environ.get('S3_UPLOAD_ROLE')
    if not s3role:
        logger.error(f'[{request_id}] S3_UPLOAD_ROLE env var not set')
        return None

    try:
        client = boto3.client('sts')
        assumed = client.assume_role(
            DurationSeconds=900,
            RoleArn=s3role,
            RoleSessionName='PreSign',
        )
        creds = assumed['Credentials']
        presign_session = boto3.session.Session(
            aws_access_key_id=creds['AccessKeyId'],
            aws_secret_access_key=creds['SecretAccessKey'],
            aws_session_token=creds['SessionToken'],
        )
        s3_client = presign_session.client('s3')
        response = s3_client.generate_presigned_post(
            bucket_name, object_name,
            Fields=fields, Conditions=conditions, ExpiresIn=expiration,
        )
        return response
    except ClientError as e:
        logger.exception(f'[{request_id}] Failed to generate presigned POST for {object_name}: {e}')
        return None


def write_data(body, origin, request_id):
    table = dynamodb.Table(TABLE_CLASSIFIEDS)
    timestamp_str = body['createdAt']
    dt = datetime.strptime(timestamp_str, '%Y-%m-%dT%H:%M:%S.%fZ').replace(tzinfo=timezone.utc)

    imgurls = []
    item_id = None
    for url in body.get('images', []):
        clean_path = url.split('?')[0].rstrip('/')
        parts = clean_path.split('/')
        uid = parts[-2]
        filename = parts[-1]
        if item_id is None:
            item_id = uid
        imgurls.append(f'{origin}/uploads/{uid}/{filename}')

    if item_id is None:
        item_id = str(uuid.uuid4())
        _log(request_id, 'info', 'No images provided — generated fallback item_id', item_id=item_id)

    _log(request_id, 'info', 'Writing item to DynamoDB',
         item_id=item_id, image_count=len(imgurls), table=TABLE_CLASSIFIEDS)

    response = table.put_item(Item={
        'id': item_id,
        'createdAt': body['createdAt'],
        'ts': int(dt.timestamp() * 1000),
        'status': 'active',
        'updatedAt': body['createdAt'],
        'title': body['title'],
        'description': body['description'],
        'category': body['category'],
        'price': body['price'],
        'location': body['location'],
        'condition': body['condition'],
        'images': imgurls,
    })
    _log(request_id, 'info', 'DynamoDB put_item succeeded', item_id=item_id)
    return response


def query_data(table_name, request_id):
    _log(request_id, 'info', 'Scanning DynamoDB table', table=table_name)
    table = dynamodb.Table(table_name)
    response = table.scan()
    items = response.get('Items', [])
    _log(request_id, 'info', 'DynamoDB scan complete', table=table_name, count=len(items))
    return items


def get_index(bucket, request_id):
    _log(request_id, 'info', 'Fetching index.html from S3', bucket=bucket)
    response = s3.get_object(Bucket=bucket, Key='index.html')
    return response['Body'].read()

"""Minimal classifieds API.

Routes (invoked via CloudFront -> Lambda Function URL):
    GET  /api/listings        list all listings (newest first)
    POST /api/listings        create a listing
    GET  /api/listings/{id}   fetch a single listing
    POST /api/presign         mint presigned POSTs for direct S3 image upload

Images are uploaded by the browser straight to a private S3 bucket using the
presigned POST data, then served back through CloudFront at /uploads/*.
"""
import json
import os
import time
import uuid

import boto3

table = boto3.resource('dynamodb').Table(os.environ['TABLE_NAME'])
s3 = boto3.client('s3')

UPLOAD_BUCKET = os.environ.get('UPLOAD_BUCKET')
MAX_IMAGES = int(os.environ.get('MAX_IMAGES', '10'))
# Browser uploads are accepted for these content types only
ALLOWED_CONTENT_TYPES = ('image/jpeg', 'image/png', 'image/webp', 'image/gif')
MAX_BYTES = 10 * 1024 * 1024  # 10 MB per image

CORS = {
    'Content-Type': 'application/json',
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET,POST,OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
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


def handler(event, context):
    method = event.get('requestContext', {}).get('http', {}).get('method', 'GET')
    path = event.get('rawPath', '/')
    parts = [p for p in path.strip('/').split('/') if p]

    if method == 'OPTIONS':
        return _resp(200, {})

    # --- POST /api/presign ---
    # Body: { "files": [{ "contentType": "image/jpeg" }, ...] }  (max 10)
    # Returns: { "listingId": "...", "uploads": [{ "url", "fields", "key", "imagePath" }, ...] }
    if parts == ['api', 'presign'] and method == 'POST':
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
                # Path the browser uses to display the image via CloudFront
                'imagePath': f'/{key}',
            })

        return _resp(200, {'listingId': listing_id, 'uploads': uploads})

    # --- /api/listings ---
    if parts == ['api', 'listings']:
        if method == 'GET':
            items = table.scan().get('Items', [])
            items.sort(key=lambda x: x.get('createdAt', 0), reverse=True)
            return _resp(200, items)
        if method == 'POST':
            try:
                data = json.loads(event.get('body') or '{}')
            except ValueError:
                return _resp(400, {'message': 'Invalid JSON'})

            # Accept an images array (S3 paths) plus a legacy single imageUrl
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
                # Keep imageUrl as the primary thumbnail for backward compat
                'imageUrl': images[0] if images else data.get('imageUrl', ''),
                'createdAt': int(time.time() * 1000),
            }
            table.put_item(Item=item)
            return _resp(201, item)

    # --- /api/listings/{id} ---
    if len(parts) == 3 and parts[:2] == ['api', 'listings'] and method == 'GET':
        result = table.get_item(Key={'id': parts[2]})
        item = result.get('Item')
        if not item:
            return _resp(404, {'message': 'Not found'})
        return _resp(200, item)

    return _resp(404, {'message': 'Not found'})

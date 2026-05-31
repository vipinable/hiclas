"""Minimal classifieds API.

Routes (invoked via CloudFront -> Lambda Function URL):
    GET  /api/listings        list all listings (newest first)
    POST /api/listings        create a listing
    GET  /api/listings/{id}   fetch a single listing
"""
import json
import os
import time
import uuid

import boto3

table = boto3.resource('dynamodb').Table(os.environ['TABLE_NAME'])

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


def handler(event, context):
    method = event.get('requestContext', {}).get('http', {}).get('method', 'GET')
    path = event.get('rawPath', '/')
    parts = [p for p in path.strip('/').split('/') if p]

    if method == 'OPTIONS':
        return _resp(200, {})

    # /api/listings
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
            item = {
                'id': str(uuid.uuid4()),
                'title': data.get('title', 'Untitled'),
                'description': data.get('description', ''),
                'price': str(data.get('price', '')),
                'category': data.get('category', 'general'),
                'imageUrl': data.get('imageUrl', ''),
                'createdAt': int(time.time() * 1000),
            }
            table.put_item(Item=item)
            return _resp(201, item)

    # /api/listings/{id}
    if len(parts) == 3 and parts[:2] == ['api', 'listings'] and method == 'GET':
        result = table.get_item(Key={'id': parts[2]})
        item = result.get('Item')
        if not item:
            return _resp(404, {'message': 'Not found'})
        return _resp(200, item)

    return _resp(404, {'message': 'Not found'})

import logging
import boto3
import base64
import json
import os
from botocore.exceptions import ClientError
from botocore.config import Config
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
s3c = session.client('s3control',region_name='us-east-1')
dynamodb = session.resource('dynamodb')

#Load environment variable values
TABLE_CLASSIFIEDS = os.getenv('TABLE_CLASSIFIEDS')
BUCKET_STORE = os.getenv('BUCKET_STORE')

def handler(event, context):
    
    logger.info("An event received %s" % (event))

    # Check if the request is coming from an allowed IP address if not deny access
    allowed_ips = ['143.178.81.116','147.161.173.114']
    if 'via' not in event['headers'] or 'cloudfront.net' not in event['headers']['via'] or event['headers']['x-forwarded-for'] not in allowed_ips:
        ''' Deny access if using lambda url directly'''
        return({
                'statusCode': '403',
                'body': { 'message': 'Forbidden' },
                'headers': {'Content-Type': 'application/json',
            }
        })
    
    # Retrive the rawPath from the request event and convert to a list for validation and routing
    raw_path = event['rawPath'].strip('/').split('/')
    logger.info("Raw Path: %s" % (raw_path))

    if raw_path[0] == 'api' and len(raw_path) == 3:
        if raw_path[1] == 'details':
            listing_id = raw_path[2]
            table = dynamodb.Table(TABLE_CLASSIFIEDS)
            response = table.query(
                KeyConditionExpression=boto3.dynamodb.conditions.Key('id').eq(listing_id)
            )
            return({
                'statusCode': '200',
                'body': response['Items'][0],
                'headers': {'Content-Type': 'application/json'}
            })
        else:
            return({
                'statusCode': '400',
                'body': { 'message': 'Bad Request' },
                'headers': {'Content-Type': 'application/json'}
            })

    elif raw_path[0] == 'api' and len(raw_path) == 2:
        if raw_path[1] == 'listings':
            # Return all classified items
            return({
                'statusCode': '200',
                'body': query_data(TABLE_CLASSIFIEDS),
                'headers': {'Content-Type': 'application/json'}
            })
        elif raw_path[1] == 'presign':
            # Generate a presigned URL for S3 object upload
            temp_id = str(uuid.uuid4())
            # Generate 10 presigned URL  to upload 10 images
            response = { 'uuid': temp_id, 'urls': [] }
            for index in range(1):
                # Create a unique object key for each image
                # This is just an example, you can modify the logic as needed
                object_key = f'uploads/{temp_id}/{index}.jpg'  # Example object key, replace with actual logic
                response['urls'].append(urllib.parse.quote(create_uploadurl(BUCKET_STORE, object_key, 60)))
            print("Presigned URL Response: %s" % (response))
            return({
                'statusCode': '200',
                'body': response,
                'headers': {
                    'Content-Type': 'application/json',
                    'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Methods': 'GET,POST,PUT,OPTIONS',
                    'Access-Control-Allow-Headers': 'Content-Type,Authorization'
                    }
            })
        else:
            return({
                'statusCode': '400',
                'body': { 'message': 'Bad Request' },
                'headers': {'Content-Type': 'application/json'}
            })
    elif len(raw_path) == 1 and raw_path[0] == 'items':
        return({
            'statusCode': '200',
            'body': query_data(TABLE_CLASSIFIEDS),
            'headers': {'Content-Type': 'application/json',
            }
            })
    else:
        return({
            'statusCode': '200',
            'body': get_index(BUCKET_STORE),
            'headers': {'Content-Type': 'text/html'}
            })
    
    logger.info("QueryString Parameters %s" % (event['queryStringParameters']))
    
    if event['queryStringParameters'] and 'd' in event['queryStringParameters']:
        download_link = create_downloadurl(bucket,event['queryStringParameters']['d'],240)
        return {
            "statusCode": 200,
            "isBase64Encoded": False,
            "headers":{
                "Access-Control-Allow-Origin":"*",
                "Access-Control-Allow-Methods":"GET,POST,OPTIONS",
                "Content-Type": "text/html; charset=utf-8",
                "Access-Control-Allow-Headers": "Content-Type,Authorization"
                },
            "body": render_template(
                                    templatepath="templates/index.j2",
                                    PreFix=prefix,
                                    Objects=list_objects(bucket,prefix),
                                    DownloadFile=event['queryStringParameters']['d'],
                                    DownloadLink=download_link,
                                 )
            }
    
    elif event['queryStringParameters'] and 'fnamex' in event['queryStringParameters']:
        response = create_presigned_post(bucket,prefix + '/' + event['queryStringParameters']['fname'],60)
        return {
                "statusCode": 200,
                "isBase64Encoded": False,
                "headers":{
                    "Access-Control-Allow-Origin":"'*'",
                    "Access-Control-Allow-Methods":"GET",
                    "Content-Type": "text/html; charset=utf-8"
                    },
                "body": render_template(
                            templatepath="templates/upload_form.j2",
                            PreSignUrl=response['url'],
                            PreSignKey=response['fields']['key'],
                            PreSignAccessKey=response['fields']['AWSAccessKeyId'],
                            PreSignPolicy=response['fields']['policy'],
                            PreSignSignature=response['fields']['signature'],
                            PreSignToken=response['fields']['x-amz-security-token']
                        )
                }
    elif event['queryStringParameters'] and 'fname' in event['queryStringParameters']:
        response = create_presigned_post(bucket,prefix + '/' + event['queryStringParameters']['fname'],60)
        if event['queryStringParameters']['fname'] == "":
            return {
                "statusCode": 200,
                "isBase64Encoded": False,
                "headers":{
                    "Access-Control-Allow-Origin":"'*'",
                    "Access-Control-Allow-Methods":"GET",
                    "Content-Type": "text/html; charset=utf-8"
                    },
                "body": "Error: Filename not found"
                }
        else: 
            return {
                    "statusCode": 200,
                    "isBase64Encoded": False,
                    "headers":{
                        "Access-Control-Allow-Origin":"'*'",
                        "Access-Control-Allow-Methods":"GET,POST",
                        "Content-Type": "text/html; charset=utf-8"
                        },
                    "body": render_template(
                                templatepath="templates/index.j2",
                                PreFix=prefix,
                                Objects=list_objects(bucket,prefix),
                                PreSignUrl=response['url'],
                                PreSignKey=response['fields']['key'],
                                PreSignAccessKey=response['fields']['AWSAccessKeyId'],
                                PreSignPolicy=response['fields']['policy'],
                                PreSignSignature=response['fields']['signature'],
                                PreSignToken=response['fields']['x-amz-security-token']
                            )
                    }
        
    else:
        return {
            "statusCode": 200,
            "isBase64Encoded": False,
            "headers":{
                "Access-Control-Allow-Origin":"'*'",
                "Access-Control-Allow-Methods":"GET",
                "Content-Type": "text/html; charset=utf-8"
                },
            "body": render_template(
                                    templatepath="templates/index.j2",
                                    PreFix=prefix,
                                    Objects=list_objects(bucket,prefix),
                                 )
            }

def create_presigned_post(bucket_name, object_name, expiration,
                          fields=None, conditions=None):
    """Generate a presigned URL S3 POST request to upload a file

    :param bucket_name: string
    :param object_name: string
    :param fields: Dictionary of prefilled form fields
    :param conditions: List of conditions to include in the policy
    :param expiration: Time in seconds for the presigned URL to remain valid
    :return: Dictionary with the following keys:
        url: URL to post to
        fields: Dictionary of form fields and values to submit with the POST
    :return: None if error.
    """
    # Generate a presigned S3 POST URL 
    client = boto3.client('sts')
    s3role = os.environ.get('S3_UPLOAD_ROLE')
    assumed_role_object  = client.assume_role(DurationSeconds=900,RoleArn=s3role,RoleSessionName='PreSign',)
    temp_credentials = assumed_role_object['Credentials']
    PreSign = boto3.session.Session(aws_access_key_id=temp_credentials['AccessKeyId'],
                                    aws_secret_access_key=temp_credentials['SecretAccessKey'],
                                    aws_session_token=temp_credentials['SessionToken'])
    s3 = PreSign.client('s3')
    try:
        response = s3.generate_presigned_post(bucket_name,
                                                     object_name,
                                                     Fields=fields,
                                                     Conditions=conditions,
                                                     ExpiresIn=expiration)
    except ClientError as e:
        logging.error(e)
        return None

    # The response contains the presigned URL and required fields
    return response
    
def create_downloadurl(bucket ,key, expiration):
    client = boto3.client('sts')
    assumed_role_object  = client.assume_role(DurationSeconds=900,RoleArn=s3role,RoleSessionName='PreSign',)
    temp_credentials = assumed_role_object['Credentials']
    session = boto3.session.Session(aws_access_key_id=temp_credentials['AccessKeyId'],
                                    aws_secret_access_key=temp_credentials['SecretAccessKey'],
                                    aws_session_token=temp_credentials['SessionToken'])
    s3_resource = session.resource('s3')
    bucket_name = s3_resource.Bucket(bucket).name
    params = {
        'Bucket': bucket_name,
        'Key': key
    }
    s3 = session.client('s3')
    url = s3.generate_presigned_url('get_object', Params=params, ExpiresIn=expiration)
    return (url)

def create_uploadurl(bucket, key, expiration):
    """Generate a presigned URL for uploading an object to S3

    :param bucket: string
        The name of the S3 bucket
    :param key: string
        The object key to upload to
    :param expiration: int
        Time in seconds for the presigned URL to remain valid
    :return: string
        Presigned URL for uploading the object
    """
    #s3 = boto3.client('s3')
    try:
        response = s3.generate_presigned_url('put_object',
                                              Params={'Bucket': bucket, 'Key': key},
                                              ExpiresIn=expiration,
                                              HttpMethod='PUT')
    except ClientError as e:
        logging.error(e)
        return None

    return response 
    
def render_template(templatepath, items, *args, **kargs):
    """Generates the html body for upload form on the jinja template.

    Parameters
    ----------
        templatepath: string
            The path to the template to generate the body from

        *args, **kargs:
            The parameters are dependend on the variables inside the template.

    Return
    ------
        outputTemplate: string
            The mail body with the variables substituted in the template.

    """
    with open(templatepath) as templatefile:
        template = jinja2.Template(templatefile.read())

    outputTemplate = template.render(items=items)

    return outputTemplate
    
def list_objects(bucketname,prefix):
    logger.info("executing index function")
    client = boto3.client('s3')

    ObjectsList = client.list_objects(Bucket=bucketname,Prefix=prefix)
    
    if 'Contents' in ObjectsList:
        for object in ObjectsList['Contents']:
           logger.info("Object: %s" % (object))
    else:
        response = client.put_object(
                Body='create folder',
                Bucket=bucketname,
                Key=prefix+'/',
            )
        ObjectsList = client.list_objects(Bucket=bucketname,Prefix=prefix)
        if 'Contents' in ObjectsList:
            for object in ObjectsList['Contents']:
               logger.info("Object: %s" % (object))
               
    return ObjectsList['Contents']
    
def s3batchops(AccountID):
    source_bucket = os.environ.get("SOURCE_BUCKET")
    sink_bucket = os.environ.get("SINK_BUCKET")
    role_arn = os.environ.get("ROLE_ARN")
    RequestToken = str(uuid.uuid4())
    print(RequestToken,sink_bucket,source_bucket)

    response = s3c.create_job(
        AccountId=AccountID,
        Operation={
            'S3PutObjectCopy': {
                'TargetResource': 'arn:aws:s3:::' + sink_bucket,
                'CannedAccessControlList': 'bucket-owner-full-control',
                'StorageClass': 'STANDARD',
                'TargetKeyPrefix': 's3batch'
            }
        },
        Report={
            'Bucket': 'arn:aws:s3:::' + sink_bucket,
            'Format': 'Report_CSV_20180820',
            'Enabled': True,
            'ReportScope': 'AllTasks'
        },
        ClientRequestToken=RequestToken,
        Manifest={
            'Spec': {
                'Format': 'S3BatchOperations_CSV_20180820',
                'Fields': ["Bucket", "Key"]
            },
            'Location': {
                'ObjectArn': 'arn:aws:s3:::s3batchops-s3inventorybac0e24e-61tssxtt9mj7/xlsreport-test-www/gstestinv/2023-08-01T01-00Z/manifest.json',
                'ETag': '73c7771e9c87f2938406e7fa3e6a3158'
            }
        },
        Description='S3 batch operations testing',
        Priority=128,
        RoleArn=role_arn
    )
    
    print(response)

def jobstatus(AccountID,jobId):
    response = s3c.describe_job(
    AccountId=AccountID,
    JobId=jobId
    )
    return(response)
    
def joblist(AccountID):
    response = s3c.list_jobs(
        AccountId=AccountID,
    )
    return(response)

# function to write data to dynamodb table
def write_data(body):
    table = dynamodb.Table(TABLE_CLASSIFIEDS)
    timestamp_str = body['createdAt']  # Example: '2025-06-23T19:06:47.953Z'
    dt = datetime.strptime(timestamp_str, "%Y-%m-%dT%H:%M:%S.%fZ")
    dt = dt.replace(tzinfo=timezone.utc)

    response = table.put_item(Item={
        'id': str(body['id']),
        'createdAt': body['createdAt'],
        'ts': int(dt.timestamp() * 1000),  # Convert to milliseconds
        'status': 'active',
        'updatedAt': body['createdAt'],
        'title': body['title'],
        'description': body['description'],
        'category': body['category'],
        'price': body['price'],
        'location': body['location'],
        'condition': body['condition'],
    })
    return(response)

# function to write data to dynamodb table
def query_data(TABLE_NAME):
    table = dynamodb.Table(TABLE_NAME)
    response = table.scan()
    return(response['Items'])

def get_index(bucket):
    response = s3.get_object(
        Bucket=bucket,
        Key='index.html',
    )
    return(response['Body'].read())






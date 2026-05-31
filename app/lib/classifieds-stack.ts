import {
  Stack, StackProps, RemovalPolicy, Duration, CfnOutput,
  aws_lambda as lambda,
  aws_iam as iam,
  aws_s3 as s3,
  aws_s3_deployment as s3deploy,
  aws_dynamodb as dynamodb,
  aws_cloudfront as cloudfront,
  aws_cloudfront_origins as origins,
} from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as path from 'path';

/**
 * Fresh minimal classifieds application.
 *
 * Stack layout:
 *   CloudFront ──/──────────▶ S3 site bucket   (static site, private via OAC)
 *              ├─/api/*─────▶ Lambda Function URL (Python API)
 *              └─/uploads/*─▶ S3 uploads bucket (listing images, private via OAC)
 *   Lambda ◀───────────────▶ DynamoDB (listings)
 *   Browser ──presigned POST▶ S3 uploads bucket (direct image upload)
 */
export class ClassifiedsStack extends Stack {
  constructor(scope: Construct, id: string, props?: StackProps) {
    super(scope, id, props);

    // DynamoDB table holding the classified listings
    const table = new dynamodb.Table(this, 'ListingsTable', {
      partitionKey: { name: 'id', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    // Private S3 bucket serving the static frontend
    const siteBucket = new s3.Bucket(this, 'SiteBucket', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    // Private S3 bucket holding user-uploaded listing images.
    // CORS allows the browser to PUT/POST directly via presigned URLs.
    const uploadsBucket = new s3.Bucket(this, 'UploadsBucket', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      cors: [{
        allowedMethods: [s3.HttpMethods.PUT, s3.HttpMethods.POST, s3.HttpMethods.GET],
        allowedOrigins: ['*'],
        allowedHeaders: ['*'],
        exposedHeaders: ['ETag'],
        maxAge: 3000,
      }],
    });

    // Python API Lambda (boto3 only, no layer required)
    const apiFn = new lambda.Function(this, 'ApiFn', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../lambda')),
      timeout: Duration.seconds(30),
      memorySize: 256,
      environment: {
        TABLE_NAME: table.tableName,
        UPLOAD_BUCKET: uploadsBucket.bucketName,
        MAX_IMAGES: '10',
      },
    });

    table.grantReadWriteData(apiFn);
    // Lambda needs PutObject to mint presigned POSTs the browser can use
    uploadsBucket.grantPut(apiFn);
    uploadsBucket.grantRead(apiFn);

    const apiUrl = apiFn.addFunctionUrl({
      authType: lambda.FunctionUrlAuthType.NONE,
      cors: {
        allowedOrigins: ['*'],
        allowedMethods: [lambda.HttpMethod.GET, lambda.HttpMethod.POST],
        allowedHeaders: ['*'],
      },
    });

    // Shared OAC origin for the uploads bucket (serve images through the CDN)
    const uploadsOrigin = origins.S3BucketOrigin.withOriginAccessControl(uploadsBucket);

    // CloudFront distribution: static site by default, /api/* to Lambda,
    // /uploads/* to the private uploads bucket.
    const distribution = new cloudfront.Distribution(this, 'Distribution', {
      comment: 'Classifieds dev distribution',
      defaultRootObject: 'index.html',
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(siteBucket),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
      },
    });

    distribution.addBehavior('/api/*', new origins.FunctionUrlOrigin(apiUrl), {
      viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
      allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
      cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
      originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
    });

    // Listing images served from S3 through the CDN
    distribution.addBehavior('/uploads/*', uploadsOrigin, {
      viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
      cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
      compress: true,
    });

    // Deploy the static frontend to S3 and invalidate the CDN cache
    new s3deploy.BucketDeployment(this, 'DeploySite', {
      sources: [s3deploy.Source.asset(path.join(__dirname, '../web'))],
      destinationBucket: siteBucket,
      distribution,
      distributionPaths: ['/*'],
    });

    new CfnOutput(this, 'DistributionUrl', {
      value: `https://${distribution.distributionDomainName}`,
      description: 'Public URL of the classifieds app',
    });
    new CfnOutput(this, 'TableName', {
      value: table.tableName,
      description: 'DynamoDB listings table',
    });
    new CfnOutput(this, 'UploadsBucketName', {
      value: uploadsBucket.bucketName,
      description: 'S3 bucket holding user-uploaded listing images',
    });
  }
}

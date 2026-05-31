import {
  Stack, StackProps, RemovalPolicy, Duration, CfnOutput,
  aws_lambda as lambda,
  aws_iam as iam,
  aws_s3 as s3,
  aws_s3_deployment as s3deploy,
  aws_dynamodb as dynamodb,
  aws_cloudfront as cloudfront,
  aws_cloudfront_origins as origins,
  aws_cognito as cognito,
} from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as path from 'path';

/**
 * Classifieds app stack.
 *
 * CloudFront ──/──────────▶ S3 site bucket   (static site, private via OAC)
 *            ├─/api/*─────▶ Lambda Function URL (Python API)
 *            └─/uploads/*─▶ S3 uploads bucket (listing images, private via OAC)
 *
 * Auth: Cognito User Pool with email-OTP custom auth flow.
 *   Three Lambda triggers handle define / create / verify challenge.
 *   SES sends the OTP email — set sesFromEmail CDK context before deploying.
 */
export class ClassifiedsStack extends Stack {
  constructor(scope: Construct, id: string, props?: StackProps) {
    super(scope, id, props);

    const lambdaDir = path.join(__dirname, '../lambda');

    // ── DynamoDB ──────────────────────────────────────────────────────────────
    const table = new dynamodb.Table(this, 'ListingsTable', {
      partitionKey: { name: 'id', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    // ── Cognito custom-auth Lambda triggers ───────────────────────────────────
    const defineAuthFn = new lambda.Function(this, 'DefineAuthFn', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'define_auth.handler',
      code: lambda.Code.fromAsset(lambdaDir),
      timeout: Duration.seconds(5),
      description: 'Cognito trigger: drives OTP custom auth flow',
    });

    // Sender email — set via CDK context: cdk deploy -c sesFromEmail=you@example.com
    const sesFromEmail = this.node.tryGetContext('sesFromEmail') || 'noreply@example.com';

    const createAuthFn = new lambda.Function(this, 'CreateAuthFn', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'create_auth.handler',
      code: lambda.Code.fromAsset(lambdaDir),
      timeout: Duration.seconds(15),
      description: 'Cognito trigger: generates OTP and sends it via SES',
      environment: {
        OTP_SENDER_EMAIL: sesFromEmail,
      },
    });

    // Allow the OTP sender Lambda to call SES SendEmail
    createAuthFn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['ses:SendEmail', 'ses:SendRawEmail'],
      resources: ['*'],
    }));

    const verifyAuthFn = new lambda.Function(this, 'VerifyAuthFn', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'verify_auth.handler',
      code: lambda.Code.fromAsset(lambdaDir),
      timeout: Duration.seconds(5),
      description: 'Cognito trigger: validates the OTP submitted by the user',
    });

    // ── Cognito User Pool ──────────────────────────────────────────────────────
    const userPool = new cognito.UserPool(this, 'UserPool', {
      selfSignUpEnabled: true,
      signInAliases: { email: true },
      autoVerify: { email: true },
      removalPolicy: RemovalPolicy.DESTROY,
      lambdaTriggers: {
        defineAuthChallenge: defineAuthFn,
        createAuthChallenge: createAuthFn,
        verifyAuthChallengeResponse: verifyAuthFn,
      },
    });

    const userPoolClient = new cognito.UserPoolClient(this, 'UserPoolClient', {
      userPool,
      authFlows: { custom: true },
      generateSecret: false,
      authSessionValidity: Duration.minutes(15),
    });

    // ── S3 buckets ─────────────────────────────────────────────────────────────
    const siteBucket = new s3.Bucket(this, 'SiteBucket', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

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

    // ── API Lambda ─────────────────────────────────────────────────────────────
    const apiFn = new lambda.Function(this, 'ApiFn', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset(lambdaDir),
      timeout: Duration.seconds(30),
      memorySize: 256,
      environment: {
        TABLE_NAME: table.tableName,
        UPLOAD_BUCKET: uploadsBucket.bucketName,
        MAX_IMAGES: '10',
        USER_POOL_ID: userPool.userPoolId,
        USER_POOL_CLIENT_ID: userPoolClient.userPoolClientId,
      },
    });

    table.grantReadWriteData(apiFn);
    uploadsBucket.grantPut(apiFn);
    uploadsBucket.grantRead(apiFn);

    // API Lambda needs these Cognito admin operations to manage users and auth
    userPool.grant(apiFn,
      'cognito-idp:AdminGetUser',
      'cognito-idp:AdminCreateUser',
      'cognito-idp:AdminSetUserPassword',
      'cognito-idp:AdminInitiateAuth',
      'cognito-idp:AdminRespondToAuthChallenge',
    );

    const apiUrl = apiFn.addFunctionUrl({
      authType: lambda.FunctionUrlAuthType.NONE,
      cors: {
        allowedOrigins: ['*'],
        allowedMethods: [lambda.HttpMethod.GET, lambda.HttpMethod.POST],
        allowedHeaders: ['*'],
      },
    });

    // ── CloudFront ─────────────────────────────────────────────────────────────
    const uploadsOrigin = origins.S3BucketOrigin.withOriginAccessControl(uploadsBucket);

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

    distribution.addBehavior('/uploads/*', uploadsOrigin, {
      viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
      cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
      compress: true,
    });

    new s3deploy.BucketDeployment(this, 'DeploySite', {
      sources: [s3deploy.Source.asset(path.join(__dirname, '../web'))],
      destinationBucket: siteBucket,
      distribution,
      distributionPaths: ['/*'],
    });

    // ── Outputs ────────────────────────────────────────────────────────────────
    new CfnOutput(this, 'DistributionUrl', {
      value: `https://${distribution.distributionDomainName}`,
      description: 'Public URL of the classifieds app',
    });
    new CfnOutput(this, 'UserPoolId', {
      value: userPool.userPoolId,
      description: 'Cognito User Pool ID',
    });
    new CfnOutput(this, 'UserPoolClientId', {
      value: userPoolClient.userPoolClientId,
      description: 'Cognito User Pool Client ID',
    });
    new CfnOutput(this, 'TableName', {
      value: table.tableName,
      description: 'DynamoDB listings table',
    });
    new CfnOutput(this, 'UploadsBucketName', {
      value: uploadsBucket.bucketName,
      description: 'S3 bucket for listing images',
    });
    new CfnOutput(this, 'SesSetupNote', {
      value: [
        `OTP emails will be sent from: ${sesFromEmail}`,
        'Verify this address in SES (Identity Management > Email addresses).',
        'In SES sandbox mode, also verify every recipient email.',
        'To change the sender: cdk deploy -c sesFromEmail=you@yourdomain.com',
      ].join(' | '),
      description: 'ACTION REQUIRED: verify sender email in SES before auth will work',
    });
  }
}

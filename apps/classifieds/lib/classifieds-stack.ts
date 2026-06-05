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
  aws_ses as ses,
  aws_ses_actions as sesActions,
  aws_certificatemanager as acm,
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
 *
 * Email receiving: SES receipt rule stores inbound email under
 *   uploads-bucket/emails/<recipient@domain>/<messageId>.eml
 *   Requires sesReceiveDomain CDK context + MX record on that domain.
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
    const pagesBucket = new s3.Bucket(this, 'PagesBucket', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

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

    // Allow SES to write raw inbound emails to the uploads bucket
    uploadsBucket.addToResourcePolicy(new iam.PolicyStatement({
      sid: 'AllowSESInboundPut',
      principals: [new iam.ServicePrincipal('ses.amazonaws.com')],
      actions: ['s3:PutObject'],
      resources: [uploadsBucket.arnForObjects('emails/raw/*')],
      conditions: {
        StringEquals: { 'aws:Referer': this.account },
      },
    }));

    // ── SES context variables (needed by both email ingest and API Lambda) ──────
    // Set via CDK context: cdk deploy -c sesReceiveDomain=highlyclassifieds.com
    // Note: SES email receiving is only available in us-east-1, us-west-2, eu-west-1
    const sesReceiveDomain = this.node.tryGetContext('sesReceiveDomain') || '';

    // ── Email ingest Lambda ────────────────────────────────────────────────────
    const emailIngestFn = new lambda.Function(this, 'EmailIngestFn', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'email_ingest.handler',
      code: lambda.Code.fromAsset(lambdaDir),
      timeout: Duration.seconds(30),
      memorySize: 128,
      description: 'Stores inbound SES emails to emails/<cognito-sub>/ in S3 and forwards to real inbox',
      environment: {
        UPLOAD_BUCKET: uploadsBucket.bucketName,
        USER_POOL_ID: userPool.userPoolId,
        SES_RECEIVE_DOMAIN: sesReceiveDomain,
        SES_FROM_EMAIL: sesFromEmail,
      },
    });

    // Ingest Lambda: read/write S3, lookup Cognito users, forward via SES
    uploadsBucket.grantReadWrite(emailIngestFn);
    userPool.grant(emailIngestFn, 'cognito-idp:ListUsers');
    emailIngestFn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['ses:SendEmail', 'ses:SendRawEmail'],
      resources: ['*'],
    }));

    // ── SES email receiving ────────────────────────────────────────────────────
    // Also requires an MX record: sesReceiveDomain → inbound-smtp.<region>.amazonaws.com
    const receiptRuleSet = new ses.ReceiptRuleSet(this, 'EmailReceiptRuleSet', {
      receiptRuleSetName: 'classifieds-inbound',
    });

    receiptRuleSet.addRule('InboxRule', {
      // Empty recipients list = catch-all for the domain once MX is pointed here.
      // When sesReceiveDomain is set, scope to that domain only.
      recipients: sesReceiveDomain ? [sesReceiveDomain] : [],
      scanEnabled: true,
      tlsPolicy: ses.TlsPolicy.OPTIONAL,
      actions: [
        // Action 1: write the raw email to emails/raw/<messageId>
        new sesActions.S3({
          bucket: uploadsBucket,
          objectKeyPrefix: 'emails/raw/',
        }),
        // Action 2: Lambda re-keys it to emails/<recipient>/<messageId>.eml and removes raw copy
        new sesActions.Lambda({
          function: emailIngestFn,
          invocationType: sesActions.LambdaInvocationType.EVENT,
        }),
      ],
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
        SES_FROM_EMAIL: sesFromEmail,
        SES_RECEIVE_DOMAIN: sesReceiveDomain,
      },
    });

    table.grantReadWriteData(apiFn);
    uploadsBucket.grantPut(apiFn);
    uploadsBucket.grantRead(apiFn);

    // API Lambda needs Cognito admin operations and SES for the interest relay
    userPool.grant(apiFn,
      'cognito-idp:AdminGetUser',
      'cognito-idp:AdminCreateUser',
      'cognito-idp:AdminSetUserPassword',
      'cognito-idp:AdminInitiateAuth',
      'cognito-idp:AdminRespondToAuthChallenge',
    );
    apiFn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['ses:SendEmail', 'ses:SendRawEmail'],
      resources: ['*'],
    }));

    const apiUrl = apiFn.addFunctionUrl({
      authType: lambda.FunctionUrlAuthType.NONE,
      cors: {
        allowedOrigins: ['*'],
        allowedMethods: [lambda.HttpMethod.GET, lambda.HttpMethod.POST],
        allowedHeaders: ['*'],
      },
    });

    // ── CloudFront Function — strip /pages prefix before serving from pagesBucket
    const stripPagesPrefixFn = new cloudfront.Function(this, 'StripPagesPrefixFn', {
      code: cloudfront.FunctionCode.fromInline(`
function handler(event) {
  var req = event.request;
  // /pages/about.html → /about.html
  req.uri = req.uri.slice(6) || '/index.html';
  return req;
}
      `.trim()),
      comment: 'Strip /pages prefix for S3 pages bucket origin',
    });

    // ── CloudFront ─────────────────────────────────────────────────────────────
    // Optional custom domain. Set via CDK context:
    //   cdk deploy -c cfDomain=dev.highlyclassifieds.com -c cfCertArn=arn:aws:acm:us-east-1:...
    // The ACM certificate must exist in us-east-1 and cover the cfDomain before deploying.
    const cfDomain = this.node.tryGetContext('cfDomain') || '';
    const cfCertArn = this.node.tryGetContext('cfCertArn') || '';

    const certificate = cfCertArn
      ? acm.Certificate.fromCertificateArn(this, 'SiteCert', cfCertArn)
      : undefined;

    const uploadsOrigin = origins.S3BucketOrigin.withOriginAccessControl(uploadsBucket);

    const distribution = new cloudfront.Distribution(this, 'Distribution', {
      comment: 'Classifieds dev distribution',
      defaultRootObject: 'index.html',
      ...(cfDomain && certificate ? {
        domainNames: [cfDomain],
        certificate,
      } : {}),
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

    distribution.addBehavior('/pages/*', origins.S3BucketOrigin.withOriginAccessControl(pagesBucket), {
      viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
      cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
      compress: true,
      functionAssociations: [{
        function: stripPagesPrefixFn,
        eventType: cloudfront.FunctionEventType.VIEWER_REQUEST,
      }],
    });

    new s3deploy.BucketDeployment(this, 'DeploySite', {
      sources: [s3deploy.Source.asset(path.join(__dirname, '../web'))],
      destinationBucket: siteBucket,
      distribution,
      distributionPaths: ['/*'],
    });

    new s3deploy.BucketDeployment(this, 'DeployPages', {
      sources: [s3deploy.Source.asset(path.join(__dirname, '../pages'))],
      destinationBucket: pagesBucket,
      distribution,
      distributionPaths: ['/pages/*'],
    });

    // ── Outputs ────────────────────────────────────────────────────────────────
    new CfnOutput(this, 'DistributionUrl', {
      value: cfDomain ? `https://${cfDomain}` : `https://${distribution.distributionDomainName}`,
      description: 'Public URL of the classifieds app',
    });
    new CfnOutput(this, 'DistributionDomain', {
      value: distribution.distributionDomainName,
      description: 'CloudFront domain — point your CNAME here',
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
    new CfnOutput(this, 'PagesBucketName', {
      value: pagesBucket.bucketName,
      description: 'S3 bucket for static pages (About, FAQ, Terms)',
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
    new CfnOutput(this, 'EmailReceivingNote', {
      value: sesReceiveDomain
        ? [
            `Receiving email for domain: ${sesReceiveDomain}`,
            `Add MX record: ${sesReceiveDomain} → inbound-smtp.${this.region}.amazonaws.com (priority 10)`,
            'Activate rule set in SES console: Email receiving > Receipt rule sets > classifieds-inbound > Set as active',
            'Emails land at: uploads-bucket/emails/<cognito-sub>/<messageId>.txt and are forwarded to real inbox',
          ].join(' | ')
        : 'Email receiving disabled. Deploy with: cdk deploy -c sesReceiveDomain=mail.yourdomain.com',
      description: 'Email receiving setup — SES inbound to S3',
    });
  }
}

import { 
  Stack, StackProps, Duration, RemovalPolicy, CfnOutput, Token, Lazy, EncodingOptions, Fn,
  aws_lambda as lambda,
  aws_iam as iam,
  aws_certificatemanager as acm,
  aws_s3 as s3,
  aws_s3_deployment as s3deploy,
  aws_dynamodb as dynamodb,
  aws_ssm as ssm,
  aws_logs as logs,
  aws_cloudfront as cloudfront,
  aws_cloudfront_origins as origins, 
  aws_apigateway as apigateway,
  aws_cognito as cognito,
} from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as path from 'path';

export class LambdaWithLayer extends Stack {
  public fnUrl: string
  public apiUrl: string

  //BeginStackDefinition
  constructor(scope: Construct, id: string, props?: StackProps) {
    super(scope, id, props);

    console.log('accessing context 👉', this.node.tryGetContext('fromApp'));

    // Create a loggroup for the resources in this stack
    const hiclasLogGroup = new logs.LogGroup(this, 'hiclasLogGroup', {
      logGroupName: `/aws/${id}/logs`,
      removalPolicy: RemovalPolicy.DESTROY,
      retention: logs.RetentionDays.ONE_WEEK,
    });

    // -------------------------------------------------------------------------
    // Cognito User Pool — restricts access to dev.highlyclassifieds.com
    // -------------------------------------------------------------------------
    const userPool = new cognito.UserPool(this, 'HiclasUserPool', {
      userPoolName: `${id}-users`,
      selfSignUpEnabled: false,       // only admins can create accounts
      signInAliases: { email: true },
      passwordPolicy: {
        minLength: 8,
        requireUppercase: true,
        requireLowercase: true,
        requireDigits: true,
        requireSymbols: false,
      },
      accountRecovery: cognito.AccountRecovery.EMAIL_ONLY,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    const userPoolClient = new cognito.UserPoolClient(this, 'HiclasUserPoolClient', {
      userPool,
      generateSecret: false,
      oAuth: {
        flows: { authorizationCodeGrant: true },
        scopes: [
          cognito.OAuthScope.OPENID,
          cognito.OAuthScope.EMAIL,
          cognito.OAuthScope.PROFILE,
        ],
        callbackUrls: [
          'https://dev.highlyclassifieds.com/auth/callback',
          'https://fn.theworkingmethods.com/auth/callback',
        ],
        logoutUrls: [
          'https://dev.highlyclassifieds.com/',
          'https://fn.theworkingmethods.com/',
        ],
      },
      supportedIdentityProviders: [cognito.UserPoolClientIdentityProvider.COGNITO],
    });

    const userPoolDomain = new cognito.UserPoolDomain(this, 'HiclasUserPoolDomain', {
      userPool,
      cognitoDomain: { domainPrefix: 'hiclas-dev-auth' },
    });

    // -------------------------------------------------------------------------
    // Lambda@Edge — auth gate (origin-request, Node.js 20)
    // Runs on every cache miss; verifies Cognito id_token cookie.
    // origin-request supports environment variables (viewer-request does not).
    // -------------------------------------------------------------------------
    const edgeFunction = new cloudfront.experimental.EdgeFunction(this, 'EdgeFunction', {
      runtime: lambda.Runtime.NODEJS_20_X,
      handler: 'authfn.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../src')),
      memorySize: 128,
      timeout: Duration.seconds(5),
      description: 'Cognito auth gate for hiclas CloudFront distribution',
      environment: {
        COGNITO_USER_POOL_ID: userPool.userPoolId,
        COGNITO_CLIENT_ID:    userPoolClient.userPoolClientId,
        COGNITO_DOMAIN:       `${userPoolDomain.domainName}.auth.${this.region}.amazoncognito.com`,
        APP_DOMAIN:           'dev.highlyclassifieds.com',
        COGNITO_REGION:       this.region,
      },
    });

    const authEdge = [{
      functionVersion: edgeFunction.currentVersion,
      eventType: cloudfront.LambdaEdgeEventType.ORIGIN_REQUEST,
    }];

    // Lambda layer creation definition
    const layer0 = new lambda.LayerVersion(this, 'LayerVersion', {
      compatibleRuntimes: [
        lambda.Runtime.PYTHON_3_12,
      ],
      code: lambda.Code.fromAsset(path.join(__dirname,'../../layer/bin')),
      });

    const s3Bucket = new s3.Bucket(this, 's3inventory', {
      objectOwnership: s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      versioned: true,
    });

    s3Bucket.grantRead(new iam.AccountRootPrincipal());
    s3Bucket.grantPut(new iam.AccountRootPrincipal());

    /**
     * Create an s3 bucket as backend storage
     */
    const hiclastore = new s3.Bucket(this, 'hiclastore', {
      objectOwnership: s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
      publicReadAccess: false,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      versioned: true,
    });

    /**
     * Create an s3 policy to allow uploads to the bucket
     * This policy will be used for presigned url to upload files to the bucket
     */
    const s3UploadPolicy = new iam.Policy(this, `${id}S3UploadPolicy`, {
      statements: [
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            's3:PutObject'
          ],
          resources: [
            hiclastore.arnForObjects('*'),
            hiclastore.bucketArn
          ],
        }),
      ],
    });

    /**
     * Create an IAM role for the S3 upload policy
     */
    const s3UploadRole = new iam.Role(this, `${id}S3UploadRole`, {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      description: 'Role for S3 upload policy',
      inlinePolicies: {
        S3UploadPolicy: s3UploadPolicy.document,
      },
    });
          
    //Index function definition
    const indexfn = new lambda.Function(this, 'indexfn', {
      description: 'hiclas index function',
      runtime: lambda.Runtime.PYTHON_3_12,
      timeout: Duration.seconds(30),
      handler: 'indexfn.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../src')),
      layers: [layer0],
      logGroup: hiclasLogGroup,
      environment: {
        APPNAME: process.env.ApplicationName!,
        ENVNAME: process.env.Environment!, 
        BUCKET_STORE: hiclastore.bucketName,
        S3_UPLOAD_ROLE: s3UploadRole.roleArn,
        },
      });
    
      indexfn.addToRolePolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      resources: [
        hiclastore.arnForObjects("*"),
        hiclastore.bucketArn
      ],
      actions: [
        's3:PutObject',
        's3:GetObject',
        's3:ListBucket'
      ],
      }));

      s3UploadRole.assumeRolePolicy?.addStatements(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          principals: [new iam.ArnPrincipal(indexfn.role!.roleArn)],
          actions: ['sts:AssumeRole'],
        })
      );

    const indexfnUrl = indexfn.addFunctionUrl({
      authType: lambda.FunctionUrlAuthType.NONE,
      cors: {
        allowedOrigins: ['*'],
        allowedMethods: [lambda.HttpMethod.GET, lambda.HttpMethod.POST],
        allowedHeaders: ['*'],
      }
    })

    //Index function definition
    const apifn = new lambda.Function(this, 'apifn', {
      description: 'hiclas api function',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'apifn.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../src')),
      layers: [layer0],
      logGroup: hiclasLogGroup,
      environment: {
        APPNAME: process.env.ApplicationName!,
        ENVNAME: process.env.Environment!, 
        },
      });
    
      apifn.addToRolePolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      resources: [
        s3Bucket.arnForObjects("*"),
        s3Bucket.bucketArn
      ],
      actions: [
        's3:PutObject',
        's3:GetObject',
        's3:ListBucket'
      ],
      }));

    /**
     * Create an Origin Access Identity (OAI) for CloudFront to securely access the S3 bucket
     */
    const originAccessIdentity = new cloudfront.OriginAccessIdentity(this, 'OAI');

    /**
     * Grant CloudFront access to the bucket
     */
    hiclastore.addToResourcePolicy(new iam.PolicyStatement({
      actions: ['s3:GetObject'],
      resources: [hiclastore.arnForObjects('*')],
      principals: [new iam.CanonicalUserPrincipal(originAccessIdentity.cloudFrontOriginAccessIdentityS3CanonicalUserId)],
    }));


    const s3BucketOrigin = origins.S3BucketOrigin.withOriginAccessControl(s3Bucket, {
      originAccessLevels: [cloudfront.AccessLevel.READ, cloudfront.AccessLevel.LIST],
    });

    const certificateArn = `arn:aws:acm:${process.env.CDK_DEFAULT_REGION}:${process.env.CDK_DEFAULT_ACCOUNT}:certificate/e2803f4f-7240-4f20-8fab-510f8a833e15`;

    const domainCert = acm.Certificate.fromCertificateArn(this, 'domainCert', certificateArn);

    // Create an S3 bucket origin for the CloudFront distribution
    const hiclastoreOrigin = origins.S3BucketOrigin.withOriginAccessControl(hiclastore, {
      originAccessLevels: [cloudfront.AccessLevel.READ, cloudfront.AccessLevel.LIST],
    });

    const indexfnOrigin = new origins.FunctionUrlOrigin(indexfnUrl)

    //Import the certificate from ARN
    const hiclasCertArn = `arn:aws:acm:${process.env.CDK_DEFAULT_REGION}:${process.env.CDK_DEFAULT_ACCOUNT}:certificate/a37cb872-61aa-4027-9227-2099c27ec7ec`;
    const hiclasCert = acm.Certificate.fromCertificateArn(this, 'hiclasCert', hiclasCertArn);


    // Use the OAC to create a CloudFront distribution
    const hiclasDist = new cloudfront.Distribution(this, 'hiclasDist', {
      comment: 'Distribution for hiclas deployment',
      defaultBehavior: {
        origin: hiclastoreOrigin,
        allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
        originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
        edgeLambdas: authEdge,
      },
      domainNames: ['fn.theworkingmethods.com','dev.highlyclassifieds.com'],
      certificate: hiclasCert,
      defaultRootObject: 'index.html',
    });

    /**
     * Behavior for API calls — auth enforced via edge function
     */
    hiclasDist.addBehavior('/api/*', indexfnOrigin, {
      allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
      cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
      viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
      edgeLambdas: authEdge,
    });

    /**
     * Behavior for POST calls — auth enforced via edge function
     */
    hiclasDist.addBehavior('/post', indexfnOrigin, {
      allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
      cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
      viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
      edgeLambdas: authEdge,
    });

    /**
     * Behavior for /listing/* — auth enforced via edge function
     */
    hiclasDist.addBehavior('/listing/*', indexfnOrigin, {
      allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
      cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
      viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
      edgeLambdas: authEdge,
    });

    /**
     * Behavior for CSS files — static, no auth needed
     */
    hiclasDist.addBehavior('/css/*', hiclastoreOrigin, {
      cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
      viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
    });

    /**
     * Behavior for Assets files — static, no auth needed
     */
    hiclasDist.addBehavior('/assets/*', hiclastoreOrigin, {
      cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
      viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
    });

    /**
     * Behavior for images — static, no auth needed
     */
    hiclasDist.addBehavior('/images/*', hiclastoreOrigin, {
      cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
      viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
      compress: true
    });

    /**
     * Behavior for user-uploaded listing images — static, no auth needed
     */
    hiclasDist.addBehavior('/uploads/*', hiclastoreOrigin, {
      cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
      viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
      compress: true,
    });

    /**
     * Deploy CSS files to the S3 bucket.
     */
    new s3deploy.BucketDeployment(this, 'DeployCSS', {
      sources: [s3deploy.Source.asset('../iac/src/css')], 
      destinationBucket: hiclastore,
      destinationKeyPrefix: 'css/',
      prune: false,
      retainOnDelete: false,
    });

    /**
     * Deploy assets files to the S3 bucket.
     */
    new s3deploy.BucketDeployment(this, 'DeployAssets', {
      sources: [s3deploy.Source.asset('../dist/')],
      destinationBucket: hiclastore,
      prune: false,
      retainOnDelete: false,
    });

    /**
     * Deploy images files to the S3 bucket.
     */
    new s3deploy.BucketDeployment(this, 'DeployImages', {
      sources: [s3deploy.Source.asset('../iac/src/images/')],
      destinationBucket: hiclastore,
      destinationKeyPrefix: 'images/',
      prune: false,
      retainOnDelete: false,
    });

    /**
     * Create DynamoDB Table data store
     */ 
    const classifiedsTable = new dynamodb.Table(this, 'ClassifiedsTable', {
      partitionKey: { name: 'id', type: dynamodb.AttributeType.STRING }, 
      sortKey: { name: 'category', type: dynamodb.AttributeType.STRING }, 
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    /**
     * Enable lambda access to dynamodb
     */
    classifiedsTable.grantReadWriteData(indexfn);
    indexfn.addEnvironment('TABLE_CLASSIFIEDS', classifiedsTable.tableName)

    // Create a deny statement for any other accounts to prevent them from invoking the API Gateway
    const apiResourcePolicy = new iam.PolicyStatement({
      effect: iam.Effect.DENY,
      principals: [new iam.AccountRootPrincipal()],
      actions: ['execute-api:Invoke'],
      resources: ['arn:aws:execute-api:*:*:*/*/*/*'],
      conditions: {
        'StringNotEquals': {
          'aws:SourceAccount': process.env.CDK_DEFAULT_ACCOUNT,
        },
      },
    });

    // Create another policy statement to allow CloudFront to invoke the API Gateway
    const cloudfrontInvokePolicy = new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      principals: [new iam.ServicePrincipal('cloudfront.amazonaws.com')],
      actions: ['execute-api:Invoke'],
      resources: ['arn:aws:execute-api:*:*:*/*/*/*']
    });

    // Create a private API Gateway for the backend with resource policy
    const hiclasapi = new apigateway.RestApi(this, 'hiclasapi', {
      endpointConfiguration: {
        types: [apigateway.EndpointType.PRIVATE],
      },
      policy: new iam.PolicyDocument({
        statements: [apiResourcePolicy, cloudfrontInvokePolicy],
      }),
      deployOptions: {
        stageName: 'api',
        loggingLevel: apigateway.MethodLoggingLevel.INFO,
        dataTraceEnabled: true,
        metricsEnabled: true,
      },
      description: 'Hiclas API Gateway',
      cloudWatchRole: true,
    });

    //Integrate the apifn lambda with the backend api gateway
    const hiclasapiIntegration = new apigateway.LambdaIntegration(apifn)
    //Add a resource to the API Gateway for the root path
    const rootResource = hiclasapi.root.addResource('api');
    //Add a method to the root resource that integrates with the apifn lambda
    rootResource.addMethod('ANY', hiclasapiIntegration, {
      methodResponses: [
        {
          statusCode: '200',
          responseModels: {
            'application/json': apigateway.Model.EMPTY_MODEL,
          },
        },
        {
          statusCode: '400',
          responseModels: {
            'application/json': apigateway.Model.EMPTY_MODEL,
          },
        },
        {
          statusCode: '500',
          responseModels: {
            'application/json': apigateway.Model.EMPTY_MODEL,
          },
        },
      ],
      requestParameters: {
        'method.request.header.Content-Type': true,
        'method.request.header.Accept': true,
        'method.request.header.Authorization': true,
      },
      requestModels: {
        'application/json': apigateway.Model.EMPTY_MODEL,
      },
    });

    // Add proxy resource for /api/*
    const proxyResource = rootResource.addResource('{proxy+}');
    proxyResource.addMethod('ANY', hiclasapiIntegration);

    // -------------------------------------------------------------------------
    // Outputs
    // -------------------------------------------------------------------------
    new CfnOutput(this, 'CognitoUserPoolId', {
      value: userPool.userPoolId,
      description: 'Cognito User Pool ID — use this to create users via AWS CLI or console',
    });

    new CfnOutput(this, 'CognitoClientId', {
      value: userPoolClient.userPoolClientId,
      description: 'Cognito App Client ID',
    });

    new CfnOutput(this, 'CognitoHostedUiDomain', {
      value: `https://${userPoolDomain.domainName}.auth.${this.region}.amazoncognito.com`,
      description: 'Cognito Hosted UI base URL',
    });

  //EndStack
  }}

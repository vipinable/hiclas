import { Stack, StackProps, Duration, RemovalPolicy, CfnOutput, Token, Lazy, EncodingOptions, Fn } from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as s3deploy from 'aws-cdk-lib/aws-s3-deployment';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb'
import * as ssm from 'aws-cdk-lib/aws-ssm';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins'; 
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as path from 'path';
// import { EdgeFunction } from 'aws-cdk-lib/aws-cloudfront/lib/experimental';
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
      removalPolicy: RemovalPolicy.DESTROY, // Change to RETAIN for production
      retention: logs.RetentionDays.ONE_WEEK, // Change to your desired retention period
    });


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
      authType: lambda.FunctionUrlAuthType.NONE, // No authentication for the function URL
      cors: {
        allowedOrigins: ['*'], // Allow all origins, adjust as needed
        allowedMethods: [lambda.HttpMethod.GET, lambda.HttpMethod.POST], // Allow GET and POST methods
        allowedHeaders: ['*'], // Allow all headers, adjust as needed
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

    // Create a Cloudfront edge@Lambda function
    const edgeFunction = new cloudfront.experimental.EdgeFunction(this, 'EdgeFunction', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'edgefn.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../src')),
      memorySize: 128,
      logGroup: hiclasLogGroup,
      description: 'Edge function for hiclas CloudFront distribution',
    });

    edgeFunction.addToRolePolicy(new iam.PolicyStatement({
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

    // Add permission to invoke the edge function from CloudFront
    edgeFunction.addPermission('AllowCloudFrontInvoke', {
      principal: new iam.ServicePrincipal('edgelambda.amazonaws.com'),
      action: 'lambda:InvokeFunction',
      sourceArn: `arn:aws:cloudfront::${process.env.CDK_DEFAULT_ACCOUNT}:distribution/*`, // Replace with your CloudFront distribution ARN
      sourceAccount: process.env.CDK_DEFAULT_ACCOUNT,
    });

    // // Define a custom OAC
    // const oac = new cloudfront.FunctionUrlOriginAccessControl(this, 'MyOAC', {
    //   signing: cloudfront.Signing.SIGV4_ALWAYS // No signing required for Function URL
    // });

    // const indexfnOrigin = origins.FunctionUrlOrigin.withOriginAccessControl(indexfnUrl, {
    //   originAccessControl: oac,
    // })

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
        // origin: indexfnOrigin,
        origin: hiclastoreOrigin,
        allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
        originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
      },
      domainNames: ['fn.theworkingmethods.com','dev.highlyclassifieds.com'],
      certificate: hiclasCert,
      defaultRootObject: 'index.html',
    });

    /**
     * Behavior for API calls
     */
    hiclasDist.addBehavior('/api/*', indexfnOrigin, {
      allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
      cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
      viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
    });

    /**
     * Behavior for POST calls
     */
    hiclasDist.addBehavior('/post', indexfnOrigin, {
      allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
      cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
      viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
    });


    /**
     * Behavior for /listing/*
     */
    hiclasDist.addBehavior('/listing/*', indexfnOrigin, {
      allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
      cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
      viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
    });

    /**
     * Behavior for CSS files
     */
    hiclasDist.addBehavior('/css/*', hiclastoreOrigin, {
      cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
      viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
    });

    /**
     * Behavior for Assets files
     */
    hiclasDist.addBehavior('/assets/*', hiclastoreOrigin, {
      cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
      viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
    });

    /**
     * Behavior for images
     */
    hiclasDist.addBehavior('/images/*', hiclastoreOrigin, {
      cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
      viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
      compress: true
    });

    /**
     * Deploy CSS files to the S3 bucket.
     */
    new s3deploy.BucketDeployment(this, 'DeployCSS', {
      sources: [s3deploy.Source.asset('../iac/src/css')], 
      destinationBucket: hiclastore,
      destinationKeyPrefix: 'css/',
      prune: false, // Set to true to remove files not in the source
      retainOnDelete: false, // Set to true to retain files on stack deletion
    });

    /**
     * Deploy assets files to the S3 bucket.
     */
    new s3deploy.BucketDeployment(this, 'DeployAssets', {
      sources: [s3deploy.Source.asset('../dist/')],
      destinationBucket: hiclastore,
      // destinationKeyPrefix: 'assets/',
      prune: false, // Set to true to remove files not in the source
      retainOnDelete: false, // Set to true to retain files on stack deletion
    });

    /**
     * Deploy images files to the S3 bucket.
     */
    new s3deploy.BucketDeployment(this, 'DeployImages', {
      sources: [s3deploy.Source.asset('../iac/src/images/')],
      destinationBucket: hiclastore,
      destinationKeyPrefix: 'images/',
      prune: false, // Set to true to remove files not in the source
      retainOnDelete: false, // Set to true to retain files on stack deletion
    });

    /**
     * Create DynamoDB Table data store
     */ 
    const classifiedsTable = new dynamodb.Table(this, 'ClassifiedsTable', {
      partitionKey: { name: 'id', type: dynamodb.AttributeType.STRING }, 
      sortKey: { name: 'category', type: dynamodb.AttributeType.STRING }, 
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST, // On-demand pricing
      removalPolicy: RemovalPolicy.DESTROY, // Change to RETAIN for production
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
      cloudWatchRole: true, // Enable CloudWatch logging  
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
            'application/json': apigateway.Model.EMPTY_MODEL, // Use an empty model for simplicity
          },
        },
        {
          statusCode: '400',
          responseModels: {
            'application/json': apigateway.Model.EMPTY_MODEL, // Use an empty model for simplicity
          },
        },
        {
          statusCode: '500',
          responseModels: {
            'application/json': apigateway.Model.EMPTY_MODEL, // Use an empty model for simplicity
          },
        },
      ],
      requestParameters: {
        'method.request.header.Content-Type': true, // Allow Content-Type header
        'method.request.header.Accept': true, // Allow Accept header
        'method.request.header.Authorization': true, // Allow Authorization header
      },
      requestModels: {
        'application/json': apigateway.Model.EMPTY_MODEL, // Use an empty model for simplicity
      },
    });

    // Add proxy resource for /api/*
    const proxyResource = rootResource.addResource('{proxy+}');
    proxyResource.addMethod('ANY', hiclasapiIntegration);


    // //Add beheavior for api gateway and forward requests to apigateway
    // // hiclasDist.addBehavior('/api/*', new origins.HttpOrigin(hiclasapiIntegration.url.split('/')[2]), {
    // hiclasDist.addBehavior('/api/*', new origins.RestApiOrigin(hiclasapi), {
    //   viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
    //   allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
    //   cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
    //   originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
    // });

    // //export the apiUrl as a CfnOutput
    // new CfnOutput(this, 'ApiUrl', {
    //   value: hiclasapi.url.split('/')[2], // Extract the domain part from the full URL
    //   description: 'The URL of the Hiclas API Gateway',
    //   exportName: 'HiclasApiUrl', // Export name for cross-stack references
    // });


  //EndStack
  }}

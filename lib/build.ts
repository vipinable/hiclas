import { Stack, StackProps, Duration, RemovalPolicy, CfnOutput, Token, Lazy, EncodingOptions, Fn } from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as s3deploy from 'aws-cdk-lib/aws-s3-deployment';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins'; 
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as path from 'path';
import { EdgeFunction } from 'aws-cdk-lib/aws-cloudfront/lib/experimental';
export class LambdaWithLayer extends Stack {
  public fnUrl: string

  //BeginStackDefinition
  constructor(scope: Construct, id: string, props?: StackProps) {
    super(scope, id, props);

    console.log('accessing context 👉', this.node.tryGetContext('fromApp'));

    //Lambda layer creation definition
    const layer0 = new lambda.LayerVersion(this, 'LayerVersion', {
      compatibleRuntimes: [
        lambda.Runtime.PYTHON_3_6,
        lambda.Runtime.PYTHON_3_7,
        lambda.Runtime.PYTHON_3_8,
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
          
    //Index function definition
    const indexfn = new lambda.Function(this, 'indexfn', {
      description: 'hiclas index function',
      runtime: lambda.Runtime.PYTHON_3_8,
      handler: 'main.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../src')),
      layers: [layer0],
      environment: {
        APPNAME: process.env.ApplicationName!,
        ENVNAME: process.env.Environment!, 
      },
      });
    
      indexfn.addToRolePolicy(new iam.PolicyStatement({
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

    const indexfnUrl = indexfn.addFunctionUrl({
      authType: lambda.FunctionUrlAuthType.NONE
    })

    // const apigw = new apigateway.RestApi(this, 'apigw');
       
    // //API gateway lambda integration
    // const apigwbeIntegration = new apigateway.LambdaIntegration(mainfn);
    // apigw.root.addMethod('GET', apigwbeIntegration);

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

    /**
     * Create s3 origin for CloudFront
     */
    const hiclastoreOrigin = new cloudfront.origins.S3Origin(hiclastore, {
      originAccessIdentity: originAccessIdentity,
    })

    // const cfmainfn = new cloudfront.experimental.EdgeFunction(this, 'cfmainfn', {
    //   runtime: lambda.Runtime.PYTHON_3_8,
    //   handler: 'main.handler',
    //   code: lambda.Code.fromAsset(path.join(__dirname, '../src')),
    // });

    // cfmainfn.addToRolePolicy(new iam.PolicyStatement({
    //   effect: iam.Effect.ALLOW,
    //   resources: [
    //     s3Bucket.arnForObjects("*"),
    //     s3Bucket.bucketArn
    //   ],
    //   actions: [
    //     's3:PutObject',
    //     's3:GetObject',
    //     's3:ListBucket'
    //   ],
    //   }));

    const certificateArn = `arn:aws:acm:${process.env.CDK_DEFAULT_REGION}:${process.env.CDK_DEFAULT_ACCOUNT}:certificate/e2803f4f-7240-4f20-8fab-510f8a833e15`;

    const domainCert = acm.Certificate.fromCertificateArn(this, 'domainCert', certificateArn);

    const hiclasDist = new cloudfront.Distribution(this, 'hiclasDist', {
      comment: 'Distribution for hiclas deployment',
      defaultBehavior: { 
        origin: new origins.HttpOrigin(Fn.parseDomainName(indexfnUrl.url)), 
        allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
        originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
      },
      domainNames: ['fn.theworkingmethods.com'],
      certificate: domainCert
      // defaultRootObject: 'index.html'
    });

    /**
     * Behavior for CSS files
     */
    hiclasDist.addBehavior('/css/*', hiclastoreOrigin, {
      cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
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
     * Deploy CSS files to the S3 bucket
     */
    new s3deploy.BucketDeployment(this, 'DeployCSS', {
      sources: [s3deploy.Source.asset('../src/css')], 
      destinationBucket: hiclastore,
      destinationKeyPrefix: 'css/'
    });

    // hiclasDist.addBehavior('/function', fnUrlOrigin)
    //cfmainfn.grantInvoke('cloudfront.amazonaws.com')

  //EndStack
  }}

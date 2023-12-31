import { Stack, StackProps, Duration, RemovalPolicy } from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins'; 
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as path from 'path';
import { EdgeFunction } from 'aws-cdk-lib/aws-cloudfront/lib/experimental';
export class LambdaWithLayer extends Stack {
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
          
    //Main function definition
    const mainfn = new lambda.Function(this, 'mainfn', {
      description: 'hiclas main function',
      runtime: lambda.Runtime.PYTHON_3_8,
      handler: 'main.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../src')),
      layers: [layer0],
      environment: {
        APPNAME: process.env.ApplicationName!,
        ENVNAME: process.env.Environment!, 
      },
      });
    
      mainfn.addToRolePolicy(new iam.PolicyStatement({
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

    const apigw = new apigateway.RestApi(this, 'apigw');
       
    //API gateway lambda integration
    const apigwbeIntegration = new apigateway.LambdaIntegration(mainfn);
    apigw.root.addMethod('GET', apigwbeIntegration);


    const hiclasorigin = new s3.Bucket(this, 'hiclasOrigin', {
      objectOwnership: s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      versioned: true,
    });

    const cfmainfn = new cloudfront.experimental.EdgeFunction(this, 'cfmainfn', {
      runtime: lambda.Runtime.PYTHON_3_8,
      handler: 'main.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../src')),
    });

    cfmainfn.addToRolePolicy(new iam.PolicyStatement({
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

    const hiclasDist = new cloudfront.Distribution(this, 'hiclasDist', {
      defaultBehavior: { 
        origin: new origins.S3Origin(hiclasorigin),
        edgeLambdas: [
          {
          functionVersion: cfmainfn.currentVersion,
          eventType: cloudfront.LambdaEdgeEventType.VIEWER_REQUEST,
          }
        ] 
      },
      defaultRootObject: 'index.html'
    });

    //cfmainfn.grantInvoke('cloudfront.amazonaws.com')

  //EndStack
  }}

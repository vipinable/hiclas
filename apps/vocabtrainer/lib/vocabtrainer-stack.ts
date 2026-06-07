import {
  Stack, StackProps, RemovalPolicy, Duration, CfnOutput,
  aws_lambda as lambda,
  aws_s3 as s3,
  aws_s3_deployment as s3deploy,
} from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as path from 'path';

/**
 * VocabTrainer app stack.
 *
 *   Browser ──▶ Lambda Function URL ──▶ Lambda (handler.py)
 *                                          │
 *                                          ▼
 *                                  S3 vocab bucket (word,meaning CSVs)
 *
 * Browser loads index.html directly from the static-site bucket via its
 * website URL (kept simple — no CloudFront). The Lambda Function URL serves
 * both the game API and is configured with permissive CORS so the static
 * page can call it from any origin.
 *
 * Vocab CSVs live in the vocab bucket at keys like `default.csv` with rows:
 *     word,meaning
 *     ephemeral,lasting for a very short time
 *     ubiquitous,present everywhere
 *
 * A seed CSV from ../seed is uploaded on deploy so the game works out of the
 * box. Drop additional CSVs into the bucket to add more word lists.
 */
export class VocabTrainerStack extends Stack {
  constructor(scope: Construct, id: string, props?: StackProps) {
    super(scope, id, props);

    const lambdaDir = path.join(__dirname, '../lambda');

    // ── S3 bucket for vocabulary CSV files ────────────────────────────────────
    const vocabBucket = new s3.Bucket(this, 'VocabBucket', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    new s3deploy.BucketDeployment(this, 'DeploySeedVocab', {
      sources: [s3deploy.Source.asset(path.join(__dirname, '../seed'))],
      destinationBucket: vocabBucket,
    });

    // ── Game API Lambda ───────────────────────────────────────────────────────
    const apiFn = new lambda.Function(this, 'ApiFn', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset(lambdaDir),
      timeout: Duration.seconds(15),
      memorySize: 256,
      environment: {
        VOCAB_BUCKET: vocabBucket.bucketName,
        DEFAULT_VOCAB_KEY: 'default.csv',
      },
    });

    vocabBucket.grantRead(apiFn);

    const apiUrl = apiFn.addFunctionUrl({
      authType: lambda.FunctionUrlAuthType.NONE,
      cors: {
        allowedOrigins: ['*'],
        allowedMethods: [lambda.HttpMethod.GET, lambda.HttpMethod.POST],
        allowedHeaders: ['*'],
      },
    });

    // ── Static site bucket (serves index.html) ────────────────────────────────
    const siteBucket = new s3.Bucket(this, 'SiteBucket', {
      websiteIndexDocument: 'index.html',
      publicReadAccess: true,
      blockPublicAccess: new s3.BlockPublicAccess({
        blockPublicAcls: false,
        blockPublicPolicy: false,
        ignorePublicAcls: false,
        restrictPublicBuckets: false,
      }),
      removalPolicy: RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    new s3deploy.BucketDeployment(this, 'DeploySite', {
      sources: [s3deploy.Source.asset(path.join(__dirname, '../web'))],
      destinationBucket: siteBucket,
    });

    // ── Outputs ───────────────────────────────────────────────────────────────
    new CfnOutput(this, 'SiteUrl', {
      value: siteBucket.bucketWebsiteUrl,
      description: 'Open this URL to play VocabTrainer',
    });
    new CfnOutput(this, 'ApiUrl', {
      value: apiUrl.url,
      description: 'Lambda Function URL — paste into the page if it asks',
    });
    new CfnOutput(this, 'VocabBucketName', {
      value: vocabBucket.bucketName,
      description: 'Upload additional word lists (word,meaning CSV) here',
    });
  }
}

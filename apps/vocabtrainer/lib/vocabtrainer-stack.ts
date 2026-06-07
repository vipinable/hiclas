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
 *                                          │ serves /  -> index.html
 *                                          │ serves /lists, /round, /answer
 *                                          ▼
 *                                  S3 vocab bucket (word,meaning CSVs)
 *
 * The Lambda is the whole app: it ships `index.html` in its bundle and
 * serves it on `GET /`, and exposes the game API on the other routes.
 * S3 is used only to store CSV word lists, loaded on demand.
 *
 * Vocab CSVs live in the vocab bucket at keys like `default.csv` with rows:
 *     word,meaning
 *     ephemeral,lasting for a very short time
 *     ubiquitous,present everywhere
 *
 * A seed CSV from ../seed is uploaded on deploy so the game works out of
 * the box. Drop additional CSVs into the bucket to add more word lists.
 */
export class VocabTrainerStack extends Stack {
  constructor(scope: Construct, id: string, props?: StackProps) {
    super(scope, id, props);

    const lambdaDir = path.join(__dirname, '../lambda');

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

    new CfnOutput(this, 'AppUrl', {
      value: apiUrl.url,
      description: 'Open this URL to play VocabTrainer',
    });
    new CfnOutput(this, 'VocabBucketName', {
      value: vocabBucket.bucketName,
      description: 'Upload additional word lists (word,meaning CSV) here',
    });
  }
}

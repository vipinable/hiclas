#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { VocabTrainerStack } from '../lib/vocabtrainer-stack';

const app = new cdk.App();

const appName = process.env.APP_NAME || 'vocabtrainer';
const envName = process.env.ENV_NAME || 'dev';

new VocabTrainerStack(app, `${appName}-${envName}`, {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION,
  },
  description: 'VocabTrainer - vocabulary word-match learning game',
});

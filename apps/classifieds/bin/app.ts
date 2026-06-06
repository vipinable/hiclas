#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { ClassifiedsStack } from '../lib/classifieds-stack';

const app = new cdk.App();

const appName = process.env.APP_NAME || 'classifieds';
const envName = process.env.ENV_NAME || 'dev';

new ClassifiedsStack(app, `${appName}-${envName}`, {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION,
  },
  description: 'Fresh minimal classifieds app',
});

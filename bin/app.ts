#!/usr/bin/env node
import * as cdk from "aws-cdk-lib";
import { QuickFigmaConnectorStack } from "../lib/figma-mcp-proxy-stack";

const app = new cdk.App();

new QuickFigmaConnectorStack(app, "QuickFigmaConnectorStack", {
  // Space-separated. Add scopes here only after they are approved on your
  // Figma app version, otherwise the consent screen rejects the request.
  //   current_user:read     -> figma_get_me
  //   (file read scope)     -> figma_get_file
  //   (comment scopes)      -> figma_get_file_comments, figma_post_comment
  figmaScopes: app.node.tryGetContext("figmaScopes") ?? "current_user:read",
  logDebug: app.node.tryGetContext("logDebug") === "true",
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION,
  },
});

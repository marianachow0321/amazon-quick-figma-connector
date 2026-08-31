import * as cdk from "aws-cdk-lib";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as apigateway from "aws-cdk-lib/aws-apigateway";
import * as logs from "aws-cdk-lib/aws-logs";
import { Construct } from "constructs";
import * as path from "path";

export interface QuickFigmaConnectorStackProps extends cdk.StackProps {
  /**
   * API Gateway stage name
   * @default "prod"
   */
  stageName?: string;

  /**
   * Space-separated Figma OAuth scopes to request.
   * Only scopes approved on your Figma app version will be granted.
   * @default "current_user:read"
   */
  figmaScopes?: string;

  /**
   * Emit request-level debug logging. Never logs token values.
   * @default false
   */
  logDebug?: boolean;
}

export class QuickFigmaConnectorStack extends cdk.Stack {
  /** The proxy URL to use as the MCP server endpoint in Amazon Quick */
  public readonly proxyUrl: string;
  public readonly lambdaFunction: lambda.Function;
  public readonly api: apigateway.RestApi;

  constructor(
    scope: Construct,
    id: string,
    props?: QuickFigmaConnectorStackProps
  ) {
    super(scope, id, props);

    cdk.Tags.of(this).add("workload", "quick-connector");

    const stageName = props?.stageName ?? "prod";
    const figmaScopes = props?.figmaScopes ?? "current_user:read";

    const logGroup = new logs.LogGroup(this, "ProxyFunctionLogGroup", {
      logGroupName: "/aws/lambda/figma-mcp-proxy",
      retention: logs.RetentionDays.ONE_WEEK,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    this.lambdaFunction = new lambda.Function(this, "ProxyFunction", {
      functionName: "figma-mcp-proxy",
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: "handler.lambda_handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "../lambda"), {
        // Keeps local test runs (__pycache__) out of the deployed bundle.
        exclude: ["__pycache__", "*.pyc"],
      }),
      timeout: cdk.Duration.seconds(60),
      memorySize: 256,
      logGroup: logGroup,
      description: "Figma MCP proxy for Amazon Quick Suite",
      environment: {
        FIGMA_SCOPES: figmaScopes,
        LOG_DEBUG: String(props?.logDebug ?? false),
      },
    });

    this.api = new apigateway.RestApi(this, "ProxyApi", {
      restApiName: "figma-mcp-proxy-api",
      description: "API Gateway for the Figma MCP proxy",
      endpointConfiguration: {
        types: [apigateway.EndpointType.REGIONAL],
      },
      deployOptions: {
        stageName: stageName,
        // The proxy holds no credentials -- callers supply their own Figma
        // token -- but it is internet-reachable, so throttle it.
        throttlingRateLimit: 100,
        throttlingBurstLimit: 200,
      },
    });

    // Built manually to avoid a circular dependency with the Lambda env var.
    const proxyUrlValue = `https://${this.api.restApiId}.execute-api.${this.region}.amazonaws.com/${stageName}`;
    this.lambdaFunction.addEnvironment("PROXY_URL", proxyUrlValue);

    const lambdaIntegration = new apigateway.LambdaIntegration(
      this.lambdaFunction,
      { proxy: true }
    );

    this.api.root.addMethod("GET", lambdaIntegration);
    this.api.root.addMethod("POST", lambdaIntegration);

    const wellKnown = this.api.root.addResource(".well-known");
    wellKnown
      .addResource("oauth-protected-resource")
      .addMethod("GET", lambdaIntegration);
    wellKnown
      .addResource("oauth-authorization-server")
      .addMethod("GET", lambdaIntegration);

    const oauth = this.api.root.addResource("oauth");
    oauth.addResource("authorize").addMethod("GET", lambdaIntegration);
    oauth.addResource("token").addMethod("POST", lambdaIntegration);

    this.api.root.addProxy({
      defaultIntegration: lambdaIntegration,
      anyMethod: true,
    });

    this.proxyUrl = proxyUrlValue;

    new cdk.CfnOutput(this, "AmazonQuickSettings", {
      value: [
        `MCP server endpoint: ${this.proxyUrl}`,
        `Authorization URL:   ${this.proxyUrl}/oauth/authorize`,
        `Token URL:           ${this.proxyUrl}/oauth/token`,
        `Client ID:           <your Figma app client ID>`,
        `Client secret:       <your Figma app client secret>`,
        `Redirect URL:        https://${this.region}.quicksight.aws.amazon.com/sn/oauthcallback`,
      ].join("\n"),
      description: "Settings for the Amazon Quick MCP connector",
    });

    new cdk.CfnOutput(this, "FigmaRedirectUri", {
      value: `https://${this.region}.quicksight.aws.amazon.com/sn/oauthcallback`,
      description:
        "Register this as a redirect URI on your Figma app at figma.com/developers/apps",
    });

    new cdk.CfnOutput(this, "RequestedFigmaScopes", {
      value: figmaScopes,
      description:
        "Scopes injected at /oauth/authorize. Must be approved on your Figma app version.",
    });
  }
}

"""Hosted AI operations agent on Amazon Bedrock AgentCore.

  Controller ──InvokeAgentRuntime──► AgentCore Runtime (Strands agent, Claude on Bedrock)
                                         │  MCP over HTTPS, SigV4-signed (AWS_IAM authorizer)
                                         ▼
                                     AgentCore Gateway ──► tools Lambda ──► graph API Lambda ──► Neptune
                                         (6 read-only tools)
  Conversation history: AgentCore Memory (short-term, keyed by runtime session).

AgentCore resources are declared with their CloudFormation types and property names
directly (CfnResource), so this works whichever aws-cdk-lib version is installed.
"""

from __future__ import annotations

from pathlib import Path

from aws_cdk import (
    Aws, CfnOutput, CfnResource, Duration, RemovalPolicy, Stack, Token,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_s3_assets as s3_assets,
)
from constructs import Construct

from agent.tool_schemas import cfn_inline_payload
from infra.neptune_stack import NeptuneAirlineStack

ROOT = Path(__file__).resolve().parent.parent
RUNTIME_NAME = "airline_ops_agent"          # [a-zA-Z][a-zA-Z0-9_]{0,47}
GATEWAY_NAME = "airline-disruption-tools"    # ([0-9a-zA-Z][-]?){1,48}
GATEWAY_TARGET = "graph"                     # tools appear to the agent as graph___<tool>
MEMORY_NAME = "airline_ops_memory"


class AgentCoreStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, core: NeptuneAirlineStack,
                 runtime_zip: Path, model_id: str | None = None, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.add_dependency(core)
        acct, region, part = Aws.ACCOUNT_ID, Aws.REGION, Aws.PARTITION

        # ------------------------------------------------------------ gateway tools Lambda
        tools_fn = lambda_.Function(
            self, "GatewayTools",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="gateway_lambda.handler",
            code=lambda_.Code.from_asset(
                str(ROOT / "agent"),
                exclude=["chat.py", "tools.py", "runtime_app.py", "requirements-runtime.txt",
                         "__pycache__", "*.pyc"]),
            timeout=Duration.minutes(5),
            memory_size=256,
            environment={"GRAPH_API_FUNCTION": core.graph_api_fn.function_name},
            log_group=logs.LogGroup(self, "GatewayToolsLogs",
                                    retention=logs.RetentionDays.ONE_WEEK,
                                    removal_policy=RemovalPolicy.DESTROY),
            description="AgentCore Gateway target: read-only airline graph tools",
        )
        core.graph_api_fn.grant_invoke(tools_fn)

        # ------------------------------------------------------------ gateway
        gateway_role = iam.Role(
            self, "GatewayRole",
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={"StringEquals": {"aws:SourceAccount": acct}}),
            description="AgentCore Gateway: invoke the airline graph tools Lambda")
        tools_fn.grant_invoke(gateway_role)

        gateway = CfnResource(self, "Gateway", type="AWS::BedrockAgentCore::Gateway",
                              properties={
                                  "Name": GATEWAY_NAME,
                                  "Description": "Read-only airline disruption graph tools",
                                  "AuthorizerType": "AWS_IAM",
                                  "ProtocolType": "MCP",
                                  "RoleArn": gateway_role.role_arn,
                              })
        gateway.node.add_dependency(gateway_role)

        target = CfnResource(self, "GraphToolsTarget", type="AWS::BedrockAgentCore::GatewayTarget",
                             properties={
                                 "GatewayIdentifier": gateway.get_att("GatewayIdentifier"),
                                 "Name": GATEWAY_TARGET,
                                 "Description": "Neptune airline graph via the graph API Lambda",
                                 "TargetConfiguration": {"Mcp": {"Lambda": {
                                     "LambdaArn": tools_fn.function_arn,
                                     "ToolSchema": {"InlinePayload": cfn_inline_payload()},
                                 }}},
                                 "CredentialProviderConfigurations": [
                                     {"CredentialProviderType": "GATEWAY_IAM_ROLE"}],
                             })
        target.node.add_dependency(gateway_role)

        # ------------------------------------------------------------ memory
        memory = CfnResource(self, "Memory", type="AWS::BedrockAgentCore::Memory",
                             properties={"Name": MEMORY_NAME, "EventExpiryDuration": 30})

        # ------------------------------------------------------------ runtime
        code = s3_assets.Asset(self, "RuntimeCode", path=str(runtime_zip))

        runtime_role = iam.Role(
            self, "RuntimeRole",
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": acct},
                    "ArnLike": {"aws:SourceArn": f"arn:{part}:bedrock-agentcore:{region}:{acct}:*"},
                }),
            description="AgentCore Runtime: airline operations agent")
        code.grant_read(runtime_role)
        log_prefix = f"arn:{part}:logs:{region}:{acct}:log-group:/aws/bedrock-agentcore/runtimes"
        statements = [
            iam.PolicyStatement(actions=["logs:DescribeLogStreams", "logs:CreateLogGroup"],
                                resources=[f"{log_prefix}/*"]),
            iam.PolicyStatement(actions=["logs:DescribeLogGroups"],
                                resources=[f"arn:{part}:logs:{region}:{acct}:log-group:*"]),
            iam.PolicyStatement(actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                                resources=[f"{log_prefix}/*:log-stream:*"]),
            iam.PolicyStatement(actions=["xray:PutTraceSegments", "xray:PutTelemetryRecords",
                                         "xray:GetSamplingRules", "xray:GetSamplingTargets"],
                                resources=["*"]),
            iam.PolicyStatement(actions=["cloudwatch:PutMetricData"], resources=["*"],
                                conditions={"StringEquals": {
                                    "cloudwatch:namespace": "bedrock-agentcore"}}),
            iam.PolicyStatement(
                actions=["bedrock-agentcore:GetWorkloadAccessToken",
                         "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
                         "bedrock-agentcore:GetWorkloadAccessTokenForUserId"],
                resources=[
                    f"arn:{part}:bedrock-agentcore:{region}:{acct}:workload-identity-directory/default",
                    f"arn:{part}:bedrock-agentcore:{region}:{acct}:workload-identity-directory/default/workload-identity/*"]),
            # Claude on Bedrock, directly or through (cross-region/global) inference profiles.
            iam.PolicyStatement(actions=["bedrock:InvokeModel",
                                         "bedrock:InvokeModelWithResponseStream"],
                                resources=[f"arn:{part}:bedrock:*::foundation-model/*",
                                           f"arn:{part}:bedrock:*:{acct}:inference-profile/*"]),
            iam.PolicyStatement(actions=["bedrock-agentcore:InvokeGateway"],
                                resources=[Token.as_string(gateway.get_att("GatewayArn"))]),
            iam.PolicyStatement(
                actions=["bedrock-agentcore:CreateEvent", "bedrock-agentcore:GetEvent",
                         "bedrock-agentcore:ListEvents", "bedrock-agentcore:DeleteEvent",
                         "bedrock-agentcore:ListSessions", "bedrock-agentcore:ListActors",
                         "bedrock-agentcore:GetMemory", "bedrock-agentcore:RetrieveMemoryRecords",
                         "bedrock-agentcore:ListMemoryRecords", "bedrock-agentcore:GetMemoryRecord"],
                resources=[Token.as_string(memory.get_att("MemoryArn"))]),
        ]
        for statement in statements:
            runtime_role.add_to_policy(statement)

        env = {
            "GATEWAY_URL": Token.as_string(gateway.get_att("GatewayUrl")),
            "MEMORY_ID": Token.as_string(memory.get_att("MemoryId")),
            "AGENT_REGION": region,
        }
        if model_id:
            env["AGENT_MODEL_ID"] = model_id
        runtime = CfnResource(self, "Runtime", type="AWS::BedrockAgentCore::Runtime",
                              properties={
                                  "AgentRuntimeName": RUNTIME_NAME,
                                  "Description": "Airline disruption operations agent",
                                  "RoleArn": runtime_role.role_arn,
                                  "AgentRuntimeArtifact": {"CodeConfiguration": {
                                      "Code": {"S3": {"Bucket": code.s3_bucket_name,
                                                      "Prefix": code.s3_object_key}},
                                      "Runtime": "PYTHON_3_13",
                                      "EntryPoint": ["main.py"],
                                  }},
                                  "NetworkConfiguration": {"NetworkMode": "PUBLIC"},
                                  "ProtocolConfiguration": "HTTP",
                                  "EnvironmentVariables": env,
                              })
        runtime.node.add_dependency(runtime_role)
        runtime.node.add_dependency(target)

        CfnOutput(self, "AgentRuntimeArn", value=Token.as_string(runtime.get_att("AgentRuntimeArn")))
        CfnOutput(self, "GatewayUrl", value=env["GATEWAY_URL"])
        CfnOutput(self, "MemoryId", value=env["MEMORY_ID"])
        CfnOutput(self, "GatewayToolsFunction", value=tools_fn.function_name)

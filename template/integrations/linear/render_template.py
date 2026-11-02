"""Render a standalone webhook/queue stack; no AWS calls or existing-stack edits."""
import json
from pathlib import Path


def template():
    ref = lambda name: {"Ref": name}
    sub = lambda value: {"Fn::Sub": value}
    att = lambda name, attr: {"Fn::GetAtt": [name, attr]}
    allow = lambda actions, resources: {"Effect": "Allow", "Action": actions, "Resource": resources}
    resources = {}

    def resource(name, kind, properties, **extra):
        resources[name] = {"Type": kind, "Properties": properties, **extra}

    resource("DeadLetterQueue", "AWS::SQS::Queue", {
        "QueueName": sub("${ClawName}-linear-dlq.fifo"),
        "FifoQueue": True, "SqsManagedSseEnabled": True, "MessageRetentionPeriod": 1209600})
    resource("Queue", "AWS::SQS::Queue", {
        "QueueName": sub("${ClawName}-linear.fifo"),
        "FifoQueue": True, "SqsManagedSseEnabled": True, "VisibilityTimeout": 60,
        "MessageRetentionPeriod": 1209600, "ReceiveMessageWaitTimeSeconds": 20,
        "RedrivePolicy": {"deadLetterTargetArn": att("DeadLetterQueue", "Arn"), "maxReceiveCount": 8}})
    resource("ConsumerPolicy", "AWS::IAM::Policy", {
        "PolicyName": sub("${AWS::StackName}-consumer"), "Roles": [sub("${ClawName}-task")],
        "PolicyDocument": {"Version": "2012-10-17", "Statement": [allow(
            ["sqs:ReceiveMessage", "sqs:DeleteMessage"], att("Queue", "Arn"))]}})
    resource("LogGroup", "AWS::Logs::LogGroup", {"LogGroupName": sub("/ecs/${ClawName}/linear-webhook"), "RetentionInDays": 14})
    resource("ReceiverRole", "AWS::IAM::Role", {
        "RoleName": sub("${ClawName}-ln"),
        "AssumeRolePolicyDocument": {"Version": "2012-10-17", "Statement": [{
            "Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]},
        "Policies": [{"PolicyName": "signed-linear-events", "PolicyDocument": {
            "Version": "2012-10-17", "Statement": [
                allow("sqs:SendMessage", att("Queue", "Arn")),
                allow("ssm:GetParameter", sub("arn:${AWS::Partition}:ssm:${AWS::Region}:${AWS::AccountId}:parameter${SigningSecretParameter}")),
                {"Fn::If": ["HasSigningKey", {
                    **allow("kms:Decrypt", ref("SigningSecretKeyArn")),
                    "Condition": {"StringEquals": {
                        "kms:ViaService": sub("ssm.${AWS::Region}.${AWS::URLSuffix}")}}}, ref("AWS::NoValue")]},
                allow(["logs:CreateLogStream", "logs:PutLogEvents"], att("LogGroup", "Arn"))]}}]})
    resource("Receiver", "AWS::Lambda::Function", {
        "FunctionName": sub("${ClawName}-ln"),
        "Runtime": "python3.13", "Architectures": ["arm64"], "Handler": "index.handler",
        "Timeout": 5, "MemorySize": 512, "Role": att("ReceiverRole", "Arn"),
        "LoggingConfig": {"LogGroup": ref("LogGroup")},
        "Environment": {"Variables": {
            "QUEUE_URL": ref("Queue"), "ORGANIZATION_ID": ref("OrganizationId"),
            "APP_USER_ID": ref("AppUserId"), "SIGNING_SECRET_PARAMETER": ref("SigningSecretParameter")}},
        "Code": {"ZipFile": Path(__file__).with_name("webhook.py").read_text()}})
    resource("WebhookUrl", "AWS::Lambda::Url", {
        "TargetFunctionArn": ref("Receiver"), "AuthType": "NONE"})
    resource("UrlPermission", "AWS::Lambda::Permission", {
        "FunctionName": ref("Receiver"), "Action": "lambda:InvokeFunctionUrl",
        "Principal": "*", "FunctionUrlAuthType": "NONE"})
    resource("InvokePermission", "AWS::Lambda::Permission", {
        "FunctionName": ref("Receiver"), "Action": "lambda:InvokeFunction",
        "Principal": "*", "InvokedViaFunctionUrl": True})
    uuid = {"Type": "String", "AllowedPattern": "[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"}
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "Optional Linear agent webhook and outbound queue for a Hermes gateway",
        "Parameters": {
            "ClawName": {"Type": "String", "Default": "dispatch", "MinLength": 1, "MaxLength": 59,
                         "AllowedPattern": "^[a-zA-Z]([a-zA-Z0-9-]*[a-zA-Z0-9])?$",
                         "Description": "Name of the existing generated gateway stack"},
            "OrganizationId": dict(uuid), "AppUserId": dict(uuid),
            "SigningSecretParameter": {"Type": "String", "Default": "/dispatch/LINEAR_WEBHOOK_SIGNING_SECRET", "AllowedPattern": "/[a-zA-Z0-9_./-]+",
                                       "Description": "Existing SSM SecureString name, including leading slash"},
            "SigningSecretKeyArn": {"Type": "String", "Default": "",
                                    "Description": "Customer-managed KMS key ARN; empty for aws/ssm"}},
        "Conditions": {"HasSigningKey": {"Fn::Not": [{"Fn::Equals": [ref("SigningSecretKeyArn"), ""]}]}},
        "Resources": resources,
        "Outputs": {
            "QueueUrl": {"Value": ref("Queue")}, "QueueArn": {"Value": att("Queue", "Arn")},
            "DeadLetterQueueUrl": {"Value": ref("DeadLetterQueue")},
            "WebhookUrl": {"Value": att("WebhookUrl", "FunctionUrl")}}}


if __name__ == "__main__":
    print(json.dumps(template(), indent=2))

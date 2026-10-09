"""Static security checks of the CloudFormation templates (phase 16 review items).

These read the templates only; what AWS actually deployed is checked with the
commands in docs/SECURITY_REVIEW.md ("verify on laptop").
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from tests.test_infra import load, render_user_data

NETWORK = load("network.yaml")["Resources"]
DATA = load("data.yaml")["Resources"]
APP = load("app.yaml")["Resources"]
ALL = {"network": NETWORK, "data": DATA, "app": APP}


def ALL_RESOURCES() -> list[tuple[str, dict[str, Any]]]:
    return [(name, res) for resources in ALL.values() for name, res in resources.items()]


def security_group_rules() -> list[tuple[str, dict[str, Any]]]:
    """Every ingress rule in every template: inline ones and stand-alone SecurityGroupIngress."""
    rules = []
    for stack, resources in ALL.items():
        for name, res in resources.items():
            if res["Type"] == "AWS::EC2::SecurityGroup":
                for rule in res["Properties"].get("SecurityGroupIngress", []):
                    rules.append((f"{stack}.{name}", rule))
            elif res["Type"] == "AWS::EC2::SecurityGroupIngress":
                rules.append((f"{stack}.{name}", res["Properties"]))
    return rules


def test_no_security_group_opens_port_22_or_the_world() -> None:
    rules = security_group_rules()
    assert rules
    for where, rule in rules:
        low, high = int(rule["FromPort"]), int(rule["ToPort"])
        assert not (low <= 22 <= high), f"{where} allows port 22"
        assert "CidrIp" not in rule and "CidrIpv6" not in rule, f"{where} uses a CIDR source"


def test_alb_accepts_only_the_cloudfront_prefix_list_on_80() -> None:
    (rule,) = APP["AlbSg"]["Properties"]["SecurityGroupIngress"]
    assert (rule["IpProtocol"], rule["FromPort"], rule["ToPort"]) == ("tcp", 80, 80)
    assert rule["SourcePrefixListId"] == {"Ref": "CloudFrontPrefixListId"} and len(rule) == 5


def test_app_accepts_only_8000_from_the_alb_and_nothing_else_is_opened() -> None:
    (rule,) = APP["AppSg"]["Properties"]["SecurityGroupIngress"]
    assert (rule["FromPort"], rule["ToPort"], rule["SourceSecurityGroupId"]) == (8000, 8000, {"Ref": "AlbSg"})
    # No file system, so no NFS port and no stand-alone ingress rule on a security group owned by another stack.
    assert [n for n, r in ALL_RESOURCES() if r["Type"] == "AWS::EC2::SecurityGroupIngress"] == []
    assert all("2049" not in str(r) for _, r in ALL_RESOURCES() if r["Type"] == "AWS::EC2::SecurityGroup")
    assert [n for n, r in ALL_RESOURCES() if r["Type"] == "AWS::EC2::SecurityGroup"] == ["AlbSg", "AppSg"]


def test_instances_are_private_and_require_imdsv2() -> None:
    data = APP["LaunchTemplate"]["Properties"]["LaunchTemplateData"]
    assert data["MetadataOptions"] == {
        "HttpEndpoint": "enabled", "HttpTokens": "required", "HttpPutResponseHopLimit": 2,
    }
    (nic,) = data["NetworkInterfaces"]
    assert nic["AssociatePublicIpAddress"] is False and nic["Groups"] == [{"Ref": "AppSg"}]
    subnets = APP["AppAsg"]["Properties"]["VPCZoneIdentifier"]
    assert all("Private" in s["Fn::ImportValue"] for s in subnets)
    assert APP["AppAsg"]["Properties"]["MaxSize"] in (1, "1")
    assert "KeyName" not in data  # no SSH key pair


def test_storage_is_encrypted_and_the_bucket_is_private() -> None:
    # The database and the snapshots: the encrypted root volume and the encrypted, versioned S3 bucket.
    ebs = APP["LaunchTemplate"]["Properties"]["LaunchTemplateData"]["BlockDeviceMappings"][0]["Ebs"]
    assert ebs["Encrypted"] is True and ebs["VolumeType"] == "gp2" and ebs["VolumeSize"] <= 35
    assert ebs["DeleteOnTermination"] is True
    bucket = DATA["Bucket"]["Properties"]
    assert bucket["VersioningConfiguration"]["Status"] == "Enabled"
    sse = bucket["BucketEncryption"]["ServerSideEncryptionConfiguration"][0]["ServerSideEncryptionByDefault"]
    assert sse["SSEAlgorithm"] in ("AES256", "aws:kms")
    assert bucket["PublicAccessBlockConfiguration"] == {
        "BlockPublicAcls": True, "BlockPublicPolicy": True, "IgnorePublicAcls": True, "RestrictPublicBuckets": True,
    }
    # Nothing else in the data stack holds state: no file system, no registry.
    assert [n for n, r in DATA.items() if r["Type"] != "AWS::S3::Bucket"] == []
    assert DATA["Bucket"]["DeletionPolicy"] == "Retain"


def test_viewers_always_use_https() -> None:
    config = APP["Distribution"]["Properties"]["DistributionConfig"]
    behaviors = [config["DefaultCacheBehavior"], *config["CacheBehaviors"]]
    assert {b["ViewerProtocolPolicy"] for b in behaviors} == {"redirect-to-https"}


def test_user_data_and_templates_hold_no_secret() -> None:
    rendered = render_user_data(APP)
    for needle in ("KB_WRITE_TOKENS", "KB_TOKEN=", "put-parameter", "Bearer", "SecureString", "AKIA", "aws_secret"):
        assert needle not in rendered, needle
    for stack, resources in ALL.items():
        text = repr(resources)
        assert not re.search(r"AKIA[0-9A-Z]{16}", text), stack
        assert "WriteTokens" not in text and "NoEcho" not in text
    # The token reaches the app through the instance role (from the private bucket, deviation 8),
    # never through the template: the user data names the source, not the value.
    assert "KB_TOKENS_PARAM" not in rendered
    assert "-e KB_TOKENS_SOURCE=s3" in rendered and "KB_TOKENS_KEY" not in rendered


def test_app_stack_needs_no_ssm_or_cloudwatch_logs_permissions() -> None:
    """Deviation 8: the sandbox instance role allows S3 and CloudWatch metrics, not SSM or Logs."""
    rendered = render_user_data(APP)
    assert "ssm" not in rendered.lower() and "awslogs" not in rendered
    assert not [n for n, r in APP.items() if r["Type"].startswith(("AWS::Logs::", "AWS::SSM::", "AWS::IAM::"))]

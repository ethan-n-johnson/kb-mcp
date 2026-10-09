"""Static checks of the CloudFormation templates and shell scripts (phases 10, 12, 15 and the sandbox change).

Nothing here talks to AWS. The templates are parsed, the user data is rendered
the way `Fn::Sub` would render it and syntax checked with `bash -n`, the alarms
are compared with what `kb/metrics.py` emits, and the shell scripts are run
against stub `aws`, `docker` and `curl` programs.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from kb import metrics
from kb.logging import setup_logging

ROOT = Path(__file__).resolve().parent.parent
INFRA = ROOT / "infra"
SCRIPT = INFRA / "scripts" / "restore-on-instance.sh"
DEPLOY = INFRA / "deploy.sh"
PUSH = INFRA / "push-image.sh"
TEARDOWN = INFRA / "teardown-app.sh"


# --- template loading ---------------------------------------------------------------------


class CfnLoader(yaml.SafeLoader):
    """SafeLoader that turns short-form intrinsics (`!Ref x`) into their long form."""


def _intrinsic(loader: CfnLoader, suffix: str, node: yaml.Node) -> Any:
    if isinstance(node, yaml.ScalarNode):
        value: Any = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)  # type: ignore[arg-type]
    if suffix == "Ref":
        return {"Ref": value}
    if suffix == "GetAtt" and isinstance(value, str):
        value = value.split(".", 1)
    return {f"Fn::{suffix}": value}


CfnLoader.add_multi_constructor("!", _intrinsic)


def load(name: str) -> dict[str, Any]:
    return yaml.load((INFRA / name).read_text(encoding="utf-8"), Loader=CfnLoader)  # noqa: S506


@pytest.fixture(scope="module")
def app() -> dict[str, Any]:
    return load("app.yaml")


@pytest.fixture(scope="module")
def resources(app: dict[str, Any]) -> dict[str, Any]:
    return app["Resources"]


def of_type(resources: dict[str, Any], rtype: str) -> dict[str, Any]:
    return {k: v for k, v in resources.items() if v["Type"] == rtype}


# --- user data ----------------------------------------------------------------------------

PSEUDO = {"AWS::Region": "us-east-1", "AWS::StackName": "kb-app"}
PARAMETER_VALUES = {"ImageTag": "abc1234", "MetricsMode": "emf", "ExtensionOrigin": ""}


def user_data_parts(resources: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    data = resources["LaunchTemplate"]["Properties"]["LaunchTemplateData"]["UserData"]
    template, variables = data["Fn::Base64"]["Fn::Sub"]
    return template, variables


def sub_variable_names(template: str) -> set[str]:
    """Names of the `${Name}` references that Fn::Sub would substitute."""
    return set(re.findall(r"\$\{(?!!)([^}]+)\}", template))


def render_user_data(resources: dict[str, Any]) -> str:
    """Render user data like Fn::Sub: `${Name}` is replaced, `${!X}` becomes `${X}`."""
    template, variables = user_data_parts(resources)
    values = {name: f"VALUE_{name.upper()}" for name in variables} | PSEUDO | PARAMETER_VALUES

    def sub(match: re.Match[str]) -> str:
        return values[match.group(1)]

    out = re.sub(r"\$\{(?!!)([^}]+)\}", sub, template)
    return out.replace("${!", "${")


def test_every_sub_variable_in_user_data_is_defined(resources: dict[str, Any], app: dict[str, Any]) -> None:
    template, variables = user_data_parts(resources)
    known = set(variables) | set(PSEUDO) | set(app["Parameters"])
    assert sub_variable_names(template) <= known
    # Everything in the map is used (an unused entry hides a typo).
    assert set(variables) <= sub_variable_names(template)


def test_literal_shell_braces_are_escaped(resources: dict[str, Any]) -> None:
    """Every `${...}` that is not a Sub variable carries the `!` escape, and renders as shell."""
    template, variables = user_data_parts(resources)
    known = set(variables) | set(PSEUDO) | set(PARAMETER_VALUES)
    for name in sub_variable_names(template):
        assert name in known, f"${{{name}}} would be read by Fn::Sub; escape it as ${{!{name}}}"
    escaped = set(re.findall(r"\$\{!([^}]+)\}", template))
    # The shell expansions this script relies on.
    assert {"id_args[@]", "LOG_OPTS[@]", "aws:AutoScalingGroupName", "aws:InstanceId"} <= escaped
    rendered = render_user_data(resources)
    assert "${!" not in rendered and 'cfn-signal -e "$STATUS" "${id_args[@]}"' in rendered


def test_rendered_user_data_passes_bash_syntax_check(resources: dict[str, Any], tmp_path: Path) -> None:
    script = tmp_path / "user-data.sh"
    script.write_text(render_user_data(resources), encoding="utf-8")
    result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def _shellcheck() -> str | None:
    found = shutil.which("shellcheck")
    if found:
        return found
    scratch = os.environ.get("KB_SHELLCHECK")
    return scratch if scratch and Path(scratch).is_file() else None


@pytest.mark.skipif(_shellcheck() is None, reason="shellcheck is not installed")
def test_rendered_user_data_and_scripts_pass_shellcheck(resources: dict[str, Any], tmp_path: Path) -> None:
    script = tmp_path / "user-data.sh"
    script.write_text(render_user_data(resources), encoding="utf-8")
    cmd = [_shellcheck() or "", "-s", "bash", str(script), str(SCRIPT), str(DEPLOY), str(PUSH), str(TEARDOWN)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout


def test_shell_scripts_are_executable_and_have_no_trailing_junk() -> None:
    for path in (SCRIPT, DEPLOY, PUSH, TEARDOWN):
        assert path.stat().st_mode & stat.S_IXUSR, f"{path.name} is not executable"
        assert path.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash\n")


def test_container_logs_go_to_cloudwatch_with_awslogs(resources: dict[str, Any]) -> None:
    rendered = render_user_data(resources)
    for flag in (
        "--log-driver awslogs",
        "--log-opt awslogs-region=us-east-1",
        "--log-opt awslogs-group=VALUE_LOGGROUPNAME",
        '--log-opt "awslogs-stream=$INSTANCE_ID"',
        "--log-opt awslogs-format=json/emf",
    ):
        assert flag in rendered, flag
    # The array is built before the container starts and is what docker run receives.
    assert rendered.index("LOG_OPTS=(") < rendered.index("docker run -d --name kb")
    assert '"${LOG_OPTS[@]}"' in rendered
    assert "-e KB_METRICS_MODE=emf" in rendered
    assert "LOG_OPTS=()" not in rendered
    template, variables = user_data_parts(resources)
    assert variables["LogGroupName"] == {"Ref": "AppLogGroup"}  # also makes the group exist first


def test_cloudwatch_agent_is_installed_and_configured(resources: dict[str, Any]) -> None:
    rendered = render_user_data(resources)
    assert re.search(r"dnf install -y .*amazon-cloudwatch-agent", rendered)
    match = re.search(r"<<'CWA_CONFIG'\n(.*?)\nCWA_CONFIG\n", rendered, re.S)
    assert match, "agent config heredoc not found"
    config = json.loads(match.group(1))
    metrics_cfg = config["metrics"]
    assert metrics_cfg["namespace"] == "CWAgent"
    assert metrics_cfg["metrics_collected"]["mem"]["measurement"] == ["mem_used_percent"]
    assert metrics_cfg["metrics_collected"]["disk"] == {
        "measurement": ["disk_used_percent"],
        "resources": ["/"],
    }
    assert metrics_cfg["append_dimensions"] == {
        "AutoScalingGroupName": "${aws:AutoScalingGroupName}",
        "InstanceId": "${aws:InstanceId}",
    }
    assert metrics_cfg["aggregation_dimensions"] == [["AutoScalingGroupName"]]
    assert "amazon-cloudwatch-agent-ctl" in rendered and "fetch-config -m ec2" in rendered
    assert re.search(r"fetch-config -m ec2 .*-s", rendered.replace("\\\n", ""))


def test_nightly_backup_cron_replaces_the_placeholder(resources: dict[str, Any]) -> None:
    rendered = render_user_data(resources)
    assert "PLACEHOLDER" not in rendered
    assert (
        "'15 7 * * * root docker exec kb python -m kb.cli backup >> /var/log/kb-backup.log 2>&1'"
        in rendered
    )
    assert "> /etc/cron.d/kb-backup" in rendered and "chmod 0644 /etc/cron.d/kb-backup" in rendered
    assert re.search(r"systemctl enable --now docker crond", rendered)
    assert re.search(r"dnf install -y .*\bcronie\b", rendered)


def test_user_data_stays_under_the_ec2_limit(resources: dict[str, Any]) -> None:
    assert len(render_user_data(resources).encode()) < 16 * 1024


# --- parameters ---------------------------------------------------------------------------


def test_new_parameters_have_no_defaults_and_validate_their_input(app: dict[str, Any]) -> None:
    params = app["Parameters"]
    prefix = params["CloudFrontPrefixListId"]
    email = params["AlertEmail"]
    assert "Default" not in prefix and "Default" not in email
    assert re.fullmatch(prefix["AllowedPattern"], "pl-3b927c52")
    assert not re.fullmatch(prefix["AllowedPattern"], "0.0.0.0/0")
    assert not re.fullmatch(prefix["AllowedPattern"], "sg-0123456789abcdef0")
    assert re.fullmatch(email["AllowedPattern"], "ethan@example.com")
    for bad in ("ethan", "ethan@example", "a@b.c,d@e.f", "a b@c.de", ""):
        assert not re.fullmatch(email["AllowedPattern"], bad), bad
    assert params["MetricsMode"]["Default"] == "emf" and params["MetricsMode"]["AllowedValues"] == ["emf", "api"]


# --- phase 10: CloudFront and the locked ALB ----------------------------------------------

CACHING_DISABLED = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
CACHING_OPTIMIZED = "658327ea-f89d-4fab-a63d-7e88639e58f6"
ALL_VIEWER_EXCEPT_HOST = "b689b0a8-53d0-40ab-baf2-68738e2966ac"
ALL_METHODS = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]


def distribution(resources: dict[str, Any]) -> dict[str, Any]:
    return resources["Distribution"]["Properties"]["DistributionConfig"]


def test_distribution_settings(resources: dict[str, Any]) -> None:
    cfg = distribution(resources)
    assert (cfg["Enabled"], cfg["Comment"], cfg["PriceClass"], cfg["HttpVersion"]) == (
        True,
        "kb-mcp",
        "PriceClass_100",
        "http2",
    )
    assert cfg["ViewerCertificate"] == {"CloudFrontDefaultCertificate": True}
    (origin,) = cfg["Origins"]
    assert origin["DomainName"] == {"Fn::GetAtt": ["Alb", "DNSName"]}
    custom = origin["CustomOriginConfig"]
    assert custom["OriginProtocolPolicy"] == "http-only"
    assert (custom["OriginReadTimeout"], custom["OriginKeepaliveTimeout"]) == (60, 5)


def test_default_and_explicit_api_and_mcp_behaviors_are_identical_and_uncached(
    resources: dict[str, Any],
) -> None:
    cfg = distribution(resources)
    default = cfg["DefaultCacheBehavior"]
    by_path = {b["PathPattern"]: b for b in cfg["CacheBehaviors"]}
    assert set(by_path) == {"/api/*", "/mcp*", "/static/*"}
    assert default["ViewerProtocolPolicy"] == "redirect-to-https"
    assert default["AllowedMethods"] == ALL_METHODS and default["CachedMethods"] == ["GET", "HEAD"]
    assert default["CachePolicyId"] == CACHING_DISABLED
    assert default["OriginRequestPolicyId"] == ALL_VIEWER_EXCEPT_HOST
    assert default["Compress"] is True
    for path in ("/api/*", "/mcp*"):
        explicit = dict(by_path[path])
        explicit.pop("PathPattern")
        assert explicit == default, path


def test_static_behavior_is_the_only_cached_path(resources: dict[str, Any]) -> None:
    cfg = distribution(resources)
    static = next(b for b in cfg["CacheBehaviors"] if b["PathPattern"] == "/static/*")
    assert static["CachePolicyId"] == CACHING_OPTIMIZED
    assert static["AllowedMethods"] == ["GET", "HEAD"]
    assert static["ViewerProtocolPolicy"] == "redirect-to-https"
    cached = [b for b in [cfg["DefaultCacheBehavior"], *cfg["CacheBehaviors"]] if b["CachePolicyId"] != CACHING_DISABLED]
    assert [b["PathPattern"] for b in cached] == ["/static/*"]


def test_managed_policy_ids_are_flagged_as_unverified() -> None:
    text = (INFRA / "app.yaml").read_text(encoding="utf-8")
    assert "NOT re-verified" in text


def test_alb_accepts_http_only_from_the_cloudfront_prefix_list(resources: dict[str, Any]) -> None:
    (rule,) = resources["AlbSg"]["Properties"]["SecurityGroupIngress"]
    assert rule == {
        "IpProtocol": "tcp",
        "FromPort": 80,
        "ToPort": 80,
        "SourcePrefixListId": {"Ref": "CloudFrontPrefixListId"},
        "Description": "HTTP from the CloudFront origin-facing prefix list",
    }
    assert "CidrIp" not in json.dumps(resources["AlbSg"])


def test_outputs(app: dict[str, Any]) -> None:
    out = app["Outputs"]
    assert out["DistributionDomainName"]["Value"] == {"Fn::GetAtt": ["Distribution", "DomainName"]}
    assert out["DistributionId"]["Value"] == {"Ref": "Distribution"}
    for kept in ("AlbDnsName", "AsgName", "TargetGroupArn", "LoadBalancerFullName", "TargetGroupFullName"):
        assert kept in out


# --- phase 15: log group, topic, alarms ---------------------------------------------------


def test_log_group_and_topic(resources: dict[str, Any]) -> None:
    group = resources["AppLogGroup"]["Properties"]
    assert (group["LogGroupName"], group["RetentionInDays"]) == ("/kb/app", 14)
    topic = resources["AlertTopic"]["Properties"]
    assert topic["Subscription"] == [{"Protocol": "email", "Endpoint": {"Ref": "AlertEmail"}}]


def alarms(resources: dict[str, Any]) -> dict[str, dict[str, Any]]:
    found = {r["Properties"]["AlarmName"]: r["Properties"] for r in of_type(resources, "AWS::CloudWatch::Alarm").values()}
    return found


def test_exactly_the_six_alarms_exist(resources: dict[str, Any]) -> None:
    assert set(alarms(resources)) == {
        "kb-keyword-mode",
        "kb-unhealthy-host",
        "kb-no-healthy-host",
        "kb-5xx",
        "kb-high-latency",
        "kb-memory",
    }


def test_every_alarm_notifies_the_topic_on_alarm_and_on_ok(resources: dict[str, Any]) -> None:
    for name, alarm in alarms(resources).items():
        assert alarm["AlarmActions"] == [{"Ref": "AlertTopic"}], name
        assert alarm["OKActions"] == [{"Ref": "AlertTopic"}], name
        assert alarm["AlarmDescription"], name


def dims(alarm: dict[str, Any]) -> dict[str, Any]:
    return {d["Name"]: d["Value"] for d in alarm.get("Dimensions", [])}


def test_alarm_thresholds_match_the_phase_file(resources: dict[str, Any]) -> None:
    a = alarms(resources)
    keys = ("Statistic", "Period", "EvaluationPeriods", "DatapointsToAlarm", "Threshold", "ComparisonOperator", "TreatMissingData")

    def shape(alarm: dict[str, Any]) -> tuple[Any, ...]:
        return tuple(alarm.get(k) for k in keys)

    gte, lt, gt = "GreaterThanOrEqualToThreshold", "LessThanThreshold", "GreaterThanThreshold"
    assert shape(a["kb-keyword-mode"]) == ("Sum", 60, 1, None, 1, gte, "notBreaching")
    assert shape(a["kb-unhealthy-host"]) == ("Maximum", 60, 2, 2, 1, gte, "notBreaching")
    assert shape(a["kb-no-healthy-host"]) == ("Minimum", 60, 3, 3, 1, lt, "breaching")
    assert shape(a["kb-5xx"]) == ("Sum", 300, 1, None, 5, gte, "notBreaching")
    assert shape(a["kb-memory"]) == ("Average", 300, 2, 2, 85, gt, "notBreaching")
    latency = a["kb-high-latency"]
    assert (latency["ExtendedStatistic"], latency["Period"], latency["EvaluationPeriods"]) == ("p90", 300, 1)
    assert (latency["Threshold"], latency["ComparisonOperator"], latency["TreatMissingData"]) == (2000, gt, "notBreaching")
    assert "Statistic" not in latency


def test_elb_alarms_use_the_stack_s_own_target_group_and_load_balancer(resources: dict[str, Any]) -> None:
    a = alarms(resources)
    tg = {"Fn::GetAtt": ["TargetGroup", "TargetGroupFullName"]}
    lb = {"Fn::GetAtt": ["Alb", "LoadBalancerFullName"]}
    for name, metric in (("kb-unhealthy-host", "UnHealthyHostCount"), ("kb-no-healthy-host", "HealthyHostCount")):
        assert a[name]["Namespace"] == "AWS/ApplicationELB" and a[name]["MetricName"] == metric
        assert dims(a[name]) == {"TargetGroup": tg, "LoadBalancer": lb}
    assert a["kb-5xx"]["MetricName"] == "HTTPCode_Target_5XX_Count"
    assert dims(a["kb-5xx"]) == {"LoadBalancer": lb}


def emitted_metrics() -> tuple[str, set[str], list[list[str]]]:
    """Namespace, metric names and dimension sets of a real EMF line from kb.metrics."""
    buf = io.StringIO()
    setup_logging(buf)
    metrics.emit_search_metrics("keyword", "high", 1.0)
    line = json.loads(buf.getvalue().strip().splitlines()[-1])
    emf = line["_aws"]["CloudWatchMetrics"][0]
    return emf["Namespace"], {m["Name"] for m in emf["Metrics"]}, emf["Dimensions"]


def test_kbmcp_alarms_match_the_metrics_the_app_emits(
    resources: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(metrics.MODE_ENV, raising=False)
    namespace, names, dimension_sets = emitted_metrics()
    assert dimension_sets == [["Mode"]]
    a = alarms(resources)
    for name in ("kb-keyword-mode", "kb-high-latency"):
        alarm = a[name]
        assert alarm["Namespace"] == namespace
        assert alarm["MetricName"] in names
        # A CloudWatch alarm only sees a metric under exactly the dimension set it was published with.
        assert [list(dims(alarm))] == dimension_sets
    assert a["kb-keyword-mode"]["MetricName"] == "KeywordSearches"
    assert dims(a["kb-keyword-mode"]) == {"Mode": "keyword"}
    assert a["kb-high-latency"]["MetricName"] == "LatencyMs"
    assert dims(a["kb-high-latency"]) == {"Mode": "semantic"}
    assert metrics.NAMESPACE == namespace


def test_keyword_mode_alarm_reads_what_a_keyword_search_adds() -> None:
    buf = io.StringIO()
    setup_logging(buf)
    metrics.emit_search_metrics("keyword", "low", 5.0)
    metrics.emit_search_metrics("semantic", "high", 5.0)
    keyword, semantic = (json.loads(ln) for ln in buf.getvalue().strip().splitlines()[-2:])
    assert (keyword["Mode"], keyword["KeywordSearches"]) == ("keyword", 1)
    assert (semantic["Mode"], semantic["KeywordSearches"]) == ("semantic", 0)


def test_memory_alarm_reads_what_the_agent_publishes(resources: dict[str, Any]) -> None:
    alarm = alarms(resources)["kb-memory"]
    assert (alarm["Namespace"], alarm["MetricName"]) == ("CWAgent", "mem_used_percent")
    assert dims(alarm) == {"AutoScalingGroupName": {"Ref": "AppAsg"}}
    config = json.loads(
        re.search(r"<<'CWA_CONFIG'\n(.*?)\nCWA_CONFIG\n", render_user_data(resources), re.S).group(1)  # type: ignore[union-attr]
    )["metrics"]
    assert config["namespace"] == alarm["Namespace"]
    assert alarm["MetricName"] in config["metrics_collected"]["mem"]["measurement"]
    assert config["aggregation_dimensions"] == [list(dims(alarm))]


def test_alarms_that_the_demo_triggers_say_so(resources: dict[str, Any]) -> None:
    a = alarms(resources)
    for name in ("kb-keyword-mode", "kb-unhealthy-host", "kb-no-healthy-host"):
        assert "on purpose" in a[name]["AlarmDescription"], name
    for name in ("kb-5xx", "kb-high-latency", "kb-memory"):
        assert "on purpose" not in a[name]["AlarmDescription"], name


def test_dashboard_body_is_valid_json(resources: dict[str, Any]) -> None:
    body, variables = resources["Dashboard"]["Properties"]["DashboardBody"]["Fn::Sub"]
    text = body
    for name in variables:
        text = text.replace("${" + name + "}", f"value-{name}")
    text = text.replace("${AWS::Region}", "us-east-1")
    assert "${" not in text
    widgets = json.loads(text)["widgets"]
    assert len(widgets) == 4
    metric_names = {m[1] for w in widgets for m in w["properties"]["metrics"]}
    assert metric_names == {"LatencyMs", "HealthyHostCount", "UnHealthyHostCount", "mem_used_percent"}


# --- phase 12: S3 lifecycle ---------------------------------------------------------------


def test_backups_and_images_expire_after_30_days_and_originals_and_snapshots_are_untouched() -> None:
    rules = load("data.yaml")["Resources"]["Bucket"]["Properties"]["LifecycleConfiguration"]["Rules"]
    by_id = {r["Id"]: r for r in rules}
    for rule_id, prefix in (("expire-backups", "backups/"), ("expire-images", "images/")):
        rule = by_id[rule_id]
        assert (rule["Status"], rule["Prefix"], rule["ExpirationInDays"]) == ("Enabled", prefix, 30)
    assert "expire-noncurrent-versions" in by_id
    # Only backups/ and images/ expire their current versions: originals/ and snapshots/ never do.
    expiring = {r["Prefix"] for r in rules if "ExpirationInDays" in r}
    assert expiring == {"backups/", "images/"}
    assert all(r.get("Prefix") in (None, "backups/", "images/") for r in rules)


# --- sandbox change: no ECR, no EFS; image and database go through S3 ----------------------

FORBIDDEN_TYPES = ("AWS::EFS::", "AWS::ECR::")
FORBIDDEN_WORDS = re.compile(r"\befs\b|elasticfilesystem|\becr\b|RepoUri|kb-Efs|amazon-efs-utils|RequiresMountsFor", re.I)


def walk(node: Any) -> Any:
    """Yield every value in a parsed template."""
    yield node
    if isinstance(node, dict):
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from walk(value)


def test_no_efs_or_ecr_resource_remains_in_any_template() -> None:
    for name in ("network.yaml", "data.yaml", "app.yaml"):
        for res_name, res in load(name)["Resources"].items():
            assert not res["Type"].startswith(FORBIDDEN_TYPES), f"{name}: {res_name} is {res['Type']}"


def test_no_efs_or_ecr_export_or_import_remains() -> None:
    exports: set[str] = set()
    imports: set[str] = set()
    for name in ("network.yaml", "data.yaml", "app.yaml"):
        template = load(name)
        exports |= {o["Export"]["Name"] for o in template.get("Outputs", {}).values() if "Export" in o}
        imports |= {n["Fn::ImportValue"] for n in walk(template) if isinstance(n, dict) and "Fn::ImportValue" in n}
    assert not {e for e in exports | imports if re.search(r"Efs|RepoUri", e)}
    assert "kb-BucketName" in exports
    assert imports <= exports, f"imported but never exported: {imports - exports}"


def code_lines(text: str) -> list[tuple[int, str]]:
    """Numbered lines that are not whole-line comments (comments may explain why ECR and EFS are gone)."""
    return [(n, ln) for n, ln in enumerate(text.splitlines(), start=1) if not ln.lstrip().startswith("#")]


def test_templates_and_scripts_do_not_use_efs_or_ecr() -> None:
    for path in (INFRA / "network.yaml", INFRA / "data.yaml", INFRA / "app.yaml", DEPLOY, PUSH, TEARDOWN, SCRIPT):
        for number, line in code_lines(path.read_text(encoding="utf-8")):
            assert not FORBIDDEN_WORDS.search(line), f"{path.name}:{number}: {line.strip()}"
    rendered = render_user_data(load("app.yaml")["Resources"])
    for number, line in code_lines(rendered):
        assert not FORBIDDEN_WORDS.search(line), f"user data line {number}: {line.strip()}"


def test_user_data_loads_the_image_from_s3_not_a_registry(resources: dict[str, Any]) -> None:
    rendered = render_user_data(resources)
    assert 'IMAGE="kb-mcp:abc1234"' in rendered
    assert 'IMAGE_URI="s3://VALUE_BUCKET/images/kb-mcp-abc1234.tar.gz"' in rendered
    assert 'aws s3 cp "$IMAGE_URI" - --region us-east-1 | gunzip -c | docker load' in rendered
    assert "retry 5 10 load_image" in rendered
    assert "docker pull" not in rendered and "docker login" not in rendered and "docker tag" not in rendered
    template, variables = user_data_parts(resources)
    assert set(variables) == {"Bucket", "LogGroupName"}
    assert variables["Bucket"] == {"Fn::ImportValue": "kb-BucketName"}


def test_user_data_restores_the_database_before_the_app_starts(resources: dict[str, Any]) -> None:
    rendered = render_user_data(resources)
    flat = rendered.replace("\\\n", "")
    restore = "python -m kb.cli restore --latest --force --target /data/kb.db"
    assert restore in flat
    assert re.search(r"-e KB_DB=/data/kb\.db\s+-e KB_BUCKET=VALUE_BUCKET", flat)
    assert "retry 5 15 restore_db" in rendered
    # Order: directory, image, restore, then the service container, then the health wait.
    order = [
        rendered.index("mkdir -p /data"),
        rendered.index("chown 10001:10001 /data"),
        rendered.index("retry 5 10 load_image"),
        rendered.index("retry 5 15 restore_db"),
        rendered.index("docker run -d --name kb"),
        rendered.index("curl -sf http://localhost:8000/healthz"),
    ]
    assert order == sorted(order)
    # The restore container runs the loaded image, mounts the same directory, and is not the service.
    squashed = re.sub(r"\s+", " ", flat)
    assert 'docker run --rm -v /data:/data -e KB_DB=/data/kb.db' in squashed
    assert f'"$IMAGE" {restore}' in squashed
    assert "--name kb" not in squashed.split(restore)[0].rsplit("docker run", 1)[1]


def test_service_container_has_a_stop_timeout_for_the_final_snapshot(resources: dict[str, Any]) -> None:
    rendered = render_user_data(resources).replace("\\\n", "")
    run = next(ln for ln in rendered.splitlines() if ln.strip().startswith("docker run -d --name kb"))
    assert "--stop-timeout 30" in run
    assert "-v /data:/data" in run and "-e KB_DB=/data/kb.db" in run and "-e KB_BUCKET=VALUE_BUCKET" in run
    assert "--restart unless-stopped" in run


def test_data_is_a_plain_directory_not_a_mount(resources: dict[str, Any]) -> None:
    rendered = render_user_data(resources)
    assert "mount -t" not in rendered and "fstab" not in rendered
    assert "docker.service.d" not in rendered and "systemctl daemon-reload" not in rendered
    assert not re.search(r"dnf install -y .*(efs|nfs)", rendered)


def test_asg_spans_both_private_subnets_and_stays_at_one_instance(resources: dict[str, Any]) -> None:
    asg = resources["AppAsg"]["Properties"]
    assert asg["VPCZoneIdentifier"] == [
        {"Fn::ImportValue": "kb-PrivateSubnetA"},
        {"Fn::ImportValue": "kb-PrivateSubnetB"},
    ]
    assert (asg["MinSize"], asg["MaxSize"], asg["DesiredCapacity"]) == ("1", "1", "1")
    assert asg["HealthCheckGracePeriod"] == 600
    assert "EfsIngressFromApp" not in resources["AppAsg"].get("DependsOn", [])


def test_image_tag_parameter_describes_the_s3_object(app: dict[str, Any]) -> None:
    description = app["Parameters"]["ImageTag"]["Description"]
    assert "s3://<bucket>/images/kb-mcp-<tag>.tar.gz" in description and "ECR" not in description


# --- phase 16: extension origin and client address hops ------------------------------------

EXTENSION_ID = "abcdefghijklmnopabcdefghijklmnop"


def test_extension_origin_parameter_is_optional_and_shell_safe(app: dict[str, Any]) -> None:
    param = app["Parameters"]["ExtensionOrigin"]
    assert param["Type"] == "String" and param["Default"] == ""
    pattern = param["AllowedPattern"]
    assert re.fullmatch(pattern, "")
    assert re.fullmatch(pattern, f"chrome-extension://{EXTENSION_ID}")
    # It lands in a shell script, so nothing that could break out of the quotes is allowed.
    for bad in (
        "*", "https://evil.example", f"chrome-extension://{EXTENSION_ID}/", "chrome-extension://short",
        f'chrome-extension://{EXTENSION_ID}"; curl evil.example #', f"chrome-extension://{EXTENSION_ID} x",
        f"chrome-extension://{EXTENSION_ID}\n", "chrome-extension://" + "A" * 32,
    ):
        assert not re.fullmatch(pattern, bad), bad


def test_user_data_passes_the_extension_origin_and_proxy_hops_to_the_container(resources: dict[str, Any]) -> None:
    rendered = render_user_data(resources)
    assert '-e "KB_EXTENSION_ORIGIN=' in rendered
    assert "-e KB_PROXY_HOPS=2" in rendered
    # Empty parameter: the variable is set but empty, which the app reads as unset.
    assert '-e "KB_EXTENSION_ORIGIN="' in rendered
    template, _ = user_data_parts(resources)
    assert "KB_EXTENSION_ORIGIN=${ExtensionOrigin}" in template


# --- deploy.sh ----------------------------------------------------------------------------


def make_stub(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def stub_bin(tmp_path: Path) -> Path:
    """A directory first on PATH with stub `aws`, `cfn-lint`, `docker` and `curl`."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    make_stub(bin_dir, "cfn-lint", f'echo "cfn-lint $*" >> {log}\n')
    make_stub(bin_dir, "aws", f'echo "aws $*" >> {log}\n')
    return bin_dir


def run_script(
    script: Path, args: list[str], bin_dir: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    base = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(bin_dir.parent)}
    return subprocess.run(
        ["bash", str(script), *args],
        capture_output=True,
        text=True,
        env=base | (env or {}),
        timeout=60,
    )


def calls(bin_dir: Path) -> list[str]:
    log = bin_dir.parent / "calls.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


@pytest.mark.parametrize("missing", ["KB_CLOUDFRONT_PREFIX_LIST_ID", "KB_ALERT_EMAIL"])
def test_deploy_app_stops_with_a_clear_error_when_a_value_is_missing(stub_bin: Path, missing: str) -> None:
    env = {"KB_CLOUDFRONT_PREFIX_LIST_ID": "pl-3b927c52", "KB_ALERT_EMAIL": "me@example.com"}
    del env[missing]
    result = run_script(DEPLOY, ["app", "abc1234"], stub_bin, env)
    assert result.returncode == 2
    assert missing in result.stderr and "is not set" in result.stderr
    assert calls(stub_bin) == []  # failed before linting or calling AWS


def test_deploy_app_treats_empty_values_as_missing(stub_bin: Path) -> None:
    env = {"KB_CLOUDFRONT_PREFIX_LIST_ID": "", "KB_ALERT_EMAIL": "me@example.com"}
    result = run_script(DEPLOY, ["app", "abc1234"], stub_bin, env)
    assert result.returncode == 2 and "KB_CLOUDFRONT_PREFIX_LIST_ID" in result.stderr


def test_deploy_app_passes_all_parameters_to_cloudformation(stub_bin: Path) -> None:
    env = {"KB_CLOUDFRONT_PREFIX_LIST_ID": "pl-3b927c52", "KB_ALERT_EMAIL": "me@example.com"}
    result = run_script(DEPLOY, ["app", "abc1234"], stub_bin, env)
    assert result.returncode == 0, result.stderr
    deploy = next(c for c in calls(stub_bin) if c.startswith("aws cloudformation deploy"))
    assert "--stack-name kb-app" in deploy
    after = deploy.split("--parameter-overrides", 1)[1].split()
    assert after == [
        "ImageTag=abc1234",
        "CloudFrontPrefixListId=pl-3b927c52",
        "AlertEmail=me@example.com",
        "MetricsMode=emf",
        "ExtensionOrigin=",
    ]
    assert any("DistributionDomainName" in c for c in calls(stub_bin))


def test_deploy_app_passes_a_metrics_mode_override(stub_bin: Path) -> None:
    env = {"KB_CLOUDFRONT_PREFIX_LIST_ID": "pl-1", "KB_ALERT_EMAIL": "me@example.com", "KB_METRICS_MODE": "api"}
    run_script(DEPLOY, ["app", "abc1234"], stub_bin, env)
    assert "MetricsMode=api" in next(c for c in calls(stub_bin) if "cloudformation deploy" in c)


def test_deploy_app_passes_the_extension_origin_when_set(stub_bin: Path) -> None:
    env = {
        "KB_CLOUDFRONT_PREFIX_LIST_ID": "pl-1", "KB_ALERT_EMAIL": "me@example.com",
        "KB_EXTENSION_ORIGIN": f"chrome-extension://{EXTENSION_ID}",
    }
    run_script(DEPLOY, ["app", "abc1234"], stub_bin, env)
    deploy = next(c for c in calls(stub_bin) if "cloudformation deploy" in c)
    assert f"ExtensionOrigin=chrome-extension://{EXTENSION_ID}" in deploy.split()


def test_deploy_data_and_network_need_neither_value(stub_bin: Path) -> None:
    for stack in ("data", "network"):
        assert run_script(DEPLOY, [stack], stub_bin).returncode == 0
    assert not any("CloudFrontPrefixListId" in c for c in calls(stub_bin))


# --- restore-on-instance.sh ---------------------------------------------------------------

DOCKER_STUB = r"""
log={log}
echo "docker $*" >> "$log"
case "$1" in
  inspect)
    case "$*" in
      *Config.Image*) echo "kb-mcp:abc1234" ;;
      *) printf 'PATH=/usr/bin\nKB_DB=/data/kb.db\nKB_BUCKET=kb-bucket\nAWS_REGION=us-east-1\n' ;;
    esac ;;
  run) exit {run_exit} ;;
  *) exit 0 ;;
esac
"""


def stub_instance(bin_dir: Path, run_exit: int = 0, healthy: bool = True) -> None:
    log = bin_dir.parent / "calls.log"
    make_stub(bin_dir, "docker", DOCKER_STUB.format(log=log, run_exit=run_exit))
    make_stub(bin_dir, "curl", f'echo "curl $*" >> {log}\n' + ("echo \'{\"status\":\"ok\"}\'\n" if healthy else "exit 22\n"))


FAST = {"KB_HEALTH_TIMEOUT": "1"}


def test_restore_script_stops_restores_starts_then_checks_health(stub_bin: Path) -> None:
    stub_instance(stub_bin)
    result = run_script(SCRIPT, ["--latest"], stub_bin, FAST)
    assert result.returncode == 0, result.stderr
    lines = calls(stub_bin)
    verbs = [ln.split()[1] for ln in lines if ln.startswith("docker")]
    assert verbs == ["inspect"] * 4 + ["stop", "run", "start"]  # image, KB_DB, KB_BUCKET, AWS_REGION
    run = next(ln for ln in lines if ln.startswith("docker run"))
    assert "--rm" in run and "--volumes-from kb" in run
    assert "-e KB_DB=/data/kb.db" in run and "-e KB_BUCKET=kb-bucket" in run and "-e AWS_REGION=us-east-1" in run
    assert run.endswith("kb-mcp:abc1234 python -m kb.cli restore --latest --force")  # image from the running container
    stop_at = lines.index("docker stop kb")
    start_at = lines.index("docker start kb")
    assert stop_at < lines.index(run) < start_at < next(i for i, ln in enumerate(lines) if ln.startswith("curl"))
    assert lines.count("docker start kb") == 1


def test_restore_script_passes_a_key(stub_bin: Path) -> None:
    stub_instance(stub_bin)
    key = "backups/kb-20261011-071500.db.gz"
    assert run_script(SCRIPT, ["--key", key], stub_bin, FAST).returncode == 0
    run = next(ln for ln in calls(stub_bin) if ln.startswith("docker run"))
    assert run.endswith(f"restore --key {key} --force")


def test_failed_restore_still_restarts_the_app_and_fails(stub_bin: Path) -> None:
    stub_instance(stub_bin, run_exit=3)
    result = run_script(SCRIPT, ["--latest"], stub_bin, FAST)
    assert result.returncode == 3
    assert "restore FAILED" in result.stderr and "still in place" in result.stderr
    assert calls(stub_bin).count("docker start kb") == 1


def test_unhealthy_app_after_restore_is_a_failure(stub_bin: Path) -> None:
    stub_instance(stub_bin, healthy=False)
    result = run_script(SCRIPT, ["--latest"], stub_bin, FAST)
    assert result.returncode == 1 and "did not become healthy" in result.stderr


@pytest.mark.parametrize("args", [[], ["--force"], ["--key"], ["--latest", "extra"], ["--key", ""]])
def test_restore_script_usage_errors_touch_nothing(stub_bin: Path, args: list[str]) -> None:
    stub_instance(stub_bin)
    result = run_script(SCRIPT, args, stub_bin, FAST)
    assert result.returncode == 2 and "usage:" in result.stderr
    assert calls(stub_bin) == []


# --- push-image.sh ------------------------------------------------------------------------

AWS_PUSH_STUB = r"""
log={log}
echo "aws $*" >> "$log"
case "$1 $2" in
  "cloudformation list-exports") echo "{bucket}" ;;
  "s3 cp") cat > {uploaded}; echo "upload: stdin to s3://x" ; exit {cp_exit} ;;
  "s3 rm") exit 0 ;;
  "s3api head-object") echo "{length}" ;;
  *) exit 0 ;;
esac
"""

DOCKER_PUSH_STUB = r"""
log={log}
echo "docker $*" >> "$log"
case "$1" in
  image) case "$2" in inspect) [ "{have_image}" = yes ] || exit 1; echo 123456 ;; esac ;;
  save) printf 'IMAGE-BYTES'; exit {save_exit} ;;
  build) echo "building" ;;
  *) exit 0 ;;
esac
"""


def stub_push(
    bin_dir: Path, *, bucket: str = "kb-bucket", cp_exit: int = 0, length: str = "2048",
    save_exit: int = 0, have_image: str = "yes",
) -> Path:
    log = bin_dir.parent / "calls.log"
    uploaded = bin_dir.parent / "uploaded.bin"
    make_stub(bin_dir, "aws", AWS_PUSH_STUB.format(log=log, bucket=bucket, cp_exit=cp_exit, length=length, uploaded=uploaded))
    make_stub(bin_dir, "docker", DOCKER_PUSH_STUB.format(log=log, save_exit=save_exit, have_image=have_image))
    return uploaded


def run_push(bin_dir: Path, args: list[str] | None = None) -> subprocess.CompletedProcess[str]:
    base = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(bin_dir.parent)}
    return subprocess.run(["bash", str(PUSH), *(args or [])], capture_output=True, text=True, env=base, timeout=60)


def test_push_image_uploads_a_gzipped_docker_save_and_prints_only_the_tag(stub_bin: Path) -> None:
    uploaded = stub_push(stub_bin)
    result = run_push(stub_bin)
    assert result.returncode == 0, result.stderr
    assert re.fullmatch(r"[0-9a-f]{7,40}(-dirty)?\n", result.stdout), repr(result.stdout)
    tag = result.stdout.strip()
    lines = calls(stub_bin)
    assert f"docker tag kb-mcp:dev kb-mcp:{tag}" in lines
    assert f"docker save kb-mcp:{tag}" in lines
    cp = next(ln for ln in lines if ln.startswith("aws s3 cp"))
    assert cp == f"aws s3 cp - s3://kb-bucket/images/kb-mcp-{tag}.tar.gz --region us-east-1 --sse AES256"
    assert gzip.decompress(uploaded.read_bytes()) == b"IMAGE-BYTES"  # docker save | gzip, streamed
    head = next(ln for ln in lines if ln.startswith("aws s3api head-object"))
    assert f"--bucket kb-bucket --key images/kb-mcp-{tag}.tar.gz" in head
    assert lines.index(cp) < lines.index(head)
    assert "ecr" not in " ".join(lines).lower()
    assert f"s3://kb-bucket/images/kb-mcp-{tag}.tar.gz" in result.stderr


def test_push_image_reads_the_bucket_from_the_kb_bucketname_export(stub_bin: Path) -> None:
    stub_push(stub_bin, bucket="another-bucket")
    assert run_push(stub_bin).returncode == 0
    lines = calls(stub_bin)
    assert any("list-exports" in ln and "kb-BucketName" in ln for ln in lines)
    assert any("s3://another-bucket/images/" in ln for ln in lines)


def test_push_image_stops_when_the_data_stack_is_missing(stub_bin: Path) -> None:
    stub_push(stub_bin, bucket="None")
    result = run_push(stub_bin)
    assert result.returncode == 1 and "kb-BucketName" in result.stderr
    assert not any(ln.startswith(("docker save", "aws s3 cp")) for ln in calls(stub_bin))


def test_push_image_removes_the_partial_object_when_the_upload_fails(stub_bin: Path) -> None:
    stub_push(stub_bin, save_exit=1)  # docker save dies halfway; aws still finishes the truncated stream
    result = run_push(stub_bin)
    assert result.returncode == 1 and result.stdout == ""
    assert any(ln.startswith("aws s3 rm s3://kb-bucket/images/kb-mcp-") for ln in calls(stub_bin))
    assert "partial" in result.stderr


def test_push_image_fails_when_the_object_is_empty_afterwards(stub_bin: Path) -> None:
    stub_push(stub_bin, length="0")
    result = run_push(stub_bin)
    assert result.returncode == 1 and result.stdout == "" and "missing or empty" in result.stderr


def test_push_image_needs_the_local_image_or_build(stub_bin: Path) -> None:
    stub_push(stub_bin, have_image="no")
    result = run_push(stub_bin)
    assert result.returncode == 1 and "--build" in result.stderr and result.stdout == ""
    assert not any(ln.startswith("aws") for ln in calls(stub_bin))
    built = run_push(stub_bin, ["--build"])
    assert any(ln.startswith("docker build --platform linux/amd64 -t kb-mcp:dev") for ln in calls(stub_bin))
    assert built.stdout.strip() == "" or re.fullmatch(r"[0-9a-f]{7,40}(-dirty)?", built.stdout.strip())


def test_push_image_rejects_unknown_arguments(stub_bin: Path) -> None:
    stub_push(stub_bin)
    result = run_push(stub_bin, ["--ecr"])
    assert result.returncode == 2 and "usage:" in result.stderr and calls(stub_bin) == []


# --- house rules --------------------------------------------------------------------------


def test_no_em_or_en_dashes_in_files_from_these_phases() -> None:
    paths = [
        INFRA / "app.yaml",
        INFRA / "data.yaml",
        INFRA / "network.yaml",
        DEPLOY,
        PUSH,
        TEARDOWN,
        SCRIPT,
        ROOT / "kb" / "backup.py",
        ROOT / "kb" / "metrics.py",
        ROOT / "kb" / "cli.py",
        ROOT / "kb" / "db.py",
        ROOT / "kb" / "snapshot.py",
        ROOT / "kb" / "config.py",
        ROOT / "kb" / "main.py",
        Path(__file__),
        Path(__file__).with_name("test_backup.py"),
        Path(__file__).with_name("test_metrics.py"),
        Path(__file__).with_name("test_snapshot.py"),
        Path(__file__).with_name("test_commit_hook.py"),
    ]
    em, en = chr(0x2014), chr(0x2013)
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert em not in text and en not in text, path.name

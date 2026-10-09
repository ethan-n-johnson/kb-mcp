# Phase 06: App stack

**Day:** 3 (Fri Oct 9) · **Depends on:** 01, 03 · **Where:** AWS · **PLAN.md sections:** AWS deployment (kb-app row, deploy steps 4, 6, 7); Auth and security (security group chain, no SSH, instance metadata); Course alignment

## Goal

`kb-app` running the phase 03 image behind an ALB: NAT gateway, security group chain, launch template, Auto Scaling group across both private subnets, the image loaded from S3, and the database restored from the S3 snapshot onto the local `/data` directory before the app starts. CloudFront, alarms and SNS come later (phases 10 and 15).

## Read first

- `CLAUDE.md`, `docs/PROGRESS.md` (Outputs, deviations, current image tag)
- `docs/PLAN.md`: "AWS deployment", "Auth and security"

## Tasks

1. `infra/app.yaml` parameters: `ImageTag` (required), `InstanceType` (default `t3.medium`), `AmiId` of type `AWS::SSM::Parameter::Value<AWS::EC2::Image::Id>` defaulting to `/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64`. Import everything else from the `kb-` exports.
2. Networking:
   - Elastic IP and NAT gateway in public subnet A; `AWS::EC2::Route` `0.0.0.0/0` in the imported private route table pointing at the NAT gateway.
3. Security groups (all in the imported VPC):
   - `AlbSg`: ingress TCP 80 from `0.0.0.0/0` **for now** (phase 10 locks it to the CloudFront prefix list).
   - `AppSg`: ingress TCP 8000 from `AlbSg` only. No port 22.
4. Load balancer: internet-facing ALB in both public subnets; target group HTTP 8000, target type `instance`, health check `/healthz`, interval 15 s, timeout 5 s, healthy threshold 2, unhealthy threshold 3, matcher 200, deregistration delay 30 s; listener on 80 forwarding to it.
5. Launch template:
   - `LabInstanceProfile`; no public IP; `MetadataOptions`: `HttpTokens: required`, `HttpPutResponseHopLimit: 2` (deviation 1 in PROGRESS.md: the container needs the instance role for SSM and S3).
   - Root volume 30 GiB gp2 (the sandbox allows gp2 only, up to 35 GB), encrypted. KMS is list only in this sandbox, so if the launch fails with `Client.InternalError`, set `Encrypted: false` in the launch template, record the change in `docs/PROGRESS.md` (deviations) and `docs/SECURITY_REVIEW.md`, and tell the user.
   - `UserData` inline with `Fn::Base64` + `!Sub` (deviation 2). Remember: inside `!Sub`, a literal shell `${VAR}` must be written `${!VAR}`. The script (`set -euxo pipefail`, log to `/var/log/kb-bootstrap.log`):
     1. `dnf install -y docker aws-cfn-bootstrap cronie`; enable and start `docker` and `crond` (retry: the NAT route can lag the first boot).
     2. `mkdir -p /data`; `chown 10001:10001 /data`. `/data` is a plain directory on the root volume.
     3. Fetch the image with the instance role, retried: `aws s3 cp s3://<Bucket>/images/kb-mcp-<ImageTag>.tar.gz - | gunzip -c | docker load` (through the S3 gateway endpoint, not the NAT). The image to run is `kb-mcp:<ImageTag>`.
     4. Restore before the app starts, retried: a one-off `docker run --rm -v /data:/data -e KB_DB=/data/kb.db -e KB_BUCKET=<Bucket> -e AWS_REGION=<Region> kb-mcp:<ImageTag> python -m kb.cli restore --latest --force --target /data/kb.db` (snapshot, else newest backup, else nothing and exit 0). If it keeps failing, the boot fails (never start empty over a snapshot that could not be read).
     5. `docker run -d --name kb --restart unless-stopped --stop-timeout 30 -p 8000:8000 -v /data:/data -e KB_DB=/data/kb.db -e KB_BUCKET=<Bucket> -e AWS_REGION=<Region> kb-mcp:<ImageTag>`. The stop timeout gives the shutdown snapshot flush time.
     6. Wait up to 10 minutes for `curl -sf localhost:8000/healthz`, then `/opt/aws/bin/cfn-signal -e $? --stack ${AWS::StackName} --resource AppAsg --region ${AWS::Region}`.
     Leave clearly marked spots for the backup cron (phase 12) and the CloudWatch agent and log driver (phase 15).
6. Auto Scaling group `AppAsg`: min 1, max 1, desired 1; both private subnets; target group attached; `HealthCheckType: ELB`, `HealthCheckGracePeriod: 600`. `CreationPolicy` with `ResourceSignal` count 1, timeout `PT15M`. `UpdatePolicy.AutoScalingRollingUpdate`: `MinInstancesInService: 0`, `MaxBatchSize: 1`, `WaitOnResourceSignals: true`, `PauseTime: PT15M`. The image tag appears in the user data, so changing `ImageTag` creates a new launch template version and triggers the rolling replacement.
7. Outputs: `AlbDnsName`, `AsgName`, `TargetGroupArn`, `LoadBalancerFullName`, `TargetGroupFullName` (the last two are for CloudWatch alarms in phase 15).
8. Extend `infra/deploy.sh`: `deploy.sh app <ImageTag>` passes `--parameter-overrides ImageTag=<tag>`, prints the ALB DNS name. Add `infra/teardown-app.sh` that deletes only `kb-app` after a confirmation prompt.
9. Deploy. If the stack fails, read the failure from `describe-stack-events`, and if the instance started, read `/var/log/kb-bootstrap.log` with `aws ssm send-command --document-name AWS-RunShellScript` (works without the Session Manager plugin). If SSM is unavailable, `aws ec2 get-console-output --instance-id <id> --latest` shows cloud-init output. If NAT gateway, `t3.medium` or the encrypted root volume is denied, stop and apply the matching fallback from PLAN.md's risk table only after the user agrees.
10. **HUMAN (optional):** with the Session Manager plugin installed, `aws ssm start-session --target <instance-id>` to confirm interactive shells work. Record whether it works; if not, note the bastion fallback.

## Do not

- Add CloudFront, alarms, SNS, the CloudWatch agent or backups.
- Open port 22 or give the instance a public IP.
- Raise ASG max above 1.

## Acceptance

- [ ] `cfn-lint infra/app.yaml` clean; stack `CREATE_COMPLETE` (the creation signal was received).
- [ ] `aws elbv2 describe-target-health --target-group-arn <arn>` shows one `healthy` target.
- [ ] `curl -s http://<AlbDnsName>/healthz` returns 200 JSON.
- [ ] Instance has no public IP (`describe-instances` query on `PublicIpAddress` is null) and `MetadataOptions` shows `required` and hop limit 2.
- [ ] Via `ssm send-command`: `ls -ld /data` shows a directory owned by uid 10001 (not a mount: `findmnt /data` prints nothing), `ls -l /data/kb.db` exists, and `docker image ls kb-mcp` lists `kb-mcp:<ImageTag>`.
- [ ] After one write (`POST /api/docs` with a write token), `aws s3api head-object --bucket <bucket> --key snapshots/kb-latest.db.gz` succeeds within about `KB_SNAPSHOT_INTERVAL` + 10 seconds, and the container log has a `snapshot uploaded` line with `key`, `bytes` and `ms`.
- [ ] ALB DNS name recorded in PROGRESS.md Outputs; Session Manager result recorded.
- [ ] Ask the user whether to keep `kb-app` running or delete it with `infra/teardown-app.sh` until the next session.
- [ ] Phase log updated; commit `phase 06: app stack`.

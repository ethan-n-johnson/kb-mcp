# Phase 01: Network and data stacks

**Day:** 1 (Wed Oct 7) · **Depends on:** 00 · **Where:** AWS · **PLAN.md sections:** AWS deployment (stack table, deploy steps 1 and 2); Course alignment; Auth and security (Tokens bullet)

## Goal

Deploy `kb-network` and `kb-data` with CloudFormation, and create the write-token parameter. These hold all state and cost almost nothing idle.

## Read first

- `CLAUDE.md`, `docs/PROGRESS.md` (Environment table)
- `docs/PLAN.md`: "AWS deployment"

## Tasks

1. `infra/network.yaml`:
   - VPC `10.0.0.0/16`, `EnableDnsSupport` and `EnableDnsHostnames` true (the S3 gateway endpoint and instance DNS names need them).
   - Public subnets `10.0.1.0/24` (AZ a) and `10.0.2.0/24` (AZ b), `MapPublicIpOnLaunch: true`. Private subnets `10.0.11.0/24` (AZ a) and `10.0.12.0/24` (AZ b). Pick AZs with `!Select [0, !GetAZs '']` and `!Select [1, !GetAZs '']`.
   - Internet gateway and attachment. Public route table with `0.0.0.0/0` to the IGW, associated with both public subnets.
   - One private route table associated with both private subnets. **No default route here**; the app stack adds the NAT route so the NAT gateway can be deleted with `kb-app`.
   - S3 gateway endpoint (`com.amazonaws.${AWS::Region}.s3`) attached to the private route table.
   - Tag everything `Project=kb-mcp`.
   - Exports (prefix `kb-`): `kb-VpcId`, `kb-VpcCidr`, `kb-PublicSubnetA`, `kb-PublicSubnetB`, `kb-PrivateSubnetA`, `kb-PrivateSubnetB`, `kb-PrivateRouteTableId`.
2. `infra/data.yaml`:
   - S3 bucket (let CloudFormation name it): versioning enabled, `BucketEncryption` with `AES256`, all four Block Public Access settings true, `DeletionPolicy: Retain`. Lifecycle rules: expire noncurrent versions after 30 days; expire `backups/` after 30 days; expire `images/` after 30 days; abort incomplete multipart uploads after 2 days. The bucket holds originals, the container image tarball (`images/`), the database snapshot (`snapshots/kb-latest.db.gz`, overwritten after every write, history kept by versioning) and the nightly backups (`backups/`).
   - No ECR repository and no EFS file system: both are blocked in the sandbox (see `docs/PLAN.md`, "Sandbox changes").
   - Export: `kb-BucketName`.
3. `infra/deploy.sh` helper: `deploy.sh network` and `deploy.sh data` run `cfn-lint`, `aws cloudformation validate-template`, then `aws cloudformation deploy --stack-name kb-<name> --template-file infra/<name>.yaml --no-fail-on-empty-changeset --tags Project=kb-mcp`. Leave a clear spot for `app` (phase 06). `set -euo pipefail`.
4. Deploy `kb-network`, then `kb-data`. If a resource is denied, stop, record it, and ask the user before changing the design.
5. **HUMAN:** create the token parameter so the secret never appears in Sonnet's transcript. Give the user this block to run in their own terminal:

   ```bash
   TOKENS="temp:$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))'),demo:$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
   aws ssm put-parameter --name /kb/write-tokens --type SecureString --value "$TOKENS" --region us-east-1
   echo "$TOKENS"   # copy both tokens into a password manager, then clear the terminal
   ```

6. Verify the parameter exists without decrypting it: `aws ssm get-parameter --name /kb/write-tokens --query Parameter.Type` returns `"SecureString"`.
7. Record the VPC ID and bucket name in the Outputs table of `docs/PROGRESS.md`.

## Do not

- Create the NAT gateway, ALB, instances or CloudFront (phase 06 and 10).
- Put any secret in a template, script or commit.
- Set an explicit bucket name (avoids collisions after a Retain delete).

## Acceptance

- [ ] `cfn-lint infra/network.yaml infra/data.yaml` reports nothing.
- [ ] `aws cloudformation describe-stacks --stack-name kb-network --query 'Stacks[0].StackStatus'` and the same for `kb-data` return `CREATE_COMPLETE` (or `UPDATE_COMPLETE`).
- [ ] `aws cloudformation list-exports --query "Exports[?starts_with(Name,'kb-')].Name"` lists all 8 exports (7 from `kb-network`, `kb-BucketName` from `kb-data`).
- [ ] `aws s3api get-bucket-lifecycle-configuration --bucket <name> --query 'Rules[].ID'` lists `expire-noncurrent-versions`, `expire-backups`, `expire-images` and `abort-incomplete-uploads`.
- [ ] `aws s3api get-bucket-versioning --bucket <name>` returns `Enabled`; `aws s3api get-public-access-block --bucket <name>` shows all four true.
- [ ] Token parameter type is `SecureString`.
- [ ] Phase log updated; commit `phase 01: network and data stacks`.

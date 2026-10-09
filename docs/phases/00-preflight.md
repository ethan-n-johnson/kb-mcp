# Phase 00: Preflight and repo setup

**Day:** 1 (Wed Oct 7) · **Depends on:** nothing · **Where:** local, AWS read-only · **PLAN.md sections:** Summary; AWS deployment; Risks, cut lines and open items

## Goal

Find out what the AWS Academy environment allows before any infrastructure is written, and get the repo and local tools ready. No AWS resources are kept from this phase (the two create probes below delete what they create at once). On 2026-10-08 the lab terminal role `runserver` already got AccessDenied for `ecr:CreateRepository` and `elasticfilesystem:CreateFileSystem`, which is why the design uses S3 for images and snapshots (see `docs/PLAN.md`, "Sandbox changes"). Re-run those two probes with the laptop credentials and record the result.

## Read first

- `CLAUDE.md`
- `docs/PROGRESS.md`
- `docs/PLAN.md`: "Summary", "AWS deployment", "Risks, cut lines and open items"

## Tasks

1. **HUMAN:** Start the AWS Academy session, copy the AWS CLI credentials into `~/.aws/credentials` under `[default]`, and tell Sonnet which environment this is (expected: the AWS Academy Cloud Architecting 3 sandbox).
2. Check local tools and record versions: `python3 --version` (need 3.12), `docker version`, `docker buildx version`, `aws --version` (need v2), `git --version`. Create `.venv` and `pip install cfn-lint`. If a tool is missing, tell the user the install command for their Linux distro and wait.
3. Identity: `aws sts get-caller-identity`, `aws configure get region` (must be `us-east-1`; if empty, run `aws configure set region us-east-1`).
4. Probe services with read-only or dry-run calls. Record each result (OK, AccessDenied, other error) in the Environment table of `docs/PROGRESS.md`:
   - `aws iam get-instance-profile --instance-profile-name LabInstanceProfile`
   - `aws ec2 describe-instance-type-offerings --location-type region --filters Name=instance-type,Values=t3.medium`
   - Dry run an instance launch with an encrypted gp2 root volume (the volume type and size the launch template uses): find a default-VPC subnet with `aws ec2 describe-subnets --filters Name=default-for-az,Values=true`, look up the AL2023 AMI with `aws ssm get-parameter --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64`, then `aws ec2 run-instances --dry-run --instance-type t3.medium --image-id <ami> --subnet-id <subnet> --iam-instance-profile Name=LabInstanceProfile --block-device-mappings 'DeviceName=/dev/xvda,Ebs={VolumeSize=30,VolumeType=gp2,Encrypted=true,DeleteOnTermination=true}'`. `DryRunOperation` means allowed; `UnauthorizedOperation` means blocked. A dry run does not exercise KMS, so a root volume that cannot be encrypted (KMS is list only in this sandbox) only shows up as `Client.InternalError` on the real launch in phase 06; the fallback is `Encrypted: false`.
   - ECR and EFS create probes (expected AccessDenied, probed from `runserver` on 2026-10-08): `aws ecr create-repository --repository-name kb-probe` and `aws efs create-file-system --tags Key=Name,Value=kb-probe`. If either unexpectedly succeeds, delete it at once (`aws ecr delete-repository --repository-name kb-probe --force`, `aws efs delete-file-system --file-system-id <id>`), record that it is allowed, and tell the user; the S3-based design stays unless they decide otherwise.
   - `aws cloudfront list-distributions --max-items 1`
   - `aws ssm describe-parameters --max-results 1`
   - `aws ssm describe-instance-information --max-results 5` (Session Manager API reachable)
   - `aws elbv2 describe-load-balancers`, `aws ec2 describe-nat-gateways`, `aws sns list-topics`, `aws cloudwatch describe-alarms --max-records 1`, `aws logs describe-log-groups --limit 1`, `aws cloudformation list-stacks`
   - CloudFront origin-facing prefix list: `aws ec2 describe-managed-prefix-lists --filters Name=prefix-list-name,Values=com.amazonaws.global.cloudfront.origin-facing --query 'PrefixLists[0].[PrefixListId,MaxEntries]'`. Record the ID and max entries.
5. For every blocked service, look up the matching row in PLAN.md's risk table and write the fallback you will use under "Open issues".
6. Repo setup in `kb-mcp/` (this folder already holds `CLAUDE.md` and `docs/`):
   - `git init`, default branch `main`.
   - `.gitignore`: `.venv/`, `__pycache__/`, `*.db`, `*.db-journal`, `.localdata/`, `.env`, `models/`, `.pytest_cache/`, `dist/`, `*.zip`.
   - Commit: `phase 00: preflight and repo setup`.
7. Ask the user the two remaining open items (demo date confirmed? instructor deliverables?) and record the answers in `docs/PROGRESS.md`.

## Do not

- Create, modify or delete any AWS resource, other than the two create probes above (which must be deleted immediately if they succeed).
- Write application code or templates.

## Acceptance

- [ ] `aws sts get-caller-identity` succeeds.
- [ ] Every row of the Environment table in `docs/PROGRESS.md` is filled in, including the ECR, EFS and encrypted gp2 dry-run rows and `LabInstanceProfile`.
- [ ] Any blocked service has a fallback written under "Open issues".
- [ ] `git log --oneline` shows the phase 00 commit.

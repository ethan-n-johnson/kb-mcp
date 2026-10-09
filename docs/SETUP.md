# Setup: laptop and AWS Academy sandbox

How to get a computer ready to build and deploy kb-mcp into the **AWS Academy Cloud Architecting 3 sandbox**. Do the one-time setup once, then the per-session steps every time you start the lab.

You never upload the project into the sandbox. The code stays on your computer, your AWS CLI is pointed at the sandbox account with temporary credentials, and the scripts in `infra/` create everything in AWS from there. The app itself travels as a Docker image file that `infra/push-image.sh` uploads to S3.

## What you need

| Tool | Why |
| --- | --- |
| AWS CLI v2 | Every deploy script and phase check calls `aws` |
| Docker (with buildx) | Builds the app image (`infra/push-image.sh`) |
| Python 3.12 or newer, with venv | Runs the tests and `cfn-lint` |
| git, curl, unzip | Repo, installers |
| A bash shell | The scripts in `infra/` are bash; on Windows this means WSL |
| Claude Code | Runs the phases with Sonnet (see `docs/phases/README.md`) |

The Linux and Windows sections below install all of these. Pick one.

---

## Linux (one-time setup)

Commands are for Ubuntu or Debian, with Fedora versions where the package names differ. The AWS CLI steps are the same everywhere.

### 1. Basic tools and Python

Ubuntu or Debian:

```bash
sudo apt update
sudo apt install -y git curl unzip python3 python3-venv python3-pip
python3 --version   # must be 3.12 or newer
```

Fedora (venv is built into Fedora's Python, so there is no separate package):

```bash
sudo dnf install -y git curl unzip python3 python3-pip
python3 --version   # must be 3.12 or newer
```

### 2. AWS CLI v2

```bash
aws --version   # skip the rest of this step if it prints aws-cli/2.x
```

If you get "command not found":

```bash
cd /tmp
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o awscliv2.zip
unzip -q awscliv2.zip
sudo ./aws/install
aws --version
```

On an ARM machine (`uname -m` prints `aarch64`), use `awscli-exe-linux-aarch64.zip` instead.

### 3. Docker

`infra/push-image.sh --build` stops with `docker: command not found` until this step is done.

Ubuntu or Debian:

```bash
sudo apt install -y docker.io docker-buildx
sudo usermod -aG docker "$USER"
```

Fedora (Docker's own repository; `docker.io` does not exist there, and the service has to be started by hand):

```bash
sudo dnf -y install dnf-plugins-core
sudo dnf config-manager addrepo --from-repofile=https://download.docker.com/linux/fedora/docker-ce.repo
sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
```

On Fedora 40 or older, the repo line is `sudo dnf config-manager --add-repo https://download.docker.com/linux/fedora/docker-ce.repo`.

Log out and back in (or reboot) so the group change applies, then check:

```bash
docker run --rm hello-world
docker buildx version
```

### 4. Create the AWS credentials file

The `~/.aws` folder does not exist until you create it. It is hidden because its name starts with a dot (`ls -a ~` shows it).

```bash
mkdir -p ~/.aws
touch ~/.aws/credentials
chmod 600 ~/.aws/credentials
aws configure set region us-east-1
```

The last line creates `~/.aws/config` with the region. The credentials themselves go in during the per-session steps below.

### 5. Get the project onto the computer

Cloning with git is the safest way. If the folder was copied instead (a zip download, a USB stick, a file share), two things are usually lost, and both break the scripts:

- **The executable bit on the scripts.** Symptom: `bash: infra/deploy.sh: Permission denied`. From the `kb-mcp` folder:

  ```bash
  chmod +x infra/*.sh infra/scripts/*.sh
  ```

- **The `.git` folder.** `infra/push-image.sh` tags the image with the git commit, so it fails outside a git repository. Check with `git status`; if it says "not a git repository", make one:

  ```bash
  git init
  git add -A
  git commit -m "initial import"
  ```

A space in the folder path (for example `~/Documents/Cloud Computing/kb-mcp`) is fine; the scripts quote their paths.

### 6. Project virtualenv

`infra/deploy.sh` lints every template with `cfn-lint`, which is installed here. Without this step it stops with `cfn-lint not found`. From the `kb-mcp` folder:

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q   # fast tests, should all pass
ls .venv/bin/cfn-lint           # deploy.sh finds it here, no need to activate the venv
```

Now continue with **Every lab session** below.

---

## Windows (one-time setup)

The deploy scripts are bash, so on Windows you work inside **WSL** (Windows Subsystem for Linux, an Ubuntu environment that runs inside Windows). After step 2, everything is the Linux section above, run in the Ubuntu window.

### 1. Install WSL with Ubuntu

Open **PowerShell as Administrator** (Start menu, type PowerShell, right-click, Run as administrator):

```powershell
wsl --install
```

Reboot when asked. After the reboot an **Ubuntu** window opens and asks you to pick a Linux username and password. From now on, "the Ubuntu terminal" means that window (also in the Start menu as Ubuntu).

### 2. Install Docker Desktop

1. Download and install Docker Desktop for Windows from docker.com, keeping the "Use WSL 2" option checked.
2. Start Docker Desktop, open **Settings > Resources > WSL integration**, turn on integration for **Ubuntu**, and click Apply.
3. In the Ubuntu terminal, check: `docker run --rm hello-world`.

Skip Linux step 3 (Docker); Docker Desktop provides it.

### 3. Put the project inside WSL

Keep the repo in the Linux file system (for example `~/kb-mcp`), not under `C:\`. Builds are much faster there, and Windows line endings cannot break the bash scripts.

```bash
git config --global core.autocrlf input
```

Then copy or clone the `kb-mcp` folder into your Ubuntu home directory. From Windows, the Ubuntu files are reachable in File Explorer at `\\wsl$\Ubuntu\home\<your-linux-username>`.

### 4. Everything else

In the Ubuntu terminal, do Linux steps 1, 2, 4, 5 and 6. Note that WSL has its **own** `~/.aws` folder, separate from Windows; the scripts use the WSL one.

### Optional: AWS CLI in PowerShell too

Only needed if you also want to run `aws` commands from PowerShell. The deploy scripts do not need it.

```powershell
msiexec.exe /i https://awscli.amazonaws.com/AWSCLIV2.msi
# open a new PowerShell window, then:
aws --version
New-Item -ItemType Directory -Force "$HOME\.aws" | Out-Null
New-Item -ItemType File -Force "$HOME\.aws\credentials" | Out-Null
notepad "$HOME\.aws\credentials"
aws configure set region us-east-1
```

This credentials file (`C:\Users\<you>\.aws\credentials`) must be updated each session too, separately from the WSL one.

---

## Every lab session

The sandbox gives you temporary credentials that stop working when the lab session ends (a few hours). Do this each time you start the lab.

1. In AWS Academy, click **Start Lab** and wait for the green circle next to AWS.
2. Click **Details**, then **Show**. In the Credentials window, copy the whole **AWS CLI** block. If the window does not show one, run `cat ~/.aws/credentials` in the lab's browser terminal and copy that instead. It looks like:

   ```
   [default]
   aws_access_key_id=ASIA...
   aws_secret_access_key=...
   aws_session_token=...
   ```

3. Open your credentials file and **replace everything in it** with the block you copied:
   - Linux or WSL: `nano ~/.aws/credentials`, paste, then Ctrl+O, Enter, Ctrl+X.
   - PowerShell (optional): `notepad "$HOME\.aws\credentials"`, paste, save.
4. Check it works:

   ```bash
   aws sts get-caller-identity
   ```

   It should print the sandbox account ID. **Compare it with the last session.** When a session times out, the sandbox can come back as a brand new, empty AWS account (seen on 2026-10-09: `849928937619` in the morning, `983926826929` in the afternoon). Everything from the old account is gone then: the stacks, the bucket with the image, the write tokens and the database snapshots.
5. Bring the app up with one command (it skips whatever already exists, so it is safe to run every session):

   ```bash
   source .venv/bin/activate
   infra/up.sh
   ```

   The first time, create `~/.config/kb-mcp/up.env` with one line, `KB_ALERT_EMAIL=you@example.com` (that folder is outside the repo). `up.sh` deploys `kb-network` and `kb-data`, uploads the write tokens (from `~/.config/kb-mcp/write-tokens`, generated there on the first run; open that file to copy them), uploads your saved database snapshot if the bucket has none, builds and uploads the image unless the bucket already has it for the current commit, looks up the CloudFront prefix list and deploys `kb-app`. On an empty account this takes about 20 to 30 minutes (image upload, NAT gateway, CloudFront). Confirm the SNS email again after a new account.
6. **Before the session ends**, save the knowledge base so the next account can restore it:

   ```bash
   infra/up.sh save
   ```

   It downloads `snapshots/kb-latest.db.gz` to `~/.config/kb-mcp/kb-latest.db.gz`. Without it, a new account starts with an empty knowledge base.
7. Start Claude Code in `kb-mcp` and continue with the next phase (`docs/phases/README.md` has the prompt).

When any AWS command later fails with `ExpiredToken` or `InvalidClientTokenId`, the session ran out: click Start Lab again and repeat steps 2 to 5.

---

## Deploy flow (what the phases run)

`infra/up.sh` (above) runs all of this in order and skips what exists. The steps below are what it does, for running one piece by hand. Run from `kb-mcp` in a bash shell (Linux, or the Ubuntu terminal on Windows). Run the commands **one at a time** and check each one finished without an error before the next; pasting the whole block keeps going after a failure.

```bash
infra/deploy.sh network          # VPC, subnets, S3 gateway endpoint (phase 01)
infra/deploy.sh data             # S3 bucket (phase 01)
```

Next, create the write tokens: the block in `docs/phases/01-network-data-stacks.md`, step 5 (you run it yourself). It stores them in the data bucket at `config/write-tokens`; the app instances read them from there because the sandbox instance role cannot read SSM. If you made the tokens in SSM with an older version of that step, the same step has a command that copies them over. Then build and upload the image, keeping the tag it prints:

```bash
TAG=$(infra/push-image.sh --build)   # build the image, upload it to S3 (phase 03); rebuild after any change under kb/
echo "$TAG"
```

Then the app stack. Both variables need real values, not the examples:

- `KB_CLOUDFRONT_PREFIX_LIST_ID`: phase 00 records it in `docs/PROGRESS.md`, or look it up:

  ```bash
  aws ec2 describe-managed-prefix-lists --region us-east-1 \
    --filters Name=prefix-list-name,Values=com.amazonaws.global.cloudfront.origin-facing \
    --query 'PrefixLists[0].PrefixListId' --output text
  ```

- `KB_ALERT_EMAIL`: an address you can read; confirm the SNS email it receives.

```bash
KB_CLOUDFRONT_PREFIX_LIST_ID=pl-xxxxxxxx KB_ALERT_EMAIL=you@example.com \
  infra/deploy.sh app "$TAG"     # NAT, ALB, Auto Scaling, CloudFront, alarms (phases 06, 10, 15)
```

The app instances use the existing `myS3Role` instance profile: the sandbox has no `LabInstanceProfile`, does not allow creating roles, and only lets you pass `myS3Role` to an instance (with `EMR_EC2_DefaultRole` the deploy fails with "You are not authorized to use launch template"). To use another profile (for example `LabInstanceProfile` in Learner Lab), set `KB_INSTANCE_PROFILE=<name>` in front of `infra/deploy.sh app`.

If the app stack fails, show only the failures:

```bash
aws cloudformation describe-stack-events --region us-east-1 --stack-name kb-app --no-cli-pager \
  --query "StackEvents[?contains(ResourceStatus,'FAILED')].[LogicalResourceId,ResourceStatusReason]" --output text
```

The bottom line is the first failure. A stack in `ROLLBACK_COMPLETE` must be deleted before the next try (`aws cloudformation delete-stack --stack-name kb-app`, then `aws cloudformation wait stack-delete-complete --stack-name kb-app`). If the instance itself failed to boot, its logs are in the bucket: `aws s3 ls "s3://$BUCKET/debug/"`, then `aws s3 cp "s3://$BUCKET/debug/<file>" -`.

Never type `<tag>` literally: bash reads `<` as a redirect and fails with `syntax error near unexpected token 'newline'`.

To save sandbox credit between sessions, delete only the app stack with `infra/teardown-app.sh`; the network and data stacks keep your data.

---

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `aws: command not found` | AWS CLI not installed, or open a new terminal after installing |
| `ExpiredToken` / `InvalidClientTokenId` | Lab session ended or old token: Start Lab, paste the new credentials block |
| `Unable to locate credentials` | `~/.aws/credentials` missing or empty, or in the wrong place (WSL and Windows have separate files) |
| `You must specify a region` | `aws configure set region us-east-1` |
| `AccessDenied` on a service | The sandbox blocks it (ECR and EFS are blocked by design); see "Open issues" in `docs/PROGRESS.md` |
| `permission denied ... docker.sock` (Linux) | You are not in the `docker` group yet: `sudo usermod -aG docker "$USER"`, then log out and in |
| `Cannot connect to the Docker daemon` (Windows) | Start Docker Desktop and check WSL integration for Ubuntu is on |
| `bash: infra/deploy.sh: Permission denied` | Scripts lost their executable bit (folder was copied, not cloned): `chmod +x infra/*.sh infra/scripts/*.sh` |
| `cfn-lint not found` | Project virtualenv missing: Linux step 6 |
| `docker: command not found` | Docker not installed: Linux step 3 (Fedora has its own commands) |
| `fatal: not a git repository` from `push-image.sh` | The folder has no `.git`: Linux step 5 |
| `Invalid IAM Instance Profile name` when deploying the app stack | The profile does not exist in this sandbox. The default is `myS3Role`; check with `aws iam list-instance-profiles --query 'InstanceProfiles[].InstanceProfileName'` and set `KB_INSTANCE_PROFILE` |
| `config/write-tokens is missing` in the debug boot log | Step 5 of phase 01 was not run (or wrote to SSM only): run it, then redeploy |
| AWS CLI output opens in a pager and stops at `:` | Press `q`; turn the pager off once with `aws configure set cli_pager ""` |
| `account ID` changed and `describe-stacks` shows none of the `kb-` stacks | The sandbox reset to a new account: run `infra/up.sh` (it rebuilds everything; the knowledge base comes back only from a snapshot saved with `infra/up.sh save`) |
| `You are not authorized to use launch template` | The lab user may not pass that instance profile (`iam:PassRole`). Use `myS3Role` (the default) |
| `put_metric_data failed, will retry` warnings in the container log | Expected in the sandbox: `myS3Role` cannot publish metrics. Searches are not affected |
| Boot log shows `PermissionError: [Errno 13] Permission denied: '/app/kb/__init__.py'` | The image was built from owner-only files (fixed in the Dockerfile on 2026-10-09). Rebuild with `infra/push-image.sh --build`; optionally also run `chmod -R go+rX .` in the repo |
| `syntax error near unexpected token 'newline'` | A placeholder like `<tag>` was typed literally: use the real tag, or `"$TAG"` |
| `$'\r': command not found` running a script | Windows line endings: `git config --global core.autocrlf input`, then re-clone inside WSL |

## Keep credentials safe

- Never put credentials or write tokens in the repo, in chat messages or in screenshots.
- `~/.aws/credentials` lives outside the repo, so git never sees it.
- Lab credentials expire on their own; the write tokens in the bucket do not, so revoke the `demo` token after the presentation (phase 17).

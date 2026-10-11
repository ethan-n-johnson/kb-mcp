# kb-mcp

A self-hosted knowledge base that Claude can search and write to through MCP. It is a Python FastAPI app deployed on AWS (EC2 behind an ALB and CloudFront, data in S3) as a prototype for the Cloud Computing course (AWS Academy Cloud Architecting).

- **12 MCP tools** for searching, adding, editing and uploading documents, over HTTP (`/mcp`) or stdio.
- **No token is needed for the MCP tools.** The REST API, web UI and Chrome extension still need a write token for writes.
- **One command deploys everything:** `infra/up.sh` builds the image and deploys the three stacks (`kb-network`, `kb-data`, `kb-app`). `infra/up.sh save` backs up the database before the sandbox session ends.

More detail lives in `docs/`: `SETUP.md` (full setup and troubleshooting), `PROGRESS.md` (decisions and deviations), `SECURITY_REVIEW.md`.

---

## Setup

Run everything in a bash shell: Linux, or the Ubuntu terminal (WSL2) on Windows. Steps 1 to 6 are done once per computer. Step 7 is partly once per computer and partly once per lab session.

### 1. Windows only: install WSL2 and Docker Desktop

In PowerShell as administrator:

```powershell
wsl --install -d Ubuntu
```

Reboot, open **Ubuntu** from the Start menu and create your Linux user.

For Docker, either install [Docker Desktop](https://www.docker.com/products/docker-desktop/) and turn on *Settings > Resources > WSL integration > Ubuntu* (Docker Desktop must be running whenever you deploy), or install Docker inside Ubuntu with the Linux steps in step 3.

Do all the remaining steps in the Ubuntu terminal, and keep the repo in your Linux home (`~/kb-mcp`), **not** under `/mnt/c/...`. On the Windows drive the scripts lose their executable bit and builds are slow.

### 2. Base tools

Ubuntu or Debian (including WSL):

```bash
sudo apt update
sudo apt install -y git curl unzip jq python3 python3-venv python3-pip
python3 --version   # must be 3.12 or newer
```

Fedora:

```bash
sudo dnf install -y git curl unzip jq python3 python3-pip
python3 --version   # must be 3.12 or newer
```

### 3. Docker (skip on Windows if you use Docker Desktop)

Ubuntu or Debian:

```bash
sudo apt install -y docker.io docker-buildx
sudo usermod -aG docker "$USER"
```

Fedora:

```bash
sudo dnf -y install dnf-plugins-core
sudo dnf config-manager addrepo --from-repofile=https://download.docker.com/linux/fedora/docker-ce.repo
sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
```

The new `docker` group only applies to a fresh login. On Linux, log out and back in. In WSL, logging out is not enough: run `wsl --shutdown` in PowerShell, then reopen Ubuntu. Then check:

```bash
groups                    # must list "docker"
docker run hello-world
```

Without this, `infra/up.sh` stops at the image build with `permission denied while trying to connect to the docker API at unix:///var/run/docker.sock`.

### 4. AWS CLI v2

```bash
cd ~
curl -s "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o awscliv2.zip
sudo unzip -q awscliv2.zip
sudo ./aws/install
sudo rm -rf aws awscliv2.zip
aws --version
```

### 5. Get the code

Set up an SSH key for this computer first (press Enter at every prompt), and add the printed line on GitHub under *Settings > SSH and GPG keys > New SSH key*:

```bash
ssh-keygen -t ed25519 -C "$(hostname)"
cat ~/.ssh/id_ed25519.pub
ssh -T git@github.com            # should say "Hi ethan-n-johnson!"
```

Then clone (no `sudo`, so the files belong to you):

```bash
git config --global core.autocrlf input   # keeps Windows line endings out of the scripts
git clone git@github.com:ethan-n-johnson/kb-mcp.git ~/kb-mcp
cd ~/kb-mcp
```

If a script ever fails with `Permission denied`, the executable bit was lost: `chmod +x infra/*.sh infra/scripts/*.sh`.

### 6. Python environment and tests

```bash
cd ~/kb-mcp
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'    # also installs cfn-lint, which deploy.sh needs
.venv/bin/python -m pytest -q        # fast tests, should all pass
```

The deploy scripts call `.venv/bin/cfn-lint` directly, so the venv does not need to be active for `infra/up.sh`. Activate it (`source .venv/bin/activate`) when you want to run `pytest` or other Python tools by name.

### 7. Config files

All config lives outside the repo, in `~/.aws` and `~/.config/kb-mcp`.

**Windows (WSL) only: check that `~/.aws` is a real Linux folder.** It can be a link to `C:\Users\<you>\.aws` on the Windows drive. Files there do not support Linux permissions (`chmod: Operation not permitted`), and writing to it overwrites the Windows AWS settings. Check, and replace the link with a real folder:

```bash
ls -ld ~/.aws               # bad if it shows "-> /mnt/c/..."
rm ~/.aws                   # only if it is a link; removes the link, not the Windows folder
```

**Create the files once per computer** (this makes empty ones with the right permissions):

```bash
mkdir -p -m 700 ~/.aws ~/.config/kb-mcp
printf '[default]\nregion = us-east-1\noutput = json\n' > ~/.aws/config
touch ~/.aws/credentials ~/.config/kb-mcp/up.env
chmod 600 ~/.aws/config ~/.aws/credentials ~/.config/kb-mcp/up.env
```

**Fill in `up.env`** (once per computer) with `nano ~/.config/kb-mcp/up.env`. Use your own email; do not leave the example address:

```ini
# Required: where the CloudWatch alarm emails go (confirm the SNS email once per new account)
KB_ALERT_EMAIL=you@example.com
# The only instance profile the sandbox lets EC2 use (this is also the default)
KB_INSTANCE_PROFILE=myS3Role
# Optional, see infra/deploy.sh
# KB_METRICS_MODE=
# KB_EXTENSION_ORIGIN=
```

**Fill in the credentials** (every lab session). Start the lab in AWS Academy, open **AWS Details > AWS CLI: Show**, and copy the whole `[default]` block. Then open the file, delete what is there, and paste it:

```bash
nano ~/.aws/credentials
```

It should look like this, with real values (the session token is very long):

```ini
[default]
aws_access_key_id=ASIA...
aws_secret_access_key=...
aws_session_token=...
```

In nano, right-click pastes in most terminals; Ctrl+O then Enter saves, Ctrl+X exits. Check it:

```bash
aws sts get-caller-identity       # prints the sandbox account ID
```

`InvalidClientTokenId` means the file still has placeholder or old values; `ExpiredToken` means the lab session ran out. In both cases paste a fresh block from AWS Details.

**Move your knowledge base over** (new computer or new sandbox account). `infra/up.sh` restores the database from the bucket, or from `~/.config/kb-mcp/kb-latest.db.gz` when the bucket has none. Without either, the app starts empty (`up.sh` says `no database snapshot in the bucket or saved locally`). So before the first deploy on a new computer, copy `kb-latest.db.gz` from the last computer that ran `infra/up.sh save`:

```bash
cp /path/to/kb-latest.db.gz ~/.config/kb-mcp/
```

In WSL, Windows folders are under `/mnt/c/Users/<you>/`, for example `/mnt/c/Users/<you>/Downloads/kb-latest.db.gz`.

`~/.config/kb-mcp/write-tokens` (used only by the REST API, web UI and extension) is created by `infra/up.sh` on its first run.

---

## Deploy

```bash
cd ~/kb-mcp
infra/up.sh
```

It is safe to run again at any time: unchanged stacks report "No changes to deploy". It prints the CloudFront domain at the end. The first deploy in a new account takes about 15 to 25 minutes (image build and upload, then the stacks). After every deploy, run one warm-up search before anyone else does.

Check it:

```bash
CF=https://<cloudfront-domain>
curl -s "$CF/healthz"
```

**Before the lab session ends**, always save the database. A timed-out sandbox can come back as a new, empty account:

```bash
infra/up.sh save
```

## Connect Claude Code

Install Claude Code (inside WSL on Windows):

```bash
curl -fsSL https://claude.ai/install.sh | bash
claude --version
```

Add the server. No token is needed:

```bash
claude mcp remove kb 2>/dev/null
claude mcp add --transport http kb "$CF/mcp"
```

Run this again whenever the CloudFront domain changes (a new sandbox account), not after an ordinary redeploy.

claude.ai can also use the server as a custom connector: URL `$CF/mcp`, no authentication. Whole-file uploads (`upload_file`) still need Claude Code, because the upload link is sent with `curl` from a local file.

## Run locally

```bash
.venv/bin/python -m kb.cli mcp-stdio            # stdio MCP server, logs go to stderr
```

See `CLAUDE.md` for the local web server and the environment variables.

## Security note

Anyone with the CloudFront URL can add, edit or delete documents through `/mcp`. The versioned snapshot bucket and nightly backups make recovery possible. See `docs/SECURITY_REVIEW.md`.

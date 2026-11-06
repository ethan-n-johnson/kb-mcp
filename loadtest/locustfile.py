"""Load test for the scale-out demo (phase 23).

Run from the laptop through CloudFront, never against the ALB directly:

    locust -f loadtest/locustfile.py --host "$CF" --headless -u 30 -r 2 -t 25m \
        --csv loadtest/results/run1

Read only: searches a fixed list of 20 questions and, now and then, /healthz.
No writes and no auth header. The list is hardcoded so the file is self-contained.
"""

import random

from locust import HttpUser, between, task

# 14 answerable eval questions, 3 eval paraphrases, 3 extra paraphrases.
QUESTIONS = [
    "how does an Auto Scaling group replace an unhealthy instance",
    "how do I mount EFS on Amazon Linux",
    "how do I attach an EBS volume to an EC2 instance",
    "what is the health check grace period for an Auto Scaling group",
    "how do I require IMDSv2 for instance metadata",
    "how do NAT gateways let private subnets reach the internet",
    "how do I configure health checks for an Application Load Balancer target group",
    "how do I enable versioning on an S3 bucket",
    "how do I restrict access to an Application Load Balancer so only CloudFront can reach it",
    "how do I preview CloudFormation stack changes with a change set",
    "how do I raise an HTTPException in FastAPI",
    "how do I enable CORS in FastAPI with CORSMiddleware",
    "what is the Streamable HTTP transport in MCP",
    "how does an MCP server expose tools to clients",
    "my web server died, will the fleet bring up a fresh one by itself",
    "keep older copies of files around after someone overwrites them",
    "run a script automatically the first time a virtual machine boots",
    "what happens to a private subnet instance that has no route to the internet",
    "how can I see what a CloudFormation update is going to change before applying it",
    "which FastAPI setting lets a browser page on another origin call my API",
]
assert len(QUESTIONS) == 20


class SearchUser(HttpUser):
    wait_time = between(1, 3)

    @task(19)
    def search(self):
        q = random.choice(QUESTIONS)
        # name= groups every question into one row of the statistics.
        # Non-200 responses count as failures (Locust's default).
        self.client.get("/api/search", params={"q": q}, name="/api/search")

    @task(1)
    def healthz(self):
        self.client.get("/healthz", name="/healthz")

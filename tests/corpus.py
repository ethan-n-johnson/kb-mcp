"""Small AWS-flavoured corpus shared by the fast search tests and the `models` tests.

Every section is longer than 50 words, so each heading section is its own
chunk. Heading paths therefore look like `Amazon EBS volumes > Attach a volume > Linux`.
"""

from __future__ import annotations

EBS = """# Amazon EBS volumes

Amazon Elastic Block Store provides block storage volumes for use with EC2 instances. An EBS volume behaves like a raw, unformatted block device that you can mount as a drive. Volumes persist independently from the life of the instance, and you pay for the provisioned size each month whether or not the volume is attached to anything.

## Attach a volume

### Linux

To attach an EBS volume to a Linux instance, open the console, choose the volume, then Actions and Attach volume. Pick the instance, which must be in the same Availability Zone as the volume. After the attachment completes, connect to the instance, create a file system with mkfs, and mount the new device with the mount command.

### Windows

To attach an EBS volume to a Windows instance, choose the volume in the console, then Actions and Attach volume, and select the instance. Afterwards use Disk Management in the remote desktop session to bring the new disk online, initialize it, and create a simple volume with a drive letter.

## Snapshots

An EBS snapshot is a point in time copy of a volume stored incrementally in S3. Only blocks that changed since the previous snapshot are saved, which keeps the cost low. You can restore a snapshot to a brand new volume in any Availability Zone of the same Region, and you can copy snapshots to other Regions for disaster recovery.
"""

S3 = """# Amazon S3 storage classes

Amazon S3 stores objects in buckets and offers several storage classes that trade retrieval speed against price. S3 Standard suits frequently accessed data, while the infrequent access and archive classes cost less per gigabyte but charge for retrieval. Every class is designed for eleven nines of durability across multiple Availability Zones.

## Lifecycle rules

A lifecycle rule moves objects between storage classes or deletes them after a number of days. For example a rule can transition logs to S3 Glacier after thirty days and expire them after a year. Rules apply to a whole bucket or to a prefix or tag filter, and they run automatically once a day without any servers of your own.

## Versioning

When versioning is enabled on a bucket S3 keeps every version of an object. Deleting an object only adds a delete marker, so earlier versions can be restored. Versioning protects against accidental overwrites and deletions, but every stored version is billed, so combine it with a lifecycle rule that removes noncurrent versions.
"""

ELB = """# Elastic Load Balancing

Elastic Load Balancing distributes incoming application traffic across multiple targets such as EC2 instances, containers and IP addresses in one or more Availability Zones. The Application Load Balancer works at layer seven and routes requests by path or host name, while the Network Load Balancer works at layer four for extreme performance.

## Health checks

A target group health check sends a request to each registered target at a regular interval. A target that fails several checks in a row is marked unhealthy and stops receiving traffic until it passes again. Configure the health check path, the success codes and the thresholds so a slow starting instance is not removed too early.

## Cross-zone load balancing

With cross-zone load balancing enabled each load balancer node distributes traffic across the registered targets in all enabled Availability Zones. Without it each node only sends traffic to targets in its own zone, which can overload a zone that has fewer instances. It is always on for Application Load Balancers.
"""

VPC = """# Amazon VPC networking

A virtual private cloud is a logically isolated section of the AWS cloud where you launch resources in a network that you define. You choose the IP address range, create subnets, and configure route tables and gateways. Every account has a default VPC in each Region, but production workloads normally use a custom VPC.

## Subnets and route tables

A subnet is a range of IP addresses in one Availability Zone. A public subnet has a route to an internet gateway, while a private subnet does not. Each subnet is associated with a route table that decides where network traffic is directed, and the local route that covers the VPC range cannot be removed.

## NAT gateway

A NAT gateway lets instances in a private subnet start outbound connections to the internet, for example to download patches, while preventing the internet from starting connections to them. Create the NAT gateway in a public subnet and add a route for the default destination in the private route table. A NAT gateway is billed per hour and per gigabyte processed.
"""

ERRORS = """# Troubleshooting codes

## Launch failures

The RunInstances call can fail with the error code InsufficientInstanceCapacity when AWS does not currently have enough capacity for the instance type you requested in that Availability Zone. Retry later, choose a different instance type, or launch into another Availability Zone. The code VcpuLimitExceeded means the account has reached its vCPU quota for the Region.
"""

DOCS: dict[str, tuple[str, list[str]]] = {
    "ebs": (EBS, ["storage", "ec2"]),
    "s3": (S3, ["storage"]),
    "elb": (ELB, ["networking"]),
    "vpc": (VPC, ["networking", "security"]),
    "errors": (ERRORS, ["ec2"]),
}

# Queries with one clearly right answer: (query, doc key, heading_path suffix).
QUERIES = {
    "ebs_linux": ("How do I attach an EBS volume to an EC2 instance on Linux?", "ebs", "Linux"),
    "s3_lifecycle": ("Which S3 feature moves old objects to Glacier automatically?", "s3", "Lifecycle rules"),
    "elb_health": ("What happens when a target fails its health check?", "elb", "Health checks"),
    "nat": ("How can private subnet instances reach the internet for patches?", "vpc", "NAT gateway"),
}

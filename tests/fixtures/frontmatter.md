---
title: VPC Peering Notes
tags:
  - vpc
  - networking
---

# VPC Peering Notes

A VPC peering connection is a networking connection between two VPCs that lets you route traffic between them privately using private IPv4 or IPv6 addresses. Instances in either VPC can communicate as if they were in the same network, and peering works across accounts and Regions.

## Route tables

After the peering connection is active you must add a route to each VPC route table that sends traffic for the other VPC CIDR block to the peering connection. Without these routes the connection exists but no packets flow, which is the most common mistake in the labs.

```bash
# not a heading
aws ec2 create-route --route-table-id rtb-0123 \
    --destination-cidr-block 10.1.0.0/16 \
    --vpc-peering-connection-id pcx-0123
```

The command above adds the return route. Run the same command from the accepter side with the requester CIDR block so that replies can find their way back across the connection.

## Limits

Peering is not transitive, and the two VPCs must not have overlapping CIDR blocks. A transit gateway is the usual answer when many VPCs must talk to each other, because it avoids a full mesh of peering connections and keeps route management in one place for the whole team to review.

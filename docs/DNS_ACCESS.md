# asg-insights — private DNS and browser access

By default the dashboard is reached via `aws ssm start-session` port-forward
to `http://127.0.0.1:8080/`. That's fine for a small ops team but painful
if you want leadership or a broader audience to just visit a URL.

This doc shows how to attach a **Route 53 private hosted zone** so the
dashboard is reachable at a friendly hostname
(`https://scaling-insights.internal.<your-domain>/`) from any client that
can resolve into your VPC.

The CloudFormation template already supports this out of the box —
you just pass two extra parameters at deploy time.

---

## 1. What "private hosted zone" gives you

A Route 53 **private hosted zone** is a DNS zone that resolves *only* for
clients inside associated VPCs. Public DNS never sees it. Perfect for an
internal dashboard.

Two pieces need to exist:

1. **The zone itself** — e.g. `internal.example.com`, associated with your
   VPC. Usually already exists in a customer environment; if not, Step 2
   below creates one.
2. **An A record inside that zone** — e.g. `scaling-insights.internal.example.com`
   → the EC2 private IP. The stack creates this for you.

Whoever visits `https://scaling-insights.internal.example.com/` still needs
network reachability to the VPC — see Section 4 for the options.

---

## 2. Create or reuse a private hosted zone

Check whether one already exists:

```bash
aws route53 list-hosted-zones-by-vpc --vpc-id "$VPC_ID" --vpc-region "$AWS_REGION"
```

If nothing suitable is returned, create one:

```bash
aws route53 create-hosted-zone \
  --name internal.example.com \
  --vpc VPCRegion="$AWS_REGION",VPCId="$VPC_ID" \
  --hosted-zone-config PrivateZone=true \
  --caller-reference "asg-insights-$(date +%s)"
```

Substitute `internal.example.com` with whatever internal DNS suffix your
team uses (e.g. `corp.example.com`, `svc.internal`).

Note the returned **HostedZoneId** — you'll pass it to the deploy in the
next step.

---

## 3. Deploy the stack with DNS parameters

The template exposes two parameters:

| Parameter | Purpose | Example |
|---|---|---|
| `DnsHostedZoneId` | Route 53 hosted zone ID (private or public) | `Z0123456ABCDEFGH` |
| `DnsRecordName`   | FQDN for the dashboard | `scaling-insights.internal.example.com` |

When both are set, the stack creates an A record pointing at the EC2
instance's private IP with a 300s TTL.

Pass them either directly to `deploy.sh` (edit the file to add the
parameter overrides) or pass a full `cloudformation deploy` command:

```bash
aws cloudformation deploy \
  --stack-name "$STACK" \
  --template-file infra/cloudformation.yaml \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides \
      "VpcId=$VPC_ID" \
      "SubnetId=$SUBNET_ID" \
      "AllowedCidr=$ALLOWED_CIDR" \
      "InstanceType=t3.medium" \
      "SourceBucketName=$BUCKET" \
      "DnsHostedZoneId=Z0123456ABCDEFGH" \
      "DnsRecordName=scaling-insights.internal.example.com"
```

Or, easier — inline the two parameters as env-var overrides:

```bash
DNS_ZONE_ID=Z0123456ABCDEFGH \
DNS_RECORD=scaling-insights.internal.example.com \
  ./scripts/deploy.sh
```

(If `deploy.sh` doesn't already thread those two through, add them to the
`--parameter-overrides` block near the bottom of the script — one line each.)

After the stack finishes, check the record:

```bash
aws route53 list-resource-record-sets \
  --hosted-zone-id "$DNS_ZONE_ID" \
  --query "ResourceRecordSets[?Name=='$DNS_RECORD.']"
```

---

## 4. Making clients able to *reach* the private hostname

The DNS record now exists, but a client at your desk can't resolve
`scaling-insights.internal.example.com` yet. Pick one of these three
patterns — they're roughly in order of implementation cost.

### Option A — Client is on a VPN into the VPC

Most common. If your team already uses AWS Client VPN, Direct Connect, or a
Site-to-Site VPN back to the VPC, they can resolve the private zone as long
as their DNS is set to the VPC's Route 53 Resolver (usually `+2` of the VPC
CIDR base, e.g. `172.31.0.2` for `172.31.0.0/16`).

Verification from a VPN'd client:

```bash
dig scaling-insights.internal.example.com
# Should return the EC2 private IP
```

Nothing else to do — open `https://scaling-insights.internal.example.com/`
in the browser.

### Option B — Route 53 Resolver Inbound endpoint + corporate DNS forward

For clients on your corporate LAN (no VPN), stand up a **Route 53 Resolver
inbound endpoint** in the VPC and forward the internal DNS suffix from the
corporate DNS server to the resolver's endpoint IPs.

One-time setup:

```bash
# 1. Security group for the resolver endpoint (DNS from the corporate CIDR)
RES_SG=$(aws ec2 create-security-group \
  --group-name asg-insights-resolver \
  --description "R53 Resolver inbound" \
  --vpc-id "$VPC_ID" --query 'GroupId' --output text)
aws ec2 authorize-security-group-ingress \
  --group-id "$RES_SG" \
  --ip-permissions '[
    {"IpProtocol":"udp","FromPort":53,"ToPort":53,"IpRanges":[{"CidrIp":"10.0.0.0/8"}]},
    {"IpProtocol":"tcp","FromPort":53,"ToPort":53,"IpRanges":[{"CidrIp":"10.0.0.0/8"}]}
  ]'

# 2. Resolver endpoint in two subnets (different AZs for HA)
aws route53resolver create-resolver-endpoint \
  --creator-request-id "asg-insights-$(date +%s)" \
  --security-group-ids "$RES_SG" \
  --direction INBOUND \
  --ip-addresses "SubnetId=subnet-aaaa" "SubnetId=subnet-bbbb"
```

Grab the two endpoint IPs it returns. Then in your corporate DNS server,
add a **conditional forwarder** for `internal.example.com` pointing at
those two IPs. Clients on the LAN now resolve the private zone.

Cost: ~$0.125/hour per endpoint × 2 = ~$180/month. Overkill for one dashboard;
worth it if you're standing up many internal services.

### Option C — Public ALB with SSO in front

Different pattern: skip private DNS, put the dashboard behind an internet-facing
ALB with OIDC/Cognito auth. Requires: an ACM cert, a public hosted zone
subdomain, an ALB listener rule, and either Cognito or an OIDC IdP.

Out of scope for this doc — the pattern is standard AWS. Reach for it if
you want dashboard access from anywhere without a VPN, and are OK with the
extra ~$25/month for the ALB.

---

## 5. Enabling private DNS support on the VPC (rare gotcha)

If DNS in the VPC just doesn't resolve, verify the VPC has both
`enableDnsSupport` and `enableDnsHostnames` on:

```bash
aws ec2 describe-vpc-attribute --vpc-id "$VPC_ID" --attribute enableDnsSupport
aws ec2 describe-vpc-attribute --vpc-id "$VPC_ID" --attribute enableDnsHostnames

# fix if either is false
aws ec2 modify-vpc-attribute --vpc-id "$VPC_ID" --enable-dns-support   '{"Value":true}'
aws ec2 modify-vpc-attribute --vpc-id "$VPC_ID" --enable-dns-hostnames '{"Value":true}'
```

Default VPCs have these on already; custom VPCs sometimes don't.

---

## 6. Also — put nginx on TLS

Once you have a hostname, terminate TLS on the instance so leadership isn't
staring at "Not secure" in the address bar. Two options:

### 6a. AWS Certificate Manager Private CA (customer has ACM PCA)

Issue a private cert for `scaling-insights.internal.example.com`, export it,
install into `/etc/ssl/private/`, and update the nginx site config:

```nginx
server {
  listen 443 ssl http2;
  server_name scaling-insights.internal.example.com;
  ssl_certificate     /etc/ssl/certs/asg-insights.crt;
  ssl_certificate_key /etc/ssl/private/asg-insights.key;
  location / { proxy_pass http://127.0.0.1:8000; ... }
}
server {
  listen 80;
  return 301 https://$host$request_uri;
}
```

Rotate certs with a `renew.sh` script + monthly systemd timer.

### 6b. Self-signed for a quick internal demo

Not recommended long term, but fine for a proof-of-concept:

```bash
sudo openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout /etc/ssl/private/asg-insights.key \
  -out    /etc/ssl/certs/asg-insights.crt \
  -subj "/CN=scaling-insights.internal.example.com"
```

Ship the cert to admin machines' trust stores or accept the browser warning.

---

## 7. Removing the DNS record

The A record is stack-managed. To drop it, redeploy without the two
parameters:

```bash
./scripts/deploy.sh   # with DNS_ZONE_ID and DNS_RECORD unset
```

To delete the private hosted zone itself (irreversible for existing records):

```bash
aws route53 delete-hosted-zone --id "$DNS_ZONE_ID"
```

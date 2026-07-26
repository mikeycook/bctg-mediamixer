# Server 3 — EC2 Configuration Guide

Setup steps for the media pipeline server. Every command here is for you to
run; none of it has been executed, and nothing in this document changes
server 1 or server 2.

Companion to `09-repo-implementation-plan.md`, which covers the software.

---

## 1. What you are building

| | Purpose | Notes |
|---|---|---|
| Server 3 (EC2) | Inventory sync, content library API, later the render worker | `c7i.large`, Ubuntu 24.04 x86_64, `us-east-1c`, existing VPC |
| Media database | `content_library_*` tables | RDS `db.t4g.micro` in `us-east-1c`, or on-instance — still open |
| Instance role | S3 access without stored credentials | New — see §8 |
| Security group | SSH from you; API from server 2 only | New — see §7 |

Server 3 never connects to the golden database on server 2. Traffic between
them goes one direction only: server 2's admin backend proxies API requests
in (§8 of the implementation plan).

---

## 2. Choose the region first

This is the only decision here that cannot be undone later. Instance types,
volume sizes, and security groups are all changeable; an EC2 instance or RDS
database cannot be moved between regions without rebuilding it.

Check where the clips bucket lives:

```bash
aws s3api get-bucket-location --bucket big-city-travel-guide-clips
```

(An empty `LocationConstraint` in the response means `us-east-1`.)

**If the bucket and your existing servers are in different regions, put
server 3 with the bucket.** This is a media pipeline — almost everything it
does is S3 traffic. It reads all 73 objects to checksum them, streams every
source through `ffprobe`, and during rendering downloads sources repeatedly
and uploads outputs. In-region means lower latency and no cross-region
transfer charges. The only server 2 ↔ server 3 traffic is small JSON API
calls through the proxy, which is largely indifferent to distance.

The cost of splitting regions is networking: security groups cannot be
referenced by id across regions, so the inbound rule in §7 becomes VPC
peering or CIDR-based rules instead.

**Resolved for this project:** servers 1 and 2, the clips bucket, and
server 3 are all in `us-east-1`, and server 3 joins the existing VPC. That
keeps S3 traffic in-region and lets the security group in §7 reference
server 2's group by id, with no VPC peering or CIDR workarounds.

### Choosing the availability zone

Pick the AZ deliberately rather than accepting the wizard's default. Like
region, it cannot be changed later — moving an instance between AZs means
snapshotting its volumes and relaunching.

Two things depend on it:

**Instance-type availability varies by AZ, not just by region.** `us-east-1`
has six zones and the newer families are not in all of them; `us-east-1e` is
the oldest and is missing many modern types. The launch wizard filters the
instance menu to what the chosen subnet's AZ offers, so a compute-optimized
type can appear absent when the region carries it perfectly well.

**Cross-AZ traffic is billable.** If the media database is RDS, put it in
the same AZ as server 3 so routine queries do not cross a zone boundary.

Find the zones that offer the type you will eventually want for rendering:

```bash
aws ec2 describe-instance-type-offerings \
  --location-type availability-zone \
  --filters Name=instance-type,Values=c7i.large \
  --query 'InstanceTypeOfferings[].Location' --output text
```

Then map your existing subnets to their zones and choose one that carries
both the launch type and the eventual render type:

```bash
aws ec2 describe-subnets --filters Name=vpc-id,Values=<your-vpc-id> \
  --query 'Subnets[].{Subnet:SubnetId,AZ:AvailabilityZone,CIDR:CidrBlock}' \
  --output table
```

If no subnet exists in the target AZ, create one in the existing VPC with a
non-overlapping CIDR block. That is an additive change and does not affect
server 2 or its subnet.

### Chosen: server 3 in us-east-1c, server 2 stays in us-east-1e

`us-east-1e` is the oldest zone in the region and lacks many current
instance families, which is why compute-optimized types appear unavailable
when launching into it. Server 3 goes in a different AZ, and that is fine —
preferable, in fact.

A VPC spans every zone in its region. Instances in different AZs of the same
VPC communicate over private IPs with no extra configuration, and security
groups reference each other by id across zones exactly as §7 describes. Only
a restrictive network ACL would interfere, and the default NACL permits all
traffic.

Cost and performance are unaffected in any way that matters:

- **S3 is regional.** All the high-volume traffic — checksum reads, `ffprobe`
  streams, render source downloads, export uploads — is S3-to-EC2 within
  `us-east-1`, which is free and indifferent to zone.
- **Cross-AZ traffic here is only the proxied API calls** between server 2
  and server 3: small JSON payloads, billed per GB each way, amounting to
  pennies per month.
- **Keep the database with server 3.** If the media database is RDS, place
  it in server 3's zone, since server 3 queries it constantly and server 2
  never touches it.

A secondary benefit: an incident confined to `us-east-1e` leaves server 3
running.

---

## 3. Discover your current configuration first

Before launching anything, read the settings off an existing instance so the
new one matches. Replace `<server-2-instance-id>` with the real id — find it
with `aws ec2 describe-instances --query 'Reservations[].Instances[].[InstanceId,Tags[?Key==\`Name\`].Value|[0]]' --output table`.

```bash
aws ec2 describe-instances \
  --instance-ids <server-2-instance-id> \
  --query 'Reservations[].Instances[].{
      Type:InstanceType,
      AMI:ImageId,
      Key:KeyName,
      AZ:Placement.AvailabilityZone,
      Subnet:SubnetId,
      VPC:VpcId,
      Profile:IamInstanceProfile.Arn,
      SGs:SecurityGroups[].GroupId
  }' --output json
```

That single call gives you everything worth copying: the AMI, the key pair
name, the VPC and subnet, and the security groups. Note the **region** you
ran it in — nearly everything below is region-scoped.

To see the exact rules on the existing security group, so you can model the
new one rather than reusing it:

```bash
aws ec2 describe-security-groups --group-ids <sg-id-from-above> --output json
```

---

## 4. Reusing your existing `.pem`

This is straightforward as long as you stay in the same region.

**Same region as your other instances (the normal case).** EC2 key pairs are
region-scoped, so the key pair your existing servers use is already there.
At launch, pick that same key pair name from the dropdown — or pass
`--key-name <existing-key-name>` to the CLI. Your current `.pem` file then
works for server 3 with no further steps, and `deliver_dbs.sh`-style `scp`
calls work with the same `-i` argument.

Confirm the name exists:

```bash
aws ec2 describe-key-pairs --query 'KeyPairs[].KeyName' --output table
```

**Different region (only if you have a reason).** Key pairs do not replicate
across regions, so you import the same public key under the same name.
Derive the public half from your existing private key — this reads your
`.pem` locally and does not transmit it:

```bash
ssh-keygen -y -f /path/to/your-key.pem > /tmp/your-key.pub

aws ec2 import-key-pair \
  --region <new-region> \
  --key-name <same-name-as-before> \
  --public-key-material fileb:///tmp/your-key.pub

rm /tmp/your-key.pub
```

The same `.pem` then authenticates in both regions.

A note on staying in one region: RDS, security-group references, and private
VPC traffic all get simpler, and you avoid cross-region data transfer
charges. Put server 3 in the **same VPC as server 2**, and if you use RDS,
the **same availability zone** as the database to avoid cross-AZ transfer
costs.

---

## 5. AMI

**Ubuntu Server 24.04 LTS, x86_64.** A mild preference over 26.04, not a
strong one.

24.04 ships Python 3.12; 26.04 LTS ships Python 3.14. That difference is
close to irrelevant here — server 3 runs a new codebase in its own
virtualenv, and Ubuntu now publishes an official Python PPA with backports
for LTS releases, so neither choice locks you to its default interpreter.

The actual case for 24.04 is maturity. It has two years of production use
and is well past its `.1` point release, while 26.04 is only a few months
old. The stack has two compiled dependencies — `psycopg2` and
`pydantic-core` — where a very new Python version can mean waiting on wheels
or falling back to a source build. Support through 2029 is more runway than
this project needs.

26.04 is defensible if the support window to 2031 matters more to you than
avoiding occasional packaging friction. Neither choice will hurt you.

Ubuntu over Amazon Linux 2023 for a practical reason: `ffmpeg` is a single
`apt install` on Ubuntu, while AL2023 does not carry it in the default repos.
Your existing servers are Ubuntu regardless — `deliver_dbs.sh` defaults the
SSH user to `ubuntu`.

Look up the current AMI id rather than copying one from documentation; ids
are region-specific and change with each image release. Canonical publishes
them as SSM public parameters:

```bash
aws ssm get-parameter \
  --name /aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id \
  --query 'Parameter.Value' --output text
```

That returns the correct AMI for whichever region you run it in. Substitute
`arm64` for `amd64` if you take the Graviton path in §6.

---

## 6. Instance type

**Chosen: `c7i.large` in `us-east-1c`.** Compute-optimized from the start,
which skips the later instance-type change entirely.

The inventory phase does not need this much machine — it is network-bound,
listing S3, running `ffprobe` against presigned URLs, and streaming roughly
658 MiB through a SHA-256 hash. Going straight to `c7i.large` costs roughly
$30/month more than a `t2.medium` while the box is largely idle, in exchange
for never migrating and never reasoning about burst credits. If this size
holds, a one-year Savings Plan or Reserved Instance cuts the instance cost
by roughly a third.

**Do not render on a t-family instance** if you ever reconsider. T2 and T3
are burstable with a baseline near 40% CPU, and `ffmpeg` pins every core at
100% for the length of an encode — credits would be exhausted within the
first half hour of rendering, after which everything is throttled to
baseline with no obvious signal explaining the slowdown.

### Architecture determines how easy that upgrade is

Changing instance type **within** an architecture takes about two minutes:
stop, change type, start. Same volumes, same everything. Changing **across**
architectures does not work that way — an x86 instance cannot become a
Graviton one, because Graviton needs an arm64 AMI and therefore a rebuild.

Pick one path and stay on it:

| Path | Now | For rendering | Trade-off |
|---|---|---|---|
| **x86_64** (recommended) | `t2.medium` | `c7i.large` — simple type change | Consistent with your other servers; roughly $10–15/month more than Graviton |
| **arm64** | `t4g.medium` | `c7g.large` — simple type change | Cheaper, and t4g is a better-behaved burstable generation than t2; but server 3 becomes the only ARM box in the fleet |

Everything the pipeline needs runs on either — `boto3`, `psycopg2`, FastAPI,
and Ubuntu's `ffmpeg` package all support arm64. The recommendation is x86
purely because fleet consistency is worth more than the saving at this scale.

**Expect Graviton types to be missing from the launch list.** On an amd64
AMI the console filters the instance-type menu to compatible architectures,
so `c7g.large` and other `*g` types simply will not appear. That is the
architecture split doing its job, not a quota or availability problem. On
the x86 path the compute-optimized target is `c7i.large`, with `c6i.large`
as a more widely available fallback.

Instance-type availability varies by availability zone, not only by region.
Confirm before committing:

```bash
aws ec2 describe-instance-type-offerings \
  --location-type availability-zone \
  --filters Name=location,Values=<your-az> \
            Name=instance-type,Values=c7i.large,c6i.large,t2.medium \
  --query 'InstanceTypeOfferings[].InstanceType' --output table
```

### Storage

- **Root volume**: match your existing servers; 30 GB gp3 is ample for OS,
  Python, and `ffmpeg`.
- **Scratch volume (before rendering, not now)**: a **separate EBS** volume
  mounted at `/opt/mediamixer/scratch`. Keep it off the root volume, and off the
  database volume if you run PostgreSQL locally. Encoding intermediates are
  routinely far larger than the compressed sources they come from, so a
  render that fills the disk must not be able to take down anything else.
  Size it for your largest expected render times a healthy safety factor.
- **File systems: leave as None.** That section attaches EFS or FSx —
  shared network filesystems for multiple instances. Server 3 is a single
  box, and EFS is both slower than local EBS for the large sequential I/O
  `ffmpeg` performs and more expensive per GB. The scratch volume above is
  EBS, not EFS.

---

## 7. Security group

Create a **new** security group rather than reusing server 2's — the rules
are different. Model it on what you saw in §3.

**Inbound**

| Port | Source | Purpose |
|---|---|---|
| 22 | Your IP, or your existing SSH source | Administration |
| 8000 (or your API port) | **Server 2's security group ID**, not a CIDR | The proxied content library API |

Referencing server 2's security group by id rather than by IP address means
the rule keeps working if server 2's address ever changes.

**Outbound**: default (all traffic) is fine. Server 3 needs to reach S3, and
your AI provider later if enrichment is enabled.

**Do not** open the API port to `0.0.0.0/0`. The content library API is
reached only through server 2's proxy, and — as noted in the implementation
plan — your admin backend has no inbound authentication today. An
internet-facing port here would put an unauthenticated API in front of the
media database.

**Do not** open 5432 to anything broad. If you run PostgreSQL on the
instance, it should listen on localhost only. If you use RDS, see §9.

---

## 8. IAM instance role

This is the piece with no equivalent on your existing servers, and the most
important one to get right. It replaces stored AWS credentials entirely —
`boto3` picks the role up automatically, which is why the existing
`_clip_s3()` in `main.py` already works without any keys.

This must be a **new** role, not an existing one. Roles are attached
per-instance, so adding these permissions to a role another server already
uses would grant them to that server too.

Two objects are involved: a **policy** (the permissions) and a **role** (the
identity EC2 assumes, carrying that policy).

### Creating it — console

1. IAM → **Policies** → Create policy
2. **JSON** tab; replace the contents with `iam/media-pipeline-s3-policy.json`
3. Next → name `MediaPipelineS3Access` → Create policy
4. IAM → **Roles** → Create role
5. Trusted entity **AWS service**, use case **EC2** → Next
6. Select `MediaPipelineS3Access` → Next
7. Name `MediaPipelineServer3Role` → Create role

Creating an EC2 role through the console also creates the matching instance
profile automatically.

### Creating it — CLI

```bash
cd docs/ai-media-generation/iam
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)

aws iam create-policy \
  --policy-name MediaPipelineS3Access \
  --policy-document file://media-pipeline-s3-policy.json

aws iam create-role --role-name MediaPipelineServer3Role \
  --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}'

aws iam attach-role-policy --role-name MediaPipelineServer3Role \
  --policy-arn "arn:aws:iam::${ACCOUNT}:policy/MediaPipelineS3Access"

# The console does these two steps for you; the CLI does not.
aws iam create-instance-profile --instance-profile-name MediaPipelineServer3Profile
aws iam add-role-to-instance-profile \
  --instance-profile-name MediaPipelineServer3Profile \
  --role-name MediaPipelineServer3Role
```

### Attaching it

**IAM instance profile** lives under **Advanced details** in the launch
wizard — a collapsible section near the bottom, collapsed by default. Or
launch without it and attach afterward via **Actions → Security → Modify IAM
role**, which takes effect immediately with no restart.

Either way the role must be attached before the §12 verification, which is
what confirms the policy actually works.

### The policy

Stored at `iam/media-pipeline-s3-policy.json` in this package, reproduced
here. Substitute the bucket name if it ever differs.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ListOnlyUgcAssets",
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::big-city-travel-guide-clips",
      "Condition": {
        "StringLike": {
          "s3:prefix": ["ugc-assets/", "ugc-assets/*"]
        }
      }
    },
    {
      "Sid": "ReadSourceAssets",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:GetObjectVersion"],
      "Resource": "arn:aws:s3:::big-city-travel-guide-clips/ugc-assets/*"
    },
    {
      "Sid": "WriteOnlyToExported",
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:AbortMultipartUpload"],
      "Resource": "arn:aws:s3:::big-city-travel-guide-clips/ugc-assets/exported/*"
    },
    {
      "Sid": "NeverDeleteAnything",
      "Effect": "Deny",
      "Action": ["s3:DeleteObject", "s3:DeleteObjectVersion"],
      "Resource": "arn:aws:s3:::big-city-travel-guide-clips/*"
    },
    {
      "Sid": "NeverOverwriteSourceMasters",
      "Effect": "Deny",
      "Action": ["s3:PutObject", "s3:PutObjectAcl"],
      "Resource": [
        "arn:aws:s3:::big-city-travel-guide-clips/ugc-assets/app/*",
        "arn:aws:s3:::big-city-travel-guide-clips/ugc-assets/b-roll/*",
        "arn:aws:s3:::big-city-travel-guide-clips/ugc-assets/reactions/*",
        "arn:aws:s3:::big-city-travel-guide-clips/ugc-assets/music/*",
        "arn:aws:s3:::big-city-travel-guide-clips/ugc-assets/captions/*"
      ]
    }
  ]
}
```

The two `Deny` statements are deliberate redundancy. Nothing in the `Allow`
statements grants deletes or writes outside `exported/` anyway, but an
explicit deny always wins in IAM — so a future policy edit, or a broader
managed policy attached by accident, still cannot delete or overwrite an
irreplaceable master.

`s3:AbortMultipartUpload` is scoped to `exported/` because boto3 switches to
multipart uploads automatically for objects above roughly 8 MB, which
rendered videos will exceed. Without it, a failed upload leaves an
incomplete multipart in the bucket accruing storage charges with no way to
clean it up.

While you are in the console, confirm **bucket versioning is enabled** on
`big-city-travel-guide-clips`. It is the last line of defense against an
accidental overwrite, and `02-canonical-s3-layout.md` assumes it.

---

## 9. Database

### If RDS (recommended)

RDS is a separate service — reach it by searching **RDS** in the console,
not from the EC2 instance's configuration. The two are joined by networking,
not by a parent-child relationship. The console is labelled **"Aurora and
RDS"**, which is expected: Aurora is an engine family within RDS and the two
share one console.

At the engine step choose **PostgreSQL**, *not* **Aurora PostgreSQL-Compatible
Edition**. They appear side by side and Aurora is typically listed first.
Aurora's smallest provisioned class is `db.t4g.medium` — there is no micro —
so it starts at roughly five times the cost, plus separate I/O charges, to
hold a table of 73 rows. It is engineered for scale this project does not
have.

Choose **Full Configuration**, not Express Configuration. (These buttons have
also appeared as "Standard create" and "Easy create" — AWS renames them
periodically; the distinction is the same.) Express applies recommended
defaults and hides the availability zone, public access, initial database
name, backup retention, and deletion protection, and steers toward a larger
instance class than `db.t4g.micro`.

| Wizard section | Value |
|---|---|
| Engine | PostgreSQL — latest **16.x** offered (see note below) |
| Templates | **Dev/Test** — Production defaults to Multi-AZ and doubles the cost |
| Availability | **Single-AZ DB instance** — see note below |
| Settings | Instance identifier `mediamixerdb`; credential management **Self managed**; master username **`bigcity`** (see note); record the password immediately, it is shown once |
| Instance configuration | Select **Burstable classes (includes t classes)** first — the default *Standard classes* filter shows only `db.m*`, which start many times higher — then `db.t4g.micro` (fallback `db.t3.micro`) |
| Storage | **gp3**, **20 GiB** (the minimum — override the much larger default); autoscaling enabled with a 50–100 GB maximum |
| Connectivity | **Connect to an EC2 compute resource** → server 3 (see below) |
| Database authentication | **Password authentication** — IAM database auth needs per-connection token generation that exists nowhere in this stack; enableable later |
| Public access | **No** |
| Availability Zone | `us-east-1c`, matching server 3 |
| Additional configuration | **Initial database name** — expand and set it (see below) |
| Backups | Retention 7 days or more |
| Deletion protection | Enabled |

**20 GB costs nothing in performance — because gp3.** gp3 provides 3,000
baseline IOPS and 125 MiB/s at any volume size up to 400 GB, so a 20 GB
volume performs identically to a 200 GB one. The habit of over-allocating
storage comes from gp2, where IOPS scaled at 3 per GB and a 20 GB volume got
only 60 baseline IOPS. gp3 decoupled capacity from speed. Confirm the
storage type is gp3, and override the console's much larger default: 20 GB
is roughly $2/month against roughly $23 for 200 GB, which would exceed the
instance cost.

Leave **storage autoscaling enabled with a 50–100 GB maximum**. This
database should never approach 20 GB, so it should never trigger, but
autoscaling averts an outage if something unexpectedly fills the disk while
the ceiling bounds the cost. RDS storage can only grow, never shrink, which
is precisely why the maximum matters.

**`micro` over `small`.** 1 GB versus 2 GB, roughly $12 versus $24 monthly.
The dataset — a few thousand metadata rows plus JSONB probe output — fits in
shared buffers many times over, no planned query is memory-intensive, and
connections are bounded by one small FastAPI pool plus the sync job. 1 GB is
admittedly tight for PostgreSQL in general, but that caution addresses
workloads far larger than this. Move to `small` if CloudWatch shows
`FreeableMemory` trending low, swap climbing, or connection failures under
normal use; it is a modify with a few minutes of downtime and no data
impact.

**Graviton is fine for RDS even though EC2 is x86.** The architecture
argument in §6 applies to EC2 because your code and its compiled
dependencies execute there. On RDS, AWS supplies and manages the PostgreSQL
binaries and you only connect over the network, so the underlying CPU is
invisible. `db.t4g.micro` is simply cheaper than the equivalent `t3`.

**Master username must be `bigcity`.** Every schema file in this project
ends with `ALTER TABLE ... OWNER TO bigcity` — once in
`create_content_library_table.sql` and eight more times in the v2 migration.
A differently named master role makes all of those fail, requiring either a
rewrite of the DDL or creating a `bigcity` role by hand afterward.

**Self managed credentials, not Secrets Manager.** The entire existing
stack reads a plain `DATABASE_URL` from an environment file — the backend's
`app/db.py` and every operational script. Secrets Manager would introduce
credential-fetching code that exists nowhere else, plus IAM permissions and
a per-secret charge, to protect a database with no internet exposure. It is
a sensible later upgrade if automatic rotation becomes worthwhile.

**Password character set.** A self-chosen password is fine. RDS accepts
printable ASCII except `/`, `"`, `'`, `@`, and space. The additional
constraint is that the value is embedded in a URL
(`postgresql://bigcity:PASSWORD@endpoint:5432/mediamixer`), so also avoid
`#` and `?`, which terminate the authority section, and `$`, `%`, `\`, and
backtick, which shells and systemd may interpret.

Letters, digits, and `! - _ . ~ + ,` are all safe — `!` is a sub-delimiter
under RFC 3986 and legal unencoded in userinfo. `parse_database_url()` also
calls `unquote()` on the password, so percent-encoding is available if
needed.

One caveat with `!`: interactive bash and zsh perform history expansion on
it inside **double** quotes. Use single quotes when pasting a password at a
prompt. This does not affect the systemd EnvironmentFile, which involves no
shell, nor `psql "$DATABASE_URL"`, where the variable is already expanded.

To generate one instead:

```bash
openssl rand -base64 48 | tr -d '/+=' | head -c 40
```

**Engine version.** Server 2 runs PostgreSQL 16.14. Match the **major**
version — 16 — but select the **latest 16.x** RDS offers rather than pinning
to 16.14. Minor releases within a major are bug and security fixes only, and
a 16.14 dump restores cleanly into any later 16.x. The `-R2` suffix denotes
an RDS build revision of the same PostgreSQL version; take the highest
offered.

Major version 16 rather than 17 because Ubuntu 24.04 ships the PostgreSQL
**16** client, which is what server 3 has. `pg_dump` can dump from servers
older than itself but never newer, so a 17 database would leave server 3's
v16 tooling unable to export from it — requiring the PGDG apt repository and
`postgresql-client-17`. Staying on 16 keeps server 2, server 3's client, and
RDS aligned. PostgreSQL 16 is supported into late 2028. Choosing 17 is
defensible for the longer runway; install the matching client alongside it if
so.

**Single-AZ does not weaken data protection.** Durability comes from
automated backups and point-in-time recovery, which apply either way.
Multi-AZ buys *availability* — a synchronous standby that fails over in a
minute or two, and patching without an outage. For internal tooling whose
downtime does not reach users, that is not worth doubling the instance cost.
Multi-AZ DB *cluster* is unavailable on `db.t4g.micro` regardless, and
Single-AZ can be converted to Multi-AZ later without downtime. Choosing
Single-AZ is also what allows explicitly selecting `us-east-1c`.

**"Connect to an EC2 compute resource"** is worth using. Selecting server 3
there makes RDS create a security group on each side and the port 5432 rule
between them automatically — the same result as configuring them by hand,
without the opportunity for error. It also fills in the VPC from the
instance, and typically forces Public access to No and hides the field,
which is the desired setting regardless.

It adds a security group to server 3. That is additive and leaves the
existing SSH and API rules untouched.

For the **DB subnet group**, choose **Automatic setup** unless an existing
group is already known to cover `us-east-1c` with private subnets. A DB
subnet group is a distinct RDS object rather than the subnets themselves, so
a VPC that has never hosted an RDS instance will not have one.

Note a constraint that catches people out: **a DB subnet group must span at
least two availability zones even for a Single-AZ database.** It is an RDS
API requirement, unrelated to Multi-AZ, existing so the instance can be
relocated during maintenance or recovery. Single-AZ simply means only one of
those zones is in use at a time.

A group listing three subnets is normal. The subnet group defines where RDS
is *permitted* to place the instance; the Availability Zone setting decides
where it *actually* goes. The database occupies one subnet in one zone — it
does not span them, cost more, or replicate across them.

Afterward, confirm the **Availability Zone reads `us-east-1c`**. That is the
one thing automatic setup can get wrong here; if the field shows "No
preference" it should be set explicitly so the database lands in server 3's
zone rather than across a billable boundary.

**Set the initial database name.** It lives under *Additional
configuration*, which is collapsed by default. Left blank, RDS provisions an
instance containing no database at all, and one must be created manually
afterward.

**It is not the same field as the DB instance identifier.** The identifier
names the AWS resource; the initial database name names the PostgreSQL
database created inside it. Both appear in the connection string:

```
postgresql://bigcity:PASSWORD@mediamixerdb.abc123.us-east-1.rds.amazonaws.com:5432/mediamixer
                              └─── from the instance identifier ───┘              └ db name ┘
```

Their naming rules also differ, so one cannot simply be reused for the
other: an instance identifier permits hyphens but not underscores, while a
PostgreSQL database name permits underscores but not hyphens.

**Chosen:** instance `mediamixerdb`, database `mediamixer`.

Provisioning takes 10–20 minutes. The endpoint then appears on the
database's *Connectivity & security* tab, giving:

```
postgresql://bigcity:<password>@mediamixerdb.<suffix>.us-east-1.rds.amazonaws.com:5432/mediamixer
```

That value becomes `DATABASE_URL` in `/etc/mediamixer/mediamixer.env`. Verify from
server 3 with `psql "$DATABASE_URL" -c "SELECT version()"`.

### If on-instance

- Install PostgreSQL on server 3 and leave it bound to `localhost` only.
- Put the data directory on a volume **separate from render scratch**.
- Write and schedule a `pg_dump`, verify a restore, and monitor that the job
  keeps running. If you are not going to do all three, use RDS.

---

## 10. Software on the instance

Your existing servers use Ubuntu (`deliver_dbs.sh` defaults the SSH user to
`ubuntu`), and Ubuntu 24.04 ships Python 3.12, matching the virtualenvs in
your repositories.

```bash
sudo apt update
sudo apt install -y ffmpeg python3-venv python3-pip postgresql-client unzip

ffprobe -version     # both ffprobe and ffmpeg come from the ffmpeg package
python3 --version
```

`ffprobe` is required for Phase 2 and `ffmpeg` for Phase 5; installing both
now costs nothing.

### AWS CLI

Not present on a stock Ubuntu image. Install **v2 from AWS** rather than
`apt install awscli`, which packages the superseded v1:

```bash
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o awscliv2.zip
unzip awscliv2.zip
sudo ./aws/install
aws --version
```

**Never run `aws configure` on this instance.** It prompts for an access key
and writes static credentials to `~/.aws/credentials` — precisely what the
instance role in §8 exists to eliminate. Both the CLI and `boto3` read
credentials from the instance metadata service automatically. If
`aws sts get-caller-identity` returns the role ARN, it is already working
with nothing configured.

The pipeline itself does not require the CLI — the code uses `boto3` — but
it is needed for the §12 verification and is generally useful on an
operations host.

### Python environment

Ubuntu 24.04 enforces PEP 668, marking the system Python as externally
managed: `pip install` outside a virtualenv fails by design. A venv is
therefore required, not merely advisable.

Create it when the repository is first cloned; there is nothing to install
before then.

```bash
sudo mkdir -p /opt/mediamixer
sudo chown ubuntu:ubuntu /opt/mediamixer
python3 -m venv /opt/mediamixer/venv
```

---

## 11. Layout and services

Follow the convention already in use on your other servers — systemd units
reading an `EnvironmentFile`, mirroring `/opt/travel/` on server 2.

```
/opt/mediamixer/
├── app/                # the mediamixer checkout
├── venv/
└── scratch/            # separate EBS volume, before rendering
/etc/mediamixer/
└── mediamixer.env      # chmod 600, root-owned — never in git
```

`mediamixer.env` holds `DATABASE_URL`, `CLIPS_BUCKET`, `CLIPS_PREFIX`,
`CLIPS_REGION`, and `MEDIAMIXER_ADMIN_SECRET`. It contains **no AWS keys** — S3 access
comes from the instance role in §8.

Two units, both shipped in the new repository:

- `mediamixer-api.service` — the FastAPI content library backend,
  bound to `127.0.0.1` or the private IP, reached only through server 2's
  proxy.
- `mediamixer-sync.service` plus `.timer` — the inventory sync on a
  schedule, once the manual dry runs look right.

---

## 12. Verification

Run these on server 3 after launch, before any of the data migration:

```bash
# Instance role is present and S3 is reachable with no stored credentials
aws sts get-caller-identity
aws s3 ls s3://big-city-travel-guide-clips/ugc-assets/ | head

# Writes to a source prefix are refused — this SHOULD fail
echo test > /tmp/t.txt
aws s3 cp /tmp/t.txt s3://big-city-travel-guide-clips/ugc-assets/b-roll/t.txt

# Writes to exported are allowed — this SHOULD succeed
aws s3 cp /tmp/t.txt s3://big-city-travel-guide-clips/ugc-assets/exported/dev/t.txt

# Database reachable
psql "$DATABASE_URL" -c "SELECT version()"

# Media tooling
ffprobe -version
```

The write to `b-roll/` failing is the point of the test. If it succeeds, the
policy in §8 was not applied correctly and the pipeline could overwrite an
irreplaceable master — do not proceed until it is denied.

A correct denial reads `with an explicit deny in an identity-based policy`.
That wording matters: it confirms the `NeverOverwriteSourceMasters` statement
actively fired, rather than the request merely falling through for lack of a
matching `Allow`. The redundant deny is doing real work.

Clean up the `exported/dev/t.txt` test object afterward **from your
workstation, not from server 3** — the instance is denied every delete
action by design, so it cannot remove its own test file:

```bash
aws s3 rm s3://big-city-travel-guide-clips/ugc-assets/exported/dev/t.txt
```

Expect a zero-byte object with a blank name in the `ugc-assets/` listing.
That is the folder marker for the prefix itself, and it is precisely the
kind of object inventory must skip.

Also confirm from server 2 that the API port is reachable, and from anywhere
else that it is not.

---

## 13. Things not to do

- No AWS access keys in `mediamixer.env`, in the repository, or in
  `~/.aws/credentials` on server 3. The instance role is the mechanism.
- No inbound rule from `0.0.0.0/0` on the API port or on 5432.
- No connection from server 3 to the golden database on server 2. If you
  find yourself needing one, the cities reference in §5 of the
  implementation plan is the intended answer.
- No render scratch on the same volume as a database.
- Nothing in `/etc/mediamixer/mediamixer.env` gets committed.

# Infrastructure

What each resource is for, what the schedules do, and which variables matter.

Everything lives in `infra/`. One environment per GCP project, selected by a
tfvars file.

    infra/
      main.tf       network, egress, archive bucket, service account
      sql.tf        Cloud SQL, private networking, DSN secret
      jobs.tf       Cloud Run Jobs, VPC connector, schedules
      xsec.tf       Windows node, handover bucket, IAP access
      variables.tf
      outputs.tf
      versions.tf

---

## Shape of the deployment

Two halves, and they run on different operating systems.

The **settlement gateway** is Python on Cloud Run Jobs: it builds files,
archives them, tracks state, and interprets what comes back.

The **XSec node** is a Windows VM. XSec is Elexon's encryption software,
mandated by BSCP70 Appendix 1 and supplied by BSC CSA with the communications
order. Its user guide states it is "explicitly not supported on any .NET based
environment on any non-Microsoft Windows Operating System", so a Linux-only
deployment is not available to us. The VM exists because the code requires it.

Files pass between the two through a handover bucket. The Windows node does
two things -- encrypt, and move bytes -- with all settlement logic staying on
Cloud Run where the tests are.

---

## Network

**`google_compute_network.vpc`** and **`google_compute_subnetwork.subnet`**

A single subnet, no auto-created ones. Nothing has an external address and
there are no ingress rules: under the pull method nothing initiates a
connection to us.

**`google_compute_address.egress`**

A reserved static external address. Elexon whitelists it, so every connection
to central systems must leave through it. `prevent_destroy` is set: losing it
means a whitelisting request and a wait.

**`google_compute_router_nat.nat`**

`MANUAL_ONLY` allocation pinned to that address. `AUTO_ONLY` would hand out
ephemeral addresses and silently break the whitelist.

**`google_vpc_access_connector.gateway`** *(jobs.tf)*

Connects Cloud Run Jobs to the VPC. Without it a Job gets an ephemeral egress
address and Elexon refuses the connection. Most likely piece to be missed and
hardest to diagnose: the failure looks like a transport fault rather than a
configuration one.

Needs a free `/28` in the subnet. If `subnet_cidr` is tight, give the
connector its own `ip_cidr_range`.

---

## Storage

**`google_storage_bucket.archive`** *(main.tf)*

Every file sent and received, as raw bytes. Versioned, uniform bucket-level
access, `force_destroy = false`. This is the audit record.

Retention is off by default. A retention policy blocks deletion even when
unlocked, which makes clearing test data painful during qualification.

BSC Section U1.6.3 requires 28 months in immediately usable form plus 12 in
archive: **40 months, so `archive_retention_days = 1220`**. Set and lock it
before go-live.

**`google_storage_bucket.handover`** *(xsec.tf)*

The queue between the Cloud Run jobs and the XSec node. Not the archive: this
holds files in transit, and anything here more than a day old is stuck rather
than stored. Seven-day lifecycle rule, `force_destroy = true`.

If Elexon confirm the XSec participant folders can be network shares, this
bucket is replaced by a mounted share and the sync step goes away. That
question is open.

**`google_sql_database_instance.main`** *(sql.tf)*

Postgres 16, private IP only, SSL enforced, no public address. Deletion
protection on. Automated backups at 02:00 with point-in-time recovery and 30
retained backups.

Postgres rather than Firestore because sequence allocation needs
`SELECT ... FOR UPDATE` semantics -- see ADR-0003.

**`google_secret_manager_secret.db_dsn`** *(sql.tf)*

The connection string, including a generated password. The Jobs read it at
startup; nothing else has access.

---

## Identity

**`google_service_account.gateway`**

What the Cloud Run Jobs run as.

| Grant | Why |
|---|---|
| `storage.objectAdmin` on the archive | write files, read them back for retries |
| `storage.objectAdmin` on handover | pass files to and from the XSec node |
| `secretmanager.secretAccessor` on the DSN | connect to the database |
| `cloudsql.client` | reach the instance |

**`google_service_account.xsec`** *(xsec.tf)*

What the Windows node runs as. **Handover bucket only.** It never touches the
settlement database or the archive: it encrypts bytes and moves them.
Narrowing its reach means a compromise there does not reach the settlement
record.

**`google_service_account.scheduler`** *(jobs.tf)*

Cloud Scheduler's identity, with `run.invoker` on the two Jobs and nothing
else. Separate from the gateway account so a compromised scheduler cannot read
settlement data.

---

## The XSec node

**`google_compute_instance.xsec`**

Windows Server 2022 with a desktop, `e2-small`, no external address.

Desktop rather than Core because XSecManager is a GUI and key configuration is
not a one-off -- keys rotate and participants get added. Core would be a
smaller attack surface but cannot run the manager.

**Access is IAP only.** No public address, so RDP is gated by IAM rather than
by knowing an address. The firewall rule allows 3389 from `35.235.240.0/20`,
which is the published range IAP forwards from, and only to instances running
the XSec service account.

```bash
gcloud compute reset-windows-password settlement-test-xsec \
  --zone europe-west2-a --user consus

gcloud compute start-iap-tunnel settlement-test-xsec 3389 \
  --local-host-port=localhost:3389 --zone europe-west2-a
```

Then RDP to `localhost:3389`.

**Daily snapshots, 14 day retention.** The XSec private key lives on that
disk. Losing it means repeating the key exchange with Elexon, so a restore is
much cheaper than a reinstall. `deletion_protection` is on for the same
reason.

**Not yet built:** the sync between the handover bucket and the XSec
`ENCRYPT_IN` / `DECRYPT_OUT` folders. A scheduled task or small service on the
node. Held until Elexon answer the network-share question, because the answer
decides whether it is needed at all.

---

## Jobs

Cloud Run **Jobs**, not a service. `collect` and `sweep` are scheduled, finite
and idempotent; a service would mean holding an HTTP process alive to do work
on a timer.

Both use the same image and differ only in the argument passed to the CLI.

**`collect`** — timeout 600s

Pulls waiting files, archives them, records that they arrived, parses,
dispatches to handlers, sends the ADT acknowledgement.

**`sweep`** — timeout 300s

Marks files silent for longer than the grace period as `UNACKNOWLEDGED`, then
judges each outstanding submission against its own gate closure. Exits
non-zero if anything is critical or missed, so the Job shows as failed.

**`max_retries = 0` on both.** A retried `collect` would re-acknowledge files
it already acknowledged; a retried `sweep` would re-alert. Both run frequently
enough that the next scheduled run *is* the retry.

---

## Schedules

| Job | Cron | Why this interval |
|---|---|---|
| `collect` | `*/5 * * * *` | Elexon sends feedback continuously and a rejection is only useful before Gate Closure. Five minutes is the delay we accept before noticing one. |
| `sweep` | `*/15 * * * *` | Its job is to notice silence. Urgency is judged per settlement period rather than per run, so a coarser interval loses nothing. |

Both in `Europe/London`. Nothing here is anchored to a settlement period, but
a schedule that shifts by an hour twice a year is worth avoiding.

`attempt_deadline = 320s` is how long Scheduler waits for the API call that
starts a Job, not how long the Job may run.

---

## Variables

### Required

| Variable | Notes |
|---|---|
| `project_id` | One project per environment |
| `env` | Short name used in resource names. **Not** the IDD test data flag |
| `vtp_participant_id` | Our Party Id, used with role code `VT` for WMAN and the SVAA flows |
| `ecvna_participant_id` | Our ECVNA Id, used with role code `EN` for ECVNs |

Two participant ids because we are our own ECVN Agent, and each identity has
its own sequence counter -- see ADR-0004.

### Environment

| Variable | Default | Notes |
|---|---|---|
| `settlement_environment` | `TST1` | IDD header test data flag. `OPER` is operational; anything else is a test phase. `text(4)`. Defaults to a test value deliberately: a missing setting must never mean "send to live settlement" |
| `region` | `europe-west2` | |
| `name_prefix` | `settlement` | |
| `log_level` | `INFO` | |

### XSec node

| Variable | Default | Notes |
|---|---|---|
| `xsec_zone` | `europe-west2-a` | Must be within `region` |
| `xsec_machine_type` | `e2-small` | Encrypt and move bytes, plus the Windows desktop |
| `xsec_image` | `windows-cloud/windows-2022` | Desktop, not Core: XSecManager is a GUI |
| `xsec_deletion_protection` | `true` | The private key is on this disk |
| `xsec_admins` | `[]` | Principals allowed to RDP via IAP, e.g. `["user:ethan@consusenergy.com"]` |

### Storage

| Variable | Default | Notes |
|---|---|---|
| `archive_retention_days` | `null` | Off during qualification. **`1220`** (40 months) before go-live |
| `archive_retention_locked` | `false` | **Irreversible.** Only at go-live |
| `db_tier` | `db-f1-micro` | Adequate for qualification |
| `db_availability_type` | `ZONAL` | `REGIONAL` for live: an outage during a gate-closure window is unrecoverable |
| `db_deletion_protection` | `true` | |

### Deployment

| Variable | Default | Notes |
|---|---|---|
| `image_tag` | `latest` | Use a digest for operational. A Job silently picking up a new image is a change nobody assessed |
| `subnet_cidr` | `10.20.0.0/24` | Must leave a free `/28` for the VPC connector |

---

## Outputs

| Output | Use |
|---|---|
| `egress_ip` | Give this to Elexon for whitelisting |
| `archive_bucket` | |
| `handover_bucket` | Queue to the XSec node, not the audit archive |
| `service_account` | |
| `db_dsn_secret` | Secret name, not the value |
| `image_repository` | Where to push the container image |
| `vpc_connector` | |
| `xsec_instance`, `xsec_zone` | For the IAP tunnel command |

---

## Deploying

```bash
cd infra
terraform init
terraform plan  -var-file=envs/test.tfvars
terraform apply -var-file=envs/test.tfvars
```

Build and push the gateway image:

```bash
gcloud auth configure-docker europe-west2-docker.pkg.dev
REPO=$(terraform -chdir=infra output -raw image_repository)
docker build -t "$REPO/gateway:latest" .
docker push "$REPO/gateway:latest"
```

Run a Job by hand:

```bash
gcloud run jobs execute settlement-test-collect --region europe-west2
```

Set up the XSec node, once, over RDP:

1. Install .NET Framework 4.0
2. Run XSec `Setup.exe`
3. Configure the participant id in XSecManager
4. Generate a key pair, export the public key, send it to the BSC Service Desk
5. Import their public key when it arrives
6. Round-trip the test files to confirm both directions work

---

## Before go-live

- `archive_retention_days = 1220`, then `archive_retention_locked = true`
- `db_availability_type = "REGIONAL"`
- `image_tag` pinned to a digest
- `settlement_environment = "OPER"` — operational project only
- Egress address given to Elexon and confirmed whitelisted
- **A separate key exchange for the operational XSec node.** Test keys must
  not be in the path to live settlement
- Channel rows seeded, with the operational test flag, in the operational
  project only

That last one is the structural control in ADR-0010. A test project has no
operational channel row, so it cannot build an operational header, so it
cannot send one. It is not a configuration check that can be overridden.
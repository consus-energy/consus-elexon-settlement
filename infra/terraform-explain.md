# Infrastructure

What each resource is for, what the schedules do, and which variables matter.

Everything lives in `infra/`. One environment per GCP project, selected by a
tfvars file.

    infra/
      main.tf       network, egress, archive bucket, service account
      sql.tf        Cloud SQL, private networking, DSN secret
      jobs.tf       Cloud Run Jobs, VPC connector, schedules
      variables.tf
      outputs.tf
      versions.tf

---

## Network

**`google_compute_network.vpc`** and **`google_compute_subnetwork.subnet`**

A single subnet, no auto-created ones. Nothing has an external address and
there are no ingress rules: under the pull method nothing initiates a
connection to us.

**`google_compute_address.egress`**

A reserved static external address. Elexon whitelists it, so every connection
to central systems must leave through it.

**`google_compute_router.router`** and **`google_compute_router_nat.nat`**

Cloud NAT, so outbound traffic uses the reserved address rather than an
ephemeral one.

**`google_vpc_access_connector.gateway`** *(jobs.tf)*

Connects Cloud Run Jobs to the VPC. Without it a Job gets an ephemeral egress
address and Elexon refuses the connection. This is the piece most likely to be
missed and hardest to diagnose when it is: the failure looks like a transport
fault rather than a configuration one.

Needs a free `/28` in the subnet. If `subnet_cidr` is tight, give the
connector its own `ip_cidr_range` instead.

---

## Storage

**`google_storage_bucket.archive`**

Every file sent and received, as raw bytes. Versioned, uniform bucket-level
access, `force_destroy = false`.

Retention is off by default. A retention policy blocks deletion even when
unlocked, which makes clearing test data painful during qualification. Before
go-live, set `archive_retention_days` and lock it.

BSC Section U1.6.3 requires 28 months in immediately usable form and a further
12 in archive, so 40 months total. `425` is 14 months and is not enough for
go-live; the correct value is `1220`.

**`google_sql_database_instance.main`** *(sql.tf)*

Postgres 16, private IP only, SSL enforced, no public address. Deletion
protection on. Automated backups at 02:00 with point-in-time recovery and 30
retained backups.

Postgres rather than Firestore because sequence allocation needs
`SELECT ... FOR UPDATE` semantics — see ADR-0003.

**`google_secret_manager_secret.db_dsn`** *(sql.tf)*

The connection string, including a generated password. The Jobs read it at
startup; nothing else has access.

---

## Identity

**`google_service_account.gateway`**

What the Jobs run as. Holds:

| Grant | Why |
|---|---|
| `storage.objectAdmin` on the archive bucket | write files, read them back for retries |
| `secretmanager.secretAccessor` on the DSN | connect to the database |
| `cloudsql.client` | reach the instance |

Nothing else. It cannot read other buckets, other secrets, or any other
database.

**`google_service_account.scheduler`** *(jobs.tf)*

Cloud Scheduler's identity, with `run.invoker` on the two Jobs and nothing
more. Separate from the gateway account so a compromised scheduler cannot read
settlement data.

---

## Jobs

Cloud Run **Jobs**, not a service. `collect` and `sweep` are scheduled, finite
and idempotent; a service would mean holding an HTTP process alive to do work
on a timer.

Both use the same image and differ only in the argument passed to the CLI.

**`collect`** — timeout 600s

Pulls waiting files from the transport, archives them, records that they
arrived, parses, dispatches to handlers, and sends the ADT acknowledgement.

**`sweep`** — timeout 300s

Marks files silent for longer than the grace period as `UNACKNOWLEDGED`, then
judges each outstanding submission against its own gate closure. Exits
non-zero if anything is critical or missed, so the Job shows as failed and the
scheduler alerts.

**`max_retries = 0` on both.** A retried `collect` would re-acknowledge files
it already acknowledged; a retried `sweep` would re-alert. Since both run
frequently, the next scheduled run *is* the retry.

---

## Schedules

| Job | Cron | Why this interval |
|---|---|---|
| `collect` | `*/5 * * * *` | Elexon sends feedback continuously and a rejection is only useful before Gate Closure. Five minutes is the delay we accept before noticing one. |
| `sweep` | `*/15 * * * *` | Its job is to notice silence. Urgency is judged per settlement period rather than per run, so a coarser interval loses nothing. |

Both run in `Europe/London`. Nothing here is anchored to a settlement period,
but a schedule that shifts by an hour twice a year is worth avoiding.

`attempt_deadline = 320s` is how long Scheduler waits for the API call that
starts the Job to return, not how long the Job may run. The Job's own timeout
governs that.

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
its own sequence counter — see ADR-0004.

### Environment

| Variable | Default | Notes |
|---|---|---|
| `settlement_environment` | `TST1` | The IDD header test data flag. `OPER` means operational; anything else is a test phase. `text(4)`, so four characters maximum. Defaults to a test value deliberately: a missing setting must never mean "send to live settlement". |
| `region` | `europe-west2` | |
| `name_prefix` | `settlement` | |
| `log_level` | `INFO` | |

### Storage

| Variable | Default | Notes |
|---|---|---|
| `archive_retention_days` | `null` | Off during qualification. Set to `1220` (40 months) before go-live |
| `archive_retention_locked` | `false` | **Irreversible.** Set true only at go-live |
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
| `service_account` | |
| `db_dsn_secret` | Secret name, not the value |
| `image_repository` | Where to push the container image |
| `vpc_connector` | |

---

## Deploying

```bash
cd infra
terraform init
terraform plan  -var-file=envs/test.tfvars
terraform apply -var-file=envs/test.tfvars

terraform output image_repository
```

Then build and push:

```bash
gcloud auth configure-docker europe-west2-docker.pkg.dev
docker build -t "$(terraform -chdir=infra output -raw image_repository)/gateway:latest" .
docker push "$(terraform -chdir=infra output -raw image_repository)/gateway:latest"
```

Run a Job by hand:

```bash
gcloud run jobs execute settlement-test-collect --region europe-west2
```

---

## Before go-live

- `archive_retention_days = 1220`, then `archive_retention_locked = true`
- `db_availability_type = "REGIONAL"`
- `image_tag` pinned to a digest
- `settlement_environment = "OPER"` — in the operational project only
- Egress address given to Elexon and confirmed whitelisted
- Channel rows seeded, with the operational test flag, in the operational
  project only

That last one is the structural control described in ADR-0010. A test project
has no operational channel row, so it cannot build an operational header, so
it cannot send one. It is not a configuration check that can be overridden.
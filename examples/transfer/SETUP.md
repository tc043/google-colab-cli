# Non-interactive transfers: SA key setup

One-time setup so files move between your machine and Colab VMs with **no
browser/OAuth step**. A service-account (SA) key replaces the human.

Both paths share the same first step. Run these in
[Cloud Shell](https://shell.cloud.google.com) (browser, no local gcloud needed)
or any machine with `gcloud`.

## 0. Create the service account + key (shared)

```bash
gcloud iam service-accounts create colab-transfer --display-name "colab transfer"
gcloud iam service-accounts keys create sa-key.json \
  --iam-account=colab-transfer@<PROJECT_ID>.iam.gserviceaccount.com
```

Upload `sa-key.json` to a VM before using either path:

```cmd
.venv\Scripts\colab.exe upload -s <session> sa-key.json /content/sa-key.json
```

**Treat this file like a password.** Keep it out of repos; revoke with
`gcloud iam service-accounts keys delete <KEY_ID> ...` when done.

## Path A — GCS bucket (needs billing attached to project)

```bash
gcloud storage buckets create gs://colab-transfer-<SUFFIX> --location=us-central1
gcloud storage buckets add-iam-policy-binding gs://colab-transfer-<SUFFIX> \
  --member="serviceAccount:colab-transfer@<PROJECT_ID>.iam.gserviceaccount.com" \
  --role=roles/storage.objectAdmin
```

Costs: 5GB-mo storage + 100GB/mo egress free (Feb 2025+), then ~$0.02/GB-mo and
~$0.12/GB. Treat the bucket as a pipe: delete objects after transfer.

VM side:

```bash
colab install -s <session> google-cloud-storage
# edit BUCKET at top of gcs_push.py / gcs_pull.py once, then:
colab exec -s <session> -f examples/transfer/gcs_push.py     # constants mode
colab run -s <session> examples/transfer/gcs_pull.py --bucket gs-bucket-name telemetry.db
```

(`colab run` forwards args as sys.argv; `exec -f` uses the constants.)

## Path B — Drive folder shared with the SA (NOT viable on personal accounts)

**Verified dead end (2026-08):** service accounts have **zero Drive storage
quota**, so every SA-created file fails with 403 `storageQuotaExceeded`
("Service Accounts do not have storage quota"). Google's only sanctioned
workarounds are Shared Drives (requires a paid Workspace account — unavailable
on personal Gmail) or OAuth delegation (the interactive flow this setup exists
to avoid). The drive_push/drive_pull scripts are kept for Workspace users who
can put the SA on a Shared Drive; everyone else should use Path A.

If you do have a Shared Drive: share it with the SA email as **Content
manager**, set `DRIVE_FOLDER_ID` to the Shared Drive folder ID, and pass
`supportsAllDrives=True` to list/create calls.

## Which one?

- **GCS**: fastest bulk throughput, cleanest for 10GB+ pipes; needs card on file.
- **Drive**: $0 forever, visible in Drive UI; API quotas are generous but not
  unlimited (~1000GB/day upload per project is typical headroom).

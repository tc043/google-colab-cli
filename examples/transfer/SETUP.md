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

## Path B — Drive folder shared with the SA ($0, no billing)

1. Create a Drive folder, e.g. `colab-transfer`.
2. Share it with the SA's email
   (`colab-transfer@<PROJECT_ID>.iam.gserviceaccount.com`) as **Editor**.
3. Copy the folder ID from its URL (`.../folders/<FOLDER_ID>`).

Files the SA uploads are owned by the SA but live in that shared folder, so you
can see/download them in the Drive UI.

VM side:

```bash
colab install -s <session> google-api-python-client
# edit DRIVE_FOLDER_ID at top of drive_push.py / drive_pull.py once, then:
colab exec -s <session> -f examples/transfer/drive_push.py    # constants mode
colab run -s <session> examples/transfer/drive_pull.py big.bin
```

`drive_pull.py` with no file args lists accessible files instead.

## Which one?

- **GCS**: fastest bulk throughput, cleanest for 10GB+ pipes; needs card on file.
- **Drive**: $0 forever, visible in Drive UI; API quotas are generous but not
  unlimited (~1000GB/day upload per project is typical headroom).

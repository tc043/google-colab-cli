import hashlib
import os
import sys

SA_KEY_PATH = "/content/sa-key.json"
BUCKET = ""
REMOTE_PREFIX = ""
DEST_DIR = "/content"


def _client():
    from google.cloud.storage import Client
    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_file(SA_KEY_PATH)
    return Client(credentials=creds, project=creds.project_id)


def main():
    args = sys.argv[1:]
    bucket = BUCKET
    prefix = REMOTE_PREFIX
    dest = DEST_DIR
    names = []
    i = 0
    while i < len(args):
        if args[i] == "--bucket" and i + 1 < len(args):
            bucket = args[i + 1]
            i += 2
        elif args[i] == "--prefix" and i + 1 < len(args):
            prefix = args[i + 1].strip("/")
            i += 2
        elif args[i] == "--dest" and i + 1 < len(args):
            dest = args[i + 1]
            i += 2
        else:
            names.append(args[i])
            i += 1
    if not bucket:
        sys.exit("set BUCKET at top of script or pass --bucket")

    client = _client()
    b = client.bucket(bucket)
    if names:
        for name in names:
            remote = f"{prefix}/{name}" if prefix else name
            _pull(b, remote, dest)
    else:
        blobs = client.list_blobs(bucket, prefix=prefix)
        for blob in blobs:
            if blob.name.endswith("/"):
                continue
            _pull(b, blob.name, dest)


def _pull(bucket, remote, dest):
    from google.cloud.storage import Blob

    blob = Blob(bucket, remote)
    if not blob.exists():
        sys.exit(f"gs://{bucket.name}/{remote} does not exist")
    local = os.path.join(dest, os.path.basename(remote))
    h = hashlib.sha256()
    size = 0
    with open(local, "wb") as f:
        with blob.open("rb") as src:
            for chunk in iter(lambda: src.read(1024 * 1024), b""):
                f.write(chunk)
                h.update(chunk)
                size += len(chunk)
    print(f"[gcs_pull] gs://{bucket.name}/{remote} -> {local} ({size} bytes, sha256 {h.hexdigest()})")


if __name__ == "__main__":
    main()

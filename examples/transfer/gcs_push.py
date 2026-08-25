import hashlib
import os
import sys

SA_KEY_PATH = "/content/sa-key.json"
BUCKET = ""
REMOTE_PREFIX = ""


def _client():
    from google.cloud.storage import Client
    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_file(SA_KEY_PATH)
    return Client(credentials=creds, project=creds.project_id)


def main():
    args = sys.argv[1:]
    bucket = BUCKET
    prefix = REMOTE_PREFIX
    paths = []
    i = 0
    while i < len(args):
        if args[i] == "--bucket" and i + 1 < len(args):
            bucket = args[i + 1]
            i += 2
        elif args[i] == "--prefix" and i + 1 < len(args):
            prefix = args[i + 1].strip("/")
            i += 2
        else:
            paths.append(args[i])
            i += 1
    if not bucket:
        sys.exit("set BUCKET at top of script or pass --bucket")
    if not paths:
        sys.exit("usage: gcs_push.py [--bucket B] [--prefix P] <file-or-dir>...")

    client = _client()
    b = client.bucket(bucket)
    for p in paths:
        if os.path.isdir(p):
            for root, _, files in os.walk(p):
                for f in files:
                    _push(b, prefix, os.path.join(root, f))
        else:
            _push(b, prefix, p)


def _push(bucket, prefix, local_path):
    from google.cloud.storage import Blob

    remote = f"{prefix}/{local_path.replace(os.sep, '/').lstrip('/')}" if prefix else local_path.replace(os.sep, "/").lstrip("/")
    blob = Blob(bucket, remote)
    h = hashlib.sha256()
    size = 0
    with open(local_path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
            size += len(chunk)
    blob.upload_from_filename(local_path)
    print(f"[gcs_push] {local_path} -> gs://{bucket.name}/{remote} ({size} bytes, sha256 {h.hexdigest()})")


if __name__ == "__main__":
    main()

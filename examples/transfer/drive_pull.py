import io
import os
import sys

SA_KEY_PATH = "/content/sa-key.json"
DRIVE_FOLDER_ID = ""
DEST_DIR = "/content"
SCOPES = ["https://www.googleapis.com/auth/drive"]


def _service():
    from googleapiclient.discovery import build
    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_file(SA_KEY_PATH, scopes=SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def main():
    args = sys.argv[1:]
    folder = DRIVE_FOLDER_ID
    dest = DEST_DIR
    names = []
    i = 0
    while i < len(args):
        if args[i] == "--folder" and i + 1 < len(args):
            folder = args[i + 1]
            i += 2
        elif args[i] == "--dest" and i + 1 < len(args):
            dest = args[i + 1]
            i += 2
        else:
            names.append(args[i])
            i += 1

    svc = _service()
    if not names:
        q = f"'{folder}' in parents and trashed=false" if folder else "trashed=false"
        resp = svc.files().list(q=q, pageSize=100, fields="files(id,name,size)").execute()
        for f in resp["files"]:
            print(f"{f['id']}  {f.get('size', '-')}  {f['name']}")
        return
    for name in names:
        q = f"'{folder}' in parents and trashed=false and name='{name}'" if folder else f"name='{name}' and trashed=false"
        resp = svc.files().list(q=q, pageSize=5, fields="files(id,name,size)").execute()
        files = resp["files"]
        if not files:
            sys.exit(f"no Drive file named '{name}' accessible to this service account")
        f = files[0]
        _pull(svc, f, dest)


def _pull(svc, meta, dest):
    from googleapiclient.http import MediaIoBaseDownload

    local = os.path.join(dest, meta["name"])
    req = svc.files().get_media(fileId=meta["id"])
    h_total = int(meta.get("size", 0))
    with open(local, "wb") as fh:
        dl = MediaIoBaseDownload(fh, req, chunksize=16 * 1024 * 1024)
        done = False
        while not done:
            status, done = dl.next_chunk()
            if status:
                print(f"[drive_pull] {meta['name']}: {int(status.progress() * 100)}%")
    size = os.path.getsize(local)
    if h_total and size != h_total:
        sys.exit(f"size mismatch for {local}: got {size}, expected {h_total}")
    print(f"[drive_pull] {meta['name']} -> {local} ({size} bytes)")


if __name__ == "__main__":
    main()

import os
import sys

SA_KEY_PATH = "/content/sa-key.json"
DRIVE_FOLDER_ID = ""
SCOPES = ["https://www.googleapis.com/auth/drive"]


def _service():
    from googleapiclient.discovery import build
    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_file(SA_KEY_PATH, scopes=SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def main():
    args = sys.argv[1:]
    folder = DRIVE_FOLDER_ID
    paths = []
    i = 0
    while i < len(args):
        if args[i] == "--folder" and i + 1 < len(args):
            folder = args[i + 1]
            i += 2
        else:
            paths.append(args[i])
            i += 1
    if not folder:
        sys.exit("set DRIVE_FOLDER_ID at top of script or pass --folder")
    if not paths:
        sys.exit("usage: drive_push.py [--folder ID] <file>...")

    svc = _service()
    from googleapiclient.http import MediaFileUpload

    for p in paths:
        name = os.path.basename(p)
        meta = {"name": name}
        if folder != "root":
            meta["parents"] = [folder]
        media = MediaFileUpload(p, resumable=True, chunksize=16 * 1024 * 1024)
        req = svc.files().create(body=meta, media_body=media, fields="id,size")
        resp = None
        while resp is None:
            status, resp = req.next_chunk()
            if status:
                print(f"[drive_push] {name}: {int(status.progress() * 100)}%")
        print(f"[drive_push] {name} -> id={resp['id']} size={resp.get('size')}")


if __name__ == "__main__":
    main()

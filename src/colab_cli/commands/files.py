# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import click
import gzip
import hashlib
import os
import shutil
import tempfile
import typer
from typing import Optional
from typing_extensions import Annotated

from colab_cli.contents import ContentsClient
from colab_cli.runtime import ColabRuntime

# The runtime proxy tunnel resets connections when a single PUT body is too
# large (~100MB observed). Files above this threshold are gzipped locally,
# uploaded as "<remote>.gz", decompressed on the VM via the kernel, verified
# against the original size, and cleaned up.
UPLOAD_COMPRESS_ABOVE_BYTES = 64 * 1024 * 1024


def ls(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
    path: Annotated[str, typer.Argument(help="Remote path to list")] = "content",
):
    """List files in a session"""
    from colab_cli.common import state

    name = state.resolve_session(session)
    s = state.store.get(name)
    if not s:
        typer.echo(f"[colab] Session '{name}' not found.")
        raise typer.Exit(1)
    contents = ContentsClient(s)
    try:
        data = contents.list_dir(path)
        state.history.log_event(name, "file_operation", {"op": "ls", "path": path})
        if data.get("type") == "directory":
            items = data.get("content", [])
            for item in sorted(
                items, key=lambda x: (x.get("type") != "directory", x.get("name"))
            ):
                suffix = "/" if item.get("type") == "directory" else ""
                typer.echo(f"{item.get('name')}{suffix}")
        else:
            typer.echo(data.get("name"))
    except Exception as e:
        typer.echo(f"[colab] Error: {e}")
        raise typer.Exit(1)


def rm(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
    path: Annotated[str, typer.Argument(help="Remote path to remove")] = ...,
):
    """Remove a remote file"""
    from colab_cli.common import state

    name = state.resolve_session(session)
    s = state.store.get(name)
    if not s:
        typer.echo(f"[colab] Session '{name}' not found.")
        raise typer.Exit(1)
    contents = ContentsClient(s)
    try:
        contents.rm(path)
        state.history.log_event(name, "file_operation", {"op": "rm", "path": path})
        typer.echo(f"[colab] Deleted {path}")
    except Exception as e:
        typer.echo(f"[colab] Error: {e}")
        raise typer.Exit(1)


def _upload_compressed(state, name, s, contents, local_path, remote_path):
    gz_remote = remote_path + ".gz"
    expected_size = os.path.getsize(local_path)

    fd, tmp_gz = tempfile.mkstemp(suffix=".gz")
    os.close(fd)
    try:
        with open(local_path, "rb") as src, gzip.open(tmp_gz, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst)

        contents.upload(tmp_gz, gz_remote)

        runtime = ColabRuntime(s.url, s.token, kernel_id=s.kernel_id)
        rp = repr(remote_path)
        gp = repr(gz_remote)
        runtime.execute_code(
            "import gzip, os, shutil\n"
            f"os.makedirs(os.path.dirname({rp}) or '/', exist_ok=True)\n"
            f"with gzip.open({gp}, 'rb') as src, open({rp}, 'wb') as dst:\n"
            "    shutil.copyfileobj(src, dst)\n"
            f"print('decompressed', {rp}, os.path.getsize({rp}))"
        )

        meta = contents.list_dir(remote_path)
        remote_size = meta.get("size") if isinstance(meta, dict) else None
        if remote_size is not None and int(remote_size) != expected_size:
            raise RuntimeError(
                f"Size mismatch after decompression: expected {expected_size}, got {remote_size}"
            )

        contents.rm(gz_remote)

        state.history.log_event(
            name,
            "file_operation",
            {"op": "upload", "local": local_path, "remote": remote_path, "compressed": True},
        )
        typer.echo(
            f"[colab] Uploaded '{local_path}' to '{remote_path}' "
            "(gzip-compressed in transit, decompressed on VM)"
        )
    finally:
        if os.path.exists(tmp_gz):
            os.remove(tmp_gz)


def upload(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
    local_path: Annotated[str, typer.Argument(help="Local file to upload")] = ...,
    remote_path: Annotated[str, typer.Argument(help="Remote path to upload to")] = ...,
    no_compress: Annotated[
        bool,
        typer.Option(
            "--no-compress",
            help="Disable automatic gzip transfer for large files",
        ),
    ] = False,
):
    """Upload a file to a session"""
    from colab_cli.common import state

    name = state.resolve_session(session)
    s = state.store.get(name)
    if not s:
        typer.echo(f"[colab] Session '{name}' not found.")
        raise typer.Exit(1)
    if not os.path.isfile(local_path):
        typer.echo(f"[colab] Local file '{local_path}' not found.")
        raise typer.Exit(1)
    contents = ContentsClient(s)
    try:
        if not no_compress and os.path.getsize(local_path) > UPLOAD_COMPRESS_ABOVE_BYTES:
            _upload_compressed(state, name, s, contents, local_path, remote_path)
        else:
            contents.upload(local_path, remote_path)
            state.history.log_event(
                name,
                "file_operation",
                {"op": "upload", "local": local_path, "remote": remote_path},
            )
            typer.echo(f"[colab] Uploaded '{local_path}' to '{remote_path}'")
    except Exception as e:
        typer.echo(f"[colab] Upload failed: {e}")
        raise typer.Exit(1)


def download(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
    remote_path: Annotated[
        str, typer.Argument(help="Remote path to download from")
    ] = ...,
    local_path: Annotated[
        str, typer.Argument(help="Local path to save the file")
    ] = ...,
):
    """Download a file from a session"""
    from colab_cli.common import state

    name = state.resolve_session(session)
    s = state.store.get(name)
    if not s:
        typer.echo(f"[colab] Session '{name}' not found.")
        raise typer.Exit(1)
    contents = ContentsClient(s)
    try:
        contents.download(remote_path, local_path)
        state.history.log_event(
            name,
            "file_operation",
            {"op": "download", "remote": remote_path, "local": local_path},
        )
        typer.echo(f"[colab] Downloaded '{remote_path}' to '{local_path}'")
    except Exception as e:
        typer.echo(f"[colab] Download failed: {e}")
        raise typer.Exit(1)


def edit(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
    remote_path: Annotated[str, typer.Argument(help="Remote path to edit")] = ...,
):
    """Edit a file on a running Colab session"""
    from colab_cli.common import state

    name = state.resolve_session(session)
    s = state.store.get(name)
    if not s:
        typer.echo(f"[colab] Session '{name}' not found.")
        raise typer.Exit(1)

    contents = ContentsClient(s)

    def get_file_hash(path):
        if not os.path.exists(path):
            return None
        with open(path, "rb") as f:
            return hashlib.file_digest(f, "sha256").hexdigest()

    _, ext = os.path.splitext(remote_path)

    with tempfile.NamedTemporaryFile(suffix=ext) as tf:
        local_path = tf.name

        try:
            contents.download(remote_path, local_path)
        except Exception:
            # If download fails, assume file doesn't exist and start empty
            pass

        hash_before = get_file_hash(local_path)

        click.edit(filename=local_path)

        hash_after = get_file_hash(local_path)

        if hash_after != hash_before:
            contents.upload(local_path, remote_path)
            state.history.log_event(
                name,
                "file_operation",
                {"op": "edit", "remote": remote_path},
            )
            typer.echo(f"[colab] Edited and uploaded '{remote_path}'")
        else:
            typer.echo(f"[colab] No changes made to '{remote_path}'")


def register(app: typer.Typer):
    app.command()(ls)
    app.command()(rm)
    app.command()(upload)
    app.command()(download)
    app.command()(edit)

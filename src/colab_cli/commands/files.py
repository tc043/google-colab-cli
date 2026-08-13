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
TRANSFER_COMPRESS_ABOVE_BYTES = 64 * 1024 * 1024

# Even a gzipped payload can stay above the tunnel's per-request limit
# (incompressible data barely shrinks). Transfers whose payload still exceeds
# this size are split into part files ("<remote>.clabpartNNNNN"), moved one
# request at a time, and reassembled on the receiving side via the kernel.
TRANSFER_PART_BYTES = 48 * 1024 * 1024


def ls(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
    path: Annotated[str, typer.Argument(help="Remote path to list")] = "content",
):
    """List files in a session"""
    from colab_cli.common import state

    name = state.resolve_session(session)
    if not state.store.get(name):
        typer.echo(f"[colab] Session '{name}' not found.")
        raise typer.Exit(1)
    try:
        data = state.run_with_runtime_proxy_retry(
            name, lambda s: ContentsClient(s).list_dir(path)
        )
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
    if not state.store.get(name):
        typer.echo(f"[colab] Session '{name}' not found.")
        raise typer.Exit(1)
    try:
        state.run_with_runtime_proxy_retry(name, lambda s: ContentsClient(s).rm(path))
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
        try:
            runtime.execute_code(
                "import gzip, os, shutil\n"
                f"os.makedirs(os.path.dirname({rp}) or '/', exist_ok=True)\n"
                f"with gzip.open({gp}, 'rb') as src, open({rp}, 'wb') as dst:\n"
                "    shutil.copyfileobj(src, dst)\n"
                f"print('decompressed', {rp}, os.path.getsize({rp}))",
                timeout=1800,
            )
        finally:
            runtime.stop()

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


def _upload_chunked(
    state,
    name,
    s,
    contents,
    transfer_path,
    staging_remote,
    final_remote,
    expected_size,
    display_local=None,
):
    """Upload a payload above the per-request ceiling as part files, then
    concatenate them on the VM via the kernel.

    transfer_path is the local blob to move (already gzipped if applicable);
    staging_remote is where the assembled blob lands ("<remote>.gz" when
    compressed, otherwise final_remote); expected_size is the size of the
    original uncompressed file used for verification.
    """
    display_local = display_local or transfer_path
    part_prefix = staging_remote + ".clabpart"
    total = os.path.getsize(transfer_path)
    nparts = (total + TRANSFER_PART_BYTES - 1) // TRANSFER_PART_BYTES

    uploaded_parts = []
    try:
        with open(transfer_path, "rb") as src:
            for i in range(nparts):
                data = src.read(TRANSFER_PART_BYTES)
                if not data:
                    break
                part_remote = f"{part_prefix}{i:05d}"
                contents.upload_bytes(data, part_remote)
                uploaded_parts.append(part_remote)

        runtime = ColabRuntime(s.url, s.token, kernel_id=s.kernel_id)
        stage = repr(staging_remote)
        dirname = repr(os.path.dirname(staging_remote))
        basename = repr(os.path.basename(staging_remote))
        final = repr(final_remote)
        code = (
            "import gzip, os, shutil\n"
            f"_d = os.path.dirname({stage}) or '.'\n"
            f"_parts = sorted(p for p in os.listdir(_d) "
            f"if p.startswith({basename} + '.clabpart'))\n"
            f"with open({stage}, 'wb') as out:\n"
            "    for _p in _parts:\n"
            "        with open(os.path.join(_d, _p), 'rb') as inp:\n"
            "            shutil.copyfileobj(inp, out)\n"
        )
        if staging_remote != final_remote:
            code += (
                f"with gzip.open({stage}, 'rb') as src, open({final}, 'wb') as dst:\n"
                "    shutil.copyfileobj(src, dst)\n"
            )
        code += f"print('assembled', {final}, os.path.getsize({final}))\n"
        try:
            # Assembly of multi-GB blobs can exceed the default 10s quiet
            # timeout, so give it an explicit generous budget.
            runtime.execute_code(code, timeout=1800)
        finally:
            runtime.stop()

        meta = contents.list_dir(final_remote)
        remote_size = meta.get("size") if isinstance(meta, dict) else None
        if remote_size is not None and int(remote_size) != expected_size:
            raise RuntimeError(
                f"Size mismatch after chunked assembly: "
                f"expected {expected_size}, got {remote_size}"
            )

        if staging_remote != final_remote:
            contents.rm(staging_remote)

        state.history.log_event(
            name,
            "file_operation",
            {
                "op": "upload",
                "local": display_local,
                "remote": final_remote,
                "chunked": True,
                "parts": int(nparts),
            },
        )
        typer.echo(
            f"[colab] Uploaded '{display_local}' to '{final_remote}' "
            f"in {nparts} chunks"
        )
    finally:
        for part_remote in uploaded_parts:
            try:
                contents.rm(part_remote)
            except Exception:
                pass


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
    if not state.store.get(name):
        typer.echo(f"[colab] Session '{name}' not found.")
        raise typer.Exit(1)
    if not os.path.isfile(local_path):
        typer.echo(f"[colab] Local file '{local_path}' not found.")
        raise typer.Exit(1)
    tmp_gz = None
    try:
        original_size = os.path.getsize(local_path)
        use_gz = not no_compress and original_size > TRANSFER_COMPRESS_ABOVE_BYTES
        if use_gz:
            fd, tmp_gz = tempfile.mkstemp(suffix=".gz")
            os.close(fd)
            with open(local_path, "rb") as src, gzip.open(
                tmp_gz, "wb", compresslevel=6
            ) as dst:
                shutil.copyfileobj(src, dst)
            transfer_path = tmp_gz
            staging_remote = remote_path + ".gz"
        else:
            transfer_path = local_path
            staging_remote = remote_path

        transfer_size = os.path.getsize(transfer_path)

        def transfer(current_session):
            contents = ContentsClient(current_session)
            if transfer_size > TRANSFER_PART_BYTES:
                _upload_chunked(
                    state,
                    name,
                    current_session,
                    contents,
                    transfer_path,
                    staging_remote,
                    remote_path,
                    expected_size=original_size,
                    display_local=local_path,
                )
            elif use_gz:
                _upload_compressed(
                    state,
                    name,
                    current_session,
                    contents,
                    local_path,
                    remote_path,
                )
            else:
                contents.upload(local_path, remote_path)
                state.history.log_event(
                    name,
                    "file_operation",
                    {"op": "upload", "local": local_path, "remote": remote_path},
                )
                typer.echo(f"[colab] Uploaded '{local_path}' to '{remote_path}'")

        state.run_with_runtime_proxy_retry(name, transfer)
    except Exception as e:
        typer.echo(f"[colab] Upload failed: {e}")
        raise typer.Exit(1)
    finally:
        if tmp_gz is not None and os.path.exists(tmp_gz):
            os.remove(tmp_gz)


def _compress_on_vm(s, remote_path, gz_remote):
    runtime = ColabRuntime(s.url, s.token, kernel_id=s.kernel_id)
    rp = repr(remote_path)
    gp = repr(gz_remote)
    try:
        runtime.execute_code(
            "import gzip, os, shutil\n"
            f"with open({rp}, 'rb') as src, gzip.open({gp}, 'wb', compresslevel=6) as dst:\n"
            "    shutil.copyfileobj(src, dst)\n"
            f"print('compressed', {gp}, os.path.getsize({gp}))",
            timeout=1800,
        )
    finally:
        runtime.stop()


def _download_chunked(state, name, s, contents, transfer_remote, dest_path, transfer_size):
    """Fetch a payload above the per-request ceiling by splitting it into part
    files on the VM via the kernel, downloading them one request at a time,
    and reassembling locally into dest_path."""
    part_prefix = transfer_remote + ".clabpart"
    transfer_size = int(transfer_size)
    nparts = (transfer_size + TRANSFER_PART_BYTES - 1) // TRANSFER_PART_BYTES

    fd, tmp_part = tempfile.mkstemp(suffix=".part")
    os.close(fd)
    try:
        runtime = ColabRuntime(s.url, s.token, kernel_id=s.kernel_id)
        src = repr(transfer_remote)
        try:
            # Splitting multi-GB blobs can exceed the default 10s quiet
            # timeout, so give it an explicit generous budget.
            runtime.execute_code(
                "import os\n"
                f"_src = {src}\n"
                f"_n = {TRANSFER_PART_BYTES}\n"
                "_i = 0\n"
                "with open(_src, 'rb') as f:\n"
                "    while True:\n"
                "        _b = f.read(_n)\n"
                "        if not _b:\n"
                "            break\n"
                "        with open(_src + '.clabpart%05d' % _i, 'wb') as p:\n"
                "            p.write(_b)\n"
                "        _i += 1\n"
                "print('split', _i)\n",
                timeout=1800,
            )
        finally:
            runtime.stop()

        with open(dest_path, "wb") as out:
            for i in range(nparts):
                contents.download(f"{part_prefix}{i:05d}", tmp_part)
                with open(tmp_part, "rb") as pf:
                    shutil.copyfileobj(pf, out)

        actual = os.path.getsize(dest_path)
        if actual != transfer_size:
            raise RuntimeError(
                f"Size mismatch after chunked download: "
                f"expected {transfer_size}, got {actual}"
            )
    finally:
        if os.path.exists(tmp_part):
            os.remove(tmp_part)
        for i in range(nparts):
            try:
                contents.rm(f"{part_prefix}{i:05d}")
            except Exception:
                pass


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
    no_compress: Annotated[
        bool,
        typer.Option(
            "--no-compress",
            help="Disable automatic gzip transfer for large files",
        ),
    ] = False,
):
    """Download a file from a session"""
    from colab_cli.common import state

    name = state.resolve_session(session)
    if not state.store.get(name):
        typer.echo(f"[colab] Session '{name}' not found.")
        raise typer.Exit(1)
    try:
        def transfer(current_session):
            contents = ContentsClient(current_session)
            meta = contents.list_dir(remote_path)
            remote_size = meta.get("size") if isinstance(meta, dict) else None

            if (
                not no_compress
                and remote_size is not None
                and int(remote_size) > TRANSFER_COMPRESS_ABOVE_BYTES
            ):
                gz_remote = remote_path + ".gz"
                _compress_on_vm(current_session, remote_path, gz_remote)

                fd, tmp_gz = tempfile.mkstemp(suffix=".gz")
                os.close(fd)
                try:
                    gz_meta = contents.list_dir(gz_remote)
                    gz_size = gz_meta.get("size") if isinstance(gz_meta, dict) else None
                    chunked = False
                    if gz_size is not None and int(gz_size) > TRANSFER_PART_BYTES:
                        _download_chunked(
                            state,
                            name,
                            current_session,
                            contents,
                            gz_remote,
                            tmp_gz,
                            int(gz_size),
                        )
                        chunked = True
                    else:
                        contents.download(gz_remote, tmp_gz)

                    with gzip.open(tmp_gz, "rb") as src, open(local_path, "wb") as dst:
                        shutil.copyfileobj(src, dst)

                    actual_size = os.path.getsize(local_path)
                    if actual_size != int(remote_size):
                        raise RuntimeError(
                            f"Size mismatch after decompression: "
                            f"expected {remote_size}, got {actual_size}"
                        )

                    contents.rm(gz_remote)

                    event = {
                        "op": "download",
                        "remote": remote_path,
                        "local": local_path,
                        "compressed": True,
                    }
                    if chunked:
                        event["chunked"] = True
                    state.history.log_event(name, "file_operation", event)
                    suffix = ", chunked" if chunked else ""
                    typer.echo(
                        f"[colab] Downloaded '{remote_path}' to '{local_path}' "
                        f"(gzip-compressed in transit{suffix})"
                    )
                finally:
                    if os.path.exists(tmp_gz):
                        os.remove(tmp_gz)
            elif remote_size is not None and int(remote_size) > TRANSFER_PART_BYTES:
                _download_chunked(
                    state,
                    name,
                    current_session,
                    contents,
                    remote_path,
                    local_path,
                    int(remote_size),
                )
                state.history.log_event(
                    name,
                    "file_operation",
                    {
                        "op": "download",
                        "remote": remote_path,
                        "local": local_path,
                        "chunked": True,
                    },
                )
                typer.echo(
                    f"[colab] Downloaded '{remote_path}' to '{local_path}' in chunks"
                )
            else:
                contents.download(remote_path, local_path)
                state.history.log_event(
                    name,
                    "file_operation",
                    {"op": "download", "remote": remote_path, "local": local_path},
                )
                typer.echo(f"[colab] Downloaded '{remote_path}' to '{local_path}'")

        state.run_with_runtime_proxy_retry(name, transfer)
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
    if not state.store.get(name):
        typer.echo(f"[colab] Session '{name}' not found.")
        raise typer.Exit(1)

    def get_file_hash(path):
        if not os.path.exists(path):
            return None
        with open(path, "rb") as f:
            return hashlib.file_digest(f, "sha256").hexdigest()

    _, ext = os.path.splitext(remote_path)

    with tempfile.NamedTemporaryFile(suffix=ext) as tf:
        local_path = tf.name

        try:
            state.run_with_runtime_proxy_retry(
                name, lambda s: ContentsClient(s).download(remote_path, local_path)
            )
        except Exception:
            # If download fails, assume file doesn't exist and start empty
            pass

        hash_before = get_file_hash(local_path)

        click.edit(filename=local_path)

        hash_after = get_file_hash(local_path)

        if hash_after != hash_before:
            state.run_with_runtime_proxy_retry(
                name, lambda s: ContentsClient(s).upload(local_path, remote_path)
            )
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

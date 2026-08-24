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

import gzip
import os
import sys
from unittest.mock import patch

import pytest

from colab_cli.cli import main


@pytest.fixture
def mock_contents():
    with patch("colab_cli.commands.files.ContentsClient") as mock_cls:
        yield mock_cls.return_value


def _run_upload(local, remote, extra_args=()):
    argv = ["colab", "upload", "-s", "test-session", local, remote, *extra_args]
    with patch.object(sys, "argv", argv):
        with pytest.raises(SystemExit) as error:
            main()
    return error.value.code


def _run_download(remote, local, extra_args=()):
    argv = ["colab", "download", "-s", "test-session", remote, local, *extra_args]
    with patch.object(sys, "argv", argv):
        with pytest.raises(SystemExit) as error:
            main()
    return error.value.code


def _part_names(remote, count):
    return {f"{remote}.clabpart{i:05d}" for i in range(count)}


def _removed_paths(mock_contents):
    return {call.args[0] for call in mock_contents.rm.call_args_list}


def test_upload_chunked_assembles_on_vm(
    tmp_path, mock_contents, mock_common_state, monkeypatch, capsys
):
    from colab_cli.commands import files

    monkeypatch.setattr(files, "TRANSFER_PART_BYTES", 16)
    payload = bytes(range(50))  # 50 bytes -> 4 parts (16/16/16/2)
    remote = "/content/big.bin"
    local = tmp_path / "big.bin"
    local.write_bytes(payload)

    uploaded = {}

    def fake_upload_bytes(data, rpath):
        uploaded[rpath] = data

    mock_contents.upload_bytes.side_effect = fake_upload_bytes

    vm = {}

    def fake_exec(code, timeout=None):
        assert ".clabpart" in code
        parts = sorted(p for p in uploaded if p.startswith(remote + ".clabpart"))
        assembled = b"".join(uploaded[p] for p in parts)
        if "gzip.open" in code:
            assembled = gzip.decompress(assembled)
        vm[remote] = assembled

    runtime_instance = files.ColabRuntime.return_value
    runtime_instance.execute_code.side_effect = fake_exec

    mock_contents.list_dir.return_value = {"size": len(payload)}

    code = _run_upload(str(local), remote)

    assert code == 0
    assert sorted(uploaded) == sorted(_part_names(remote, 4))
    assert b"".join(uploaded[f"{remote}.clabpart{i:05d}"] for i in range(4)) == payload
    assert vm[remote] == payload
    assert runtime_instance.execute_code.call_args.kwargs.get("timeout") == 1800
    assert _removed_paths(mock_contents) == _part_names(remote, 4)
    assert "chunks" in capsys.readouterr().out


def test_upload_chunked_size_mismatch_cleans_parts(
    tmp_path, mock_contents, mock_common_state, monkeypatch
):
    from colab_cli.commands import files

    monkeypatch.setattr(files, "TRANSFER_PART_BYTES", 16)
    local = tmp_path / "big.bin"
    local.write_bytes(os.urandom(50))
    remote = "/content/big.bin"

    mock_contents.upload_bytes.side_effect = lambda data, rpath: None
    files.ColabRuntime.return_value.execute_code.side_effect = None
    mock_contents.list_dir.return_value = {"size": 49}

    code = _run_upload(str(local), remote)

    assert code == 1
    assert _removed_paths(mock_contents) == _part_names(remote, 4)


def test_upload_no_compress_still_chunks(
    tmp_path, mock_contents, mock_common_state, monkeypatch
):
    from colab_cli.commands import files

    monkeypatch.setattr(files, "TRANSFER_PART_BYTES", 16)
    local = tmp_path / "big.bin"
    local.write_bytes(os.urandom(50))

    mock_contents.upload_bytes.side_effect = lambda data, rpath: None
    mock_contents.list_dir.return_value = {"size": 50}

    code = _run_upload(str(local), "/content/big.bin", ("--no-compress",))

    assert code == 0
    mock_contents.upload.assert_not_called()
    assert mock_contents.upload_bytes.call_count == 4


def test_download_chunked_reassembles(
    tmp_path, mock_contents, mock_common_state, monkeypatch, capsys
):
    from colab_cli.commands import files

    monkeypatch.setattr(files, "TRANSFER_PART_BYTES", 16)
    payload = bytes(range(50))
    remote = "/content/big.bin"
    local = tmp_path / "big.bin"

    vm_parts = {}

    def fake_exec(code, timeout=None):
        assert "_src =" in code
        for i in range(0, len(payload), 16):
            vm_parts[f"{remote}.clabpart{i // 16:05d}"] = payload[i : i + 16]

    files.ColabRuntime.return_value.execute_code.side_effect = fake_exec

    def fake_download(rpath, lpath):
        with open(lpath, "wb") as f:
            f.write(vm_parts[rpath])

    mock_contents.download.side_effect = fake_download
    mock_contents.list_dir.return_value = {"size": len(payload)}

    code = _run_download(remote, str(local))

    assert code == 0
    assert local.read_bytes() == payload
    assert _removed_paths(mock_contents) == _part_names(remote, 4)
    assert "chunks" in capsys.readouterr().out


def test_download_chunked_size_mismatch_cleans_parts(
    tmp_path, mock_contents, mock_common_state, monkeypatch
):
    from colab_cli.commands import files

    monkeypatch.setattr(files, "TRANSFER_PART_BYTES", 16)
    payload = bytes(range(50))
    remote = "/content/big.bin"
    local = tmp_path / "big.bin"

    vm_parts = {
        f"{remote}.clabpart{i:05d}": payload[i * 16 : (i + 1) * 16] for i in range(3)
    }
    vm_parts[f"{remote}.clabpart00003"] = b"x"  # truncated final part

    files.ColabRuntime.return_value.execute_code.side_effect = None

    def fake_download(rpath, lpath):
        with open(lpath, "wb") as f:
            f.write(vm_parts[rpath])

    mock_contents.download.side_effect = fake_download
    mock_contents.list_dir.return_value = {"size": len(payload)}

    code = _run_download(remote, str(local))

    assert code == 1
    assert _removed_paths(mock_contents) == _part_names(remote, 4)


def test_download_gzipped_payload_still_chunks(
    tmp_path, mock_contents, mock_common_state, monkeypatch, capsys
):
    from colab_cli.commands import files

    monkeypatch.setattr(files, "TRANSFER_COMPRESS_ABOVE_BYTES", 10)
    monkeypatch.setattr(files, "TRANSFER_PART_BYTES", 16)
    payload = os.urandom(100)  # random -> gz expands, stays above part ceiling
    remote = "/content/big.bin"
    gz_remote = remote + ".gz"
    gz_blob = gzip.compress(payload, compresslevel=6)
    assert len(gz_blob) > 16
    local = tmp_path / "big.bin"

    vm_parts = {}

    def fake_exec(code, timeout=None):
        if "_src =" in code:
            for i in range(0, len(gz_blob), 16):
                vm_parts[f"{gz_remote}.clabpart{i // 16:05d}"] = gz_blob[i : i + 16]

    files.ColabRuntime.return_value.execute_code.side_effect = fake_exec

    def fake_download(rpath, lpath):
        with open(lpath, "wb") as f:
            f.write(vm_parts[rpath])

    mock_contents.download.side_effect = fake_download
    meta = {remote: {"size": len(payload)}, gz_remote: {"size": len(gz_blob)}}
    mock_contents.list_dir.side_effect = lambda p: meta[p]

    code = _run_download(remote, str(local))

    assert code == 0
    assert local.read_bytes() == payload
    expected_rm = set(vm_parts) | {gz_remote}
    assert _removed_paths(mock_contents) == expected_rm
    assert "chunked" in capsys.readouterr().out


def test_upload_gzipped_payload_still_chunks(
    tmp_path, mock_contents, mock_common_state, monkeypatch, capsys
):
    from colab_cli.commands import files

    monkeypatch.setattr(files, "TRANSFER_COMPRESS_ABOVE_BYTES", 10)
    monkeypatch.setattr(files, "TRANSFER_PART_BYTES", 16)
    payload = os.urandom(100)
    remote = "/content/big.bin"
    gz_remote = remote + ".gz"
    gz_blob = gzip.compress(payload, compresslevel=6)
    local = tmp_path / "big.bin"
    local.write_bytes(payload)

    uploaded = {}

    def fake_upload_bytes(data, rpath):
        uploaded[rpath] = data

    mock_contents.upload_bytes.side_effect = fake_upload_bytes

    vm = {}

    def fake_exec(code, timeout=None):
        if "_parts =" in code:
            parts = sorted(p for p in uploaded if p.startswith(gz_remote + ".clabpart"))
            assembled = b"".join(uploaded[p] for p in parts)
            vm[gz_remote] = assembled
            if "gzip.open" in code:
                vm[remote] = gzip.decompress(assembled)

    files.ColabRuntime.return_value.execute_code.side_effect = fake_exec

    mock_contents.list_dir.side_effect = lambda p: (
        {remote: {"size": len(payload)}}[p] if p == remote else {"size": None}
    )

    code = _run_upload(str(local), remote)

    assert code == 0
    assert vm[remote] == payload
    assert _removed_paths(mock_contents) == set(uploaded) | {gz_remote}

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


def test_small_file_uploads_plain(tmp_path, mock_contents, capsys):
    local = tmp_path / "small.bin"
    local.write_bytes(b"x" * 100)

    code = _run_upload(str(local), "/content/small.bin")

    assert code == 0
    mock_contents.upload.assert_called_once_with(str(local), "/content/small.bin")
    mock_contents.rm.assert_not_called()
    assert "Uploaded" in capsys.readouterr().out


def test_large_file_gzips_and_decompresses_on_vm(
    tmp_path, mock_contents, mock_common_state, monkeypatch
):
    from colab_cli.commands import files

    monkeypatch.setattr(files, "TRANSFER_COMPRESS_ABOVE_BYTES", 10)
    payload = os.urandom(4096)
    local = tmp_path / "big.bin"
    local.write_bytes(payload)
    mock_contents.list_dir.return_value = {"size": len(payload)}
    uploaded = {}

    def fake_upload(tmp_path_arg, remote):
        with open(tmp_path_arg, "rb") as f:
            uploaded[remote] = f.read()

    mock_contents.upload.side_effect = fake_upload

    code = _run_upload(str(local), "/content/big.bin")

    assert code == 0
    assert "/content/big.bin.gz" in uploaded
    assert gzip.decompress(uploaded["/content/big.bin.gz"]) == payload

    runtime_instance = files.ColabRuntime.return_value
    runtime_instance.execute_code.assert_called_once()
    exec_code = runtime_instance.execute_code.call_args[0][0]
    assert "'/content/big.bin'" in exec_code
    assert "'/content/big.bin.gz'" in exec_code

    mock_contents.rm.assert_called_once_with("/content/big.bin.gz")


def test_large_file_size_mismatch_fails(
    tmp_path, mock_contents, mock_common_state, monkeypatch
):
    from colab_cli.commands import files

    monkeypatch.setattr(files, "TRANSFER_COMPRESS_ABOVE_BYTES", 10)
    local = tmp_path / "big.bin"
    local.write_bytes(os.urandom(4096))
    mock_contents.list_dir.return_value = {"size": 123}

    code = _run_upload(str(local), "/content/big.bin")

    assert code == 1
    mock_contents.rm.assert_not_called()


def test_no_compress_flag_bypasses_gzip(
    tmp_path, mock_contents, mock_common_state, monkeypatch
):
    from colab_cli.commands import files

    monkeypatch.setattr(files, "TRANSFER_COMPRESS_ABOVE_BYTES", 10)
    local = tmp_path / "big.bin"
    local.write_bytes(os.urandom(4096))

    code = _run_upload(str(local), "/content/big.bin", ("--no-compress",))

    assert code == 0
    mock_contents.upload.assert_called_once_with(str(local), "/content/big.bin")

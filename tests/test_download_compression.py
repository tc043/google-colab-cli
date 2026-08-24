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


def _run_download(remote, local, extra_args=()):
    argv = ["colab", "download", "-s", "test-session", remote, local, *extra_args]
    with patch.object(sys, "argv", argv):
        with pytest.raises(SystemExit) as error:
            main()
    return error.value.code


def test_small_file_downloads_plain(tmp_path, mock_contents):
    remote = "/content/small.bin"
    mock_contents.list_dir.return_value = {"size": 100}
    local = tmp_path / "small.bin"

    code = _run_download(remote, str(local))

    assert code == 0
    mock_contents.download.assert_called_once_with(remote, str(local))
    mock_contents.rm.assert_not_called()


def test_large_file_gzips_on_vm_and_verifies(
    tmp_path, mock_contents, mock_common_state, monkeypatch
):
    from colab_cli.commands import files

    monkeypatch.setattr(files, "TRANSFER_COMPRESS_ABOVE_BYTES", 10)
    payload = os.urandom(4096)
    remote = "/content/big.bin"
    mock_contents.list_dir.return_value = {"size": len(payload)}

    def fake_download(remote_path, local_path):
        with gzip.open(local_path, "wb", compresslevel=6) as dst:
            dst.write(payload)

    mock_contents.download.side_effect = fake_download
    local = tmp_path / "big.bin"

    code = _run_download(remote, str(local))

    assert code == 0
    contents_args = mock_contents.download.call_args[0]
    assert contents_args[0] == "/content/big.bin.gz"
    assert contents_args[1].endswith(".gz")

    runtime_instance = files.ColabRuntime.return_value
    runtime_instance.execute_code.assert_called_once()
    exec_code = runtime_instance.execute_code.call_args[0][0]
    assert "'/content/big.bin'" in exec_code
    assert "'/content/big.bin.gz'" in exec_code

    assert local.read_bytes() == payload
    mock_contents.rm.assert_called_once_with("/content/big.bin.gz")


def test_large_file_size_mismatch_fails(
    tmp_path, mock_contents, mock_common_state, monkeypatch
):
    from colab_cli.commands import files

    monkeypatch.setattr(files, "TRANSFER_COMPRESS_ABOVE_BYTES", 10)
    remote = "/content/big.bin"
    mock_contents.list_dir.return_value = {"size": 4096}

    def fake_download(remote_path, local_path):
        with gzip.open(local_path, "wb") as dst:
            dst.write(b"wrong bytes")

    mock_contents.download.side_effect = fake_download
    local = tmp_path / "big.bin"

    code = _run_download(remote, str(local))

    assert code == 1
    mock_contents.rm.assert_not_called()


def test_no_compress_flag_bypasses_gzip(
    tmp_path, mock_contents, mock_common_state, monkeypatch
):
    from colab_cli.commands import files

    monkeypatch.setattr(files, "TRANSFER_COMPRESS_ABOVE_BYTES", 10)
    remote = "/content/big.bin"
    mock_contents.list_dir.return_value = {"size": 4096}
    local = tmp_path / "big.bin"

    code = _run_download(remote, str(local), ("--no-compress",))

    assert code == 0
    mock_contents.download.assert_called_once_with(remote, str(local))

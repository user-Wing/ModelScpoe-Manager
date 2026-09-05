from __future__ import annotations

import argparse
import base64
import http.client
from pathlib import Path
import sys
import time
from urllib.parse import quote
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modelscope_manager.service import ModelScopeService, Repository
from modelscope_manager.webdav_server import ModelScopeWebDAV


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure a real ModelScope dataset directory listing.")
    parser.add_argument("repo_id", nargs="?", default="ARXChem/Animations-List")
    args = parser.parse_args()

    service = ModelScopeService("", require_token=False)
    started = time.perf_counter()
    repo = Repository(args.repo_id, "dataset", "public")
    entries = service.list_entries(repo)
    elapsed = time.perf_counter() - started
    unique_paths = {entry.path for entry in entries}

    print(f"repository={args.repo_id}")
    print(f"entries={len(entries)} unique_paths={len(unique_paths)} elapsed={elapsed:.2f}s")
    if len(entries) != len(unique_paths):
        print("result=FAILED duplicate paths returned")
        return 1

    server = ModelScopeWebDAV(lambda: None, "127.0.0.1", 0, "test", "test", lambda: [repo])
    server.start()
    connection = http.client.HTTPConnection("127.0.0.1", server._server.server_address[1], timeout=30)
    mount_name = server.public_mount_name(repo)
    started = time.perf_counter()
    try:
        credentials = base64.b64encode(b"test:test").decode("ascii")
        connection.request(
            "PROPFIND",
            "/dav/public/" + quote(mount_name, safe="") + "/",
            headers={"Authorization": "Basic " + credentials, "Depth": "1", "Content-Length": "0"},
        )
        response = connection.getresponse()
        body = response.read()
    finally:
        connection.close()
        server.stop()
    href_count = len(ET.fromstring(body).findall(".//{DAV:}href")) if response.status == 207 else 0
    print(
        f"webdav_status={response.status} direct_children={max(0, href_count - 1)} "
        f"elapsed={time.perf_counter() - started:.2f}s"
    )
    if response.status != 207:
        print("result=FAILED WebDAV PROPFIND")
        return 1
    print("result=OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

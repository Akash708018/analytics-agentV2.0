"""Spec-driven Prism fixture. Its owned process is always stopped."""

import json
import shutil
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"


@pytest.fixture(scope="session")
def contract():
    result = subprocess.run(
        ["node", str(FRONTEND / "dev/read_contract.cjs")],
        cwd=ROOT, capture_output=True, text=True, timeout=15, check=True,
    )
    return json.loads(result.stdout)


@pytest.fixture(scope="session")
def prism_url(tmp_path_factory):
    prism = FRONTEND / "node_modules/.bin/prism"
    if shutil.which("node") is None or not prism.exists():
        pytest.fail("Install Node and run npm ci --prefix frontend --ignore-scripts before testing.")
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    log = tmp_path_factory.mktemp("prism") / "server.log"
    with log.open("w+") as output:
        process = subprocess.Popen(
            [str(prism), "mock", str(ROOT / "docs/api/openapi.yaml"),
             "--host", "127.0.0.1", "--port", str(port)],
            cwd=ROOT, stdout=output, stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 20
            with httpx.Client(timeout=0.5, trust_env=False) as client:
                while time.monotonic() < deadline and process.poll() is None:
                    try:
                        if client.get(url + "/health").status_code == 200:
                            break
                    except httpx.RequestError:
                        pass  # The owned server is still starting; bounded by deadline.
                    time.sleep(0.1)
                else:
                    output.seek(0)
                    pytest.fail("Prism did not start:\n" + output.read()[-4000:])
            yield url
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)

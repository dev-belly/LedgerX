"""Verify the bundled workbench against a real loopback API in Chrome."""

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest


def wait_until(check):
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.1)
    pytest.fail("Browser condition did not become true")


def test_import_retry_and_journal_detail(tmp_path):
    executable = shutil.which("chromedriver")
    driver_home = os.environ.get("CHROMEWEBDRIVER")
    if executable is None and driver_home:
        candidate = Path(driver_home) / "chromedriver"
        if candidate.is_file():
            executable = str(candidate)
    if sys.platform != "linux" or executable is None:
        pytest.skip("Linux Chrome and ChromeDriver are required for this browser regression")

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        driver_port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{driver_port}"
    journal = tmp_path / "journal.jsonl"
    command = [sys.executable, "-m", "ledgerx.cli", "web", str(journal), "--port", "0"]
    server = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    driver = subprocess.Popen(
        [executable, f"--port={driver_port}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    def call(path, payload=None):
        request = urllib.request.Request(
            base + path,
            data=json.dumps(payload).encode("utf-8") if payload is not None else None,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)["value"]

    def driver_ready():
        try:
            return call("/status")["ready"]
        except urllib.error.URLError:
            return False

    try:
        assert server.stdout is not None
        address = server.stdout.readline().strip()
        if not address:
            assert server.stderr is not None
            pytest.fail(server.stderr.read())
        assert address.startswith("http://127.0.0.1:")
        wait_until(driver_ready)
        session = call(
            "/session",
            {
                "capabilities": {
                    "alwaysMatch": {
                        "browserName": "chrome",
                        "goog:chromeOptions": {
                            "args": ["--headless=new", "--no-sandbox", "--disable-dev-shm-usage"],
                            "mobileEmulation": {
                                "deviceMetrics": {"width": 390, "height": 844, "pixelRatio": 1}
                            },
                        },
                    }
                }
            },
        )["sessionId"]
        path = "/session/" + session
        call(path + "/url", {"url": address})

        def run(script):
            return call(path + "/execute/sync", {"script": script, "args": []})

        wait_until(lambda: run("return !document.querySelector('#export').disabled"))
        run("document.querySelector('#sample').click()")
        assert run("return !document.querySelector('#submit').disabled")
        run("""
            const file = new File(["ignored"], "pending.jsonl");
            const transfer = new DataTransfer();
            transfer.items.add(file);
            const input = document.querySelector("#feed");
            input.files = transfer.files;
            const read = File.prototype.arrayBuffer;
            File.prototype.arrayBuffer = () => new Promise((_, reject) => {
                window.rejectFeed = reject;
            });
            try {
                input.dispatchEvent(new Event("change"));
            } finally {
                File.prototype.arrayBuffer = read;
            }
        """)
        assert run("return document.querySelector('#submit').disabled")
        run('window.rejectFeed(new Error("Read failed"))')
        message_script = "return document.querySelector('#message').textContent"
        wait_until(lambda: run(message_script).startswith("Read failed"))
        assert run("return document.querySelector('#submit').disabled")
        assert run("return document.querySelector('#draft').value.includes('fund-001')")
        assert run("return document.querySelector('#event-count').textContent") == "0"
        run("document.querySelector('#sample').click(); document.querySelector('#submit').click()")
        wait_until(lambda: run("return document.querySelector('#event-count').textContent === '3'"))
        assert run("return document.querySelector('#book').textContent") == "$10,038.60"
        count_script = "return document.querySelector('#batch-count').textContent"
        assert run(count_script) == "3 appended · 1 reused"
        run("document.querySelector('#submit').click()")
        wait_until(lambda: run(count_script) == "0 appended · 4 reused")
        run("document.querySelector('#records button').click()")
        assert run("return document.querySelector('#entry-title').textContent") == "fill-002"
        assert run("return document.querySelectorAll('#postings tr').length") >= 2
        assert run("return document.documentElement.scrollWidth <= window.innerWidth")
    finally:
        for process in (driver, server):
            process.terminate()
            process.wait(timeout=10)

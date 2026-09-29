"""Run browser QA on an isolated evaluation database and clean up its server."""
import argparse
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path, help="Existing evaluation output directory")
    args = parser.parse_args()
    out = args.output.resolve()
    if not (out / "evaluation.db").is_file():
        raise SystemExit("evaluation.db missing")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), MW_DB_PATH=str(out / "evaluation.db"),
               MW_DAEMON_TOKEN=secrets.token_hex(24), PYTHONUTF8="1")
    url = f"http://127.0.0.1:{port}"
    with (out / "qa-runtime.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen([sys.executable, "-m", "agent_knowledge_bridge.daemon", "--port", str(port)],
            env=env, cwd=ROOT, stdout=log, stderr=log,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            request = urllib.request.Request(url + "/v1/health", headers={"Authorization":"Bearer " + env["MW_DAEMON_TOKEN"]})
            for _ in range(60):
                try:
                    with urllib.request.urlopen(request, timeout=1):
                        break
                except OSError:
                    if process.poll() is not None:
                        raise RuntimeError("QA runtime exited; inspect qa-runtime.log")
                    time.sleep(.2)
            else:
                raise RuntimeError("QA runtime startup timed out")
            env.update(MW_QA_URL=url, MW_QA_TOKEN=env["MW_DAEMON_TOKEN"],
                       MW_QA_BROWSER_PATH=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                       MW_QA_SCREENSHOT_PATH=str(out / "dashboard.png"),
                       MW_QA_DETAIL_PATH=str(out / "knowledge-detail.png"))
            subprocess.run([r"C:\Node.js\node.exe", str(ROOT / "scripts" / "qa_reuse_dashboard.cjs")],
                           env=env, cwd=ROOT, check=True, timeout=60)
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    main()

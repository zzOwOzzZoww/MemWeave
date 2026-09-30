"""Record a reproducible README GIF with isolated hooks, UI and synthetic learning.

Requires the Runtime extras, Pillow, Node.js and Playwright. No provider or real
Agent configuration is used. Pass --assets docs/assets to publish the GIFs.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

PROJECT = "demo-project"
RULE = "记住这个项目约定：Python 依赖和测试统一使用 uv；验证改动前运行 uv run pytest tests -q。"
QUESTION = "这个项目的 Python 依赖和测试使用什么命令？"
TITLE = "Python 测试约定：使用 uv"
DURATIONS = [4000, 5000, 4000, 5000, 6000, 4000]


def synthetic_reviewer(text):
    if RULE not in text:
        raise ValueError("Unexpected synthetic source turn")
    return {"proposals": [{
        "title": TITLE, "content": RULE, "knowledge_type": "procedure", "scope": "project",
        "search_terms": "Python uv pytest 依赖 测试 dependency test",
        "source_role": "user", "source_quotes": [RULE], "evidence_event_ids": [],
    }]}


def serve():
    import uvicorn
    from agent_knowledge_bridge.daemon import create_app
    app = create_app(database_path=Path(os.environ["MW_DB_PATH"]),
                     api_token=os.environ["MW_DAEMON_TOKEN"], reviewer=synthetic_reviewer)
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ["MW_DEMO_PORT"]), log_level="warning")


def invoke(phase):
    agent = "codex" if phase == "capture" else "claude-code"
    payload = {"hook_event_name": "Stop" if phase == "capture" else "UserPromptSubmit",
               "session_id": agent + "-demo-session", "turn_id": "demo-" + phase,
               "cwd": os.environ["MW_DEMO_WORKSPACE"]}
    if phase == "capture":
        payload["transcript_path"] = os.environ["MW_DEMO_TRANSCRIPT"]
    else:
        payload["prompt"] = "明天北京会下雨吗？" if phase == "unrelated" else QUESTION
    result = subprocess.run([sys.executable, "-X", "utf8", "-m",
        "agent_knowledge_bridge.hooks.generic_learning_hook", "--agent", agent],
        input=json.dumps(payload), capture_output=True, text=True, encoding="utf-8", timeout=20, check=True)
    if result.stderr:
        raise RuntimeError(result.stderr)
    print(json.dumps(json.loads(result.stdout), ensure_ascii=False))


def encode_gifs(output, assets):
    from PIL import Image, ImageStat
    report = {}
    for language in ("zh", "en"):
        frames = []
        for index in range(len(DURATIONS)):
            with Image.open(output / f"{language}-{index + 1:02}.png") as source:
                frame = source.convert("RGB")
            if frame.size != (1120, 760) or max(ImageStat.Stat(frame).stddev) < 10:
                raise ValueError("Unexpected or blank demo frame")
            frames.append(frame)
        if len({frame.tobytes() for frame in frames}) != len(frames):
            raise ValueError("Demo stages must have different visible states")
        # One palette avoids distracting colour changes between UI screenshots.
        sheet = Image.new("RGB", (1120, 760 * len(frames)))
        for index, frame in enumerate(frames):
            sheet.paste(frame, (0, 760 * index))
        palette = sheet.quantize(colors=256)
        encoded = [frame.quantize(palette=palette, dither=Image.Dither.NONE) for frame in frames]
        destination = output / f"memweave-demo.{language}.gif"
        encoded[0].save(destination, save_all=True, append_images=encoded[1:],
            duration=DURATIONS, loop=0, optimize=True, disposal=2)
        with Image.open(destination) as animation:
            if animation.n_frames != len(frames):
                raise ValueError("GIF lost a demo stage")
            duration = 0
            for index in range(animation.n_frames):
                animation.seek(index)
                duration += animation.info["duration"]
            if duration != sum(DURATIONS) or animation.info.get("loop") != 0:
                raise ValueError("Unexpected GIF playback metadata")
        if destination.stat().st_size > 4_000_000:
            raise ValueError("README GIF exceeds 4 MB")
        if assets:
            assets.mkdir(parents=True, exist_ok=True)
            shutil.copy2(destination, assets / destination.name)
        report[language] = {"frames": len(frames), "duration_ms": duration,
                            "bytes": destination.stat().st_size, "size": [1120, 760]}
        preview = Image.new("RGB", (560 * 2, 380 * 3))
        for index, frame in enumerate(frames):
            preview.paste(frame.resize((560, 380)), ((index % 2) * 560, (index // 2) * 380))
        preview.save(output / f"contact-sheet.{language}.png")
    return report


def record(args):
    from agent_knowledge_bridge.provider import atomic_json
    from agent_knowledge_bridge.store import KnowledgeStore

    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="memweave-readme-demo-") as folder:
        temp = Path(folder)
        database = temp / "demo.db"
        transcript = temp / "codex.jsonl"
        atomic_json(temp / "home" / "config.json", {"default_project": PROJECT})
        rows = [{"type": "response_item", "payload": {"type": "message", "role": "user",
                 "content": [{"type": "input_text", "text": RULE}]}},
                {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                 "content": [{"type": "output_text", "text": "已记录项目约定。"}]}}]
        transcript.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
        store = KnowledgeStore(database)
        for agent, name, adapter in (("codex", "Codex", "codex-hook"),
                                     ("claude-code", "Claude Code", "claude-hook")):
            store.register_agent(agent_id=agent, display_name=name, adapter_type=adapter,
                                 installed=False, detected_by=["synthetic-demo"])
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        token = secrets.token_hex(24)
        url = f"http://127.0.0.1:{port}"
        environment = {key: value for key, value in os.environ.items()
            if not key.startswith(("MW_", "AKB_", "MEMWEAVE", "OPENAI_", "DEEPSEEK_"))}
        environment.update({"PYTHONUTF8": "1", "PYTHONPATH": str(ROOT / "src"),
            "MEMWEAVE_HOME": str(temp / "home"), "MW_DB_PATH": str(database),
            "MW_PROJECT_KEY": PROJECT, "MW_DAEMON_URL": url, "MW_DAEMON_TOKEN": token,
            "CODEX_HOME": str(temp / "codex"), "CLAUDE_CONFIG_DIR": str(temp / "claude"),
            "GEMINI_CLI_HOME": str(temp / "gemini"), "WORKBUDDY_CONFIG_DIR": str(temp / "workbuddy"),
            "CODEBUDDY_CONFIG_DIR": str(temp / "workbuddy"), "MW_DEMO_PORT": str(port),
            "MW_DEMO_WORKSPACE": str(temp), "MW_DEMO_TRANSCRIPT": str(transcript),
            "MW_DEMO_OUTPUT": str(output), "MW_DEMO_PYTHON": sys.executable,
            "MW_DEMO_FIXTURE": json.dumps({"rule": RULE, "question": QUESTION, "title": TITLE, "project": PROJECT})})
        if args.node_modules:
            environment["NODE_PATH"] = str(args.node_modules.resolve())
        if args.browser:
            environment["MW_DEMO_BROWSER"] = str(args.browser.resolve())
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        with (output / "runtime.log").open("wb") as log:
            process = subprocess.Popen([sys.executable, "-X", "utf8", str(Path(__file__).resolve()), "--serve"],
                env=environment, cwd=ROOT, stdout=log, stderr=log, creationflags=flags)
            try:
                for _ in range(100):
                    if process.poll() is not None:
                        raise RuntimeError("Isolated demo Runtime exited; see runtime.log")
                    try:
                        request = urllib.request.Request(url + "/v1/health", headers={"Authorization": "Bearer " + token})
                        with urllib.request.urlopen(request, timeout=1) as response:
                            if json.load(response)["status"] == "ok":
                                break
                    except OSError:
                        time.sleep(.1)
                else:
                    raise RuntimeError("Isolated demo Runtime did not start")
                result = subprocess.run(["node", str(ROOT / "scripts" / "record_readme_demo.cjs")],
                    env=environment, cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                    timeout=120, creationflags=flags)
                (output / "browser.log").write_text(result.stdout + result.stderr, encoding="utf-8")
                if result.returncode:
                    raise RuntimeError(result.stderr or result.stdout)
                report = json.loads(result.stdout)
                with store._connect() as db:
                    records = db.execute("SELECT source_agent,source_session,status,verified_count FROM knowledge_records").fetchall()
                    if len(records) != 1 or tuple(records[0]) != ("codex", "codex-demo-session", "active", 1):
                        raise ValueError("Demo did not preserve source or approval state")
            finally:
                process.terminate()
                process.wait(timeout=10)
        report["animations"] = encode_gifs(output, args.assets.resolve() if args.assets else None)
        report["synthetic_session"] = True
        report["synthetic_reviewer"] = True
        report["paid_api_calls"] = 0
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "outputs" / "readme-demo")
    parser.add_argument("--assets", type=Path)
    parser.add_argument("--node-modules", type=Path)
    parser.add_argument("--browser", type=Path)
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--invoke", choices=("capture", "candidate", "active", "unrelated"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.serve:
        serve()
    elif args.invoke:
        invoke(args.invoke)
    else:
        record(args)


if __name__ == "__main__":
    main()

from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from importlib.resources import files
_source = files("agent_knowledge_bridge.hooks").joinpath('codex_learning_hook.py')
exec(compile(_source.read_text(encoding="utf-8"), str(_source), "exec"), globals())

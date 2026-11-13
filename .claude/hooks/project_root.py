"""hook が検査するプロジェクトを決め、そのプロジェクトの検査ツールを読み込めるようにする。

Claude Code が worktree（`.claude/worktrees/…` など）で作業すると、hook に渡る CLAUDE_PROJECT_DIR は
元のフォルダを指したままになる。元のフォルダを基準にすると、worktree のファイルは「本番コードではない場所」に
見え、何も止まらない。そこで、編集されるファイル（または作業中のフォルダ）から上にたどり、検査ツールと台帳を
持つ最も近いフォルダを、検査するプロジェクトとする。探すのは元のフォルダの内側だけ（外のファイルは見ない）。
"""
import os
import sys
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent


def project_root(start: str | os.PathLike | None = None) -> Path:
    base = Path(os.environ.get("CLAUDE_PROJECT_DIR") or HOOKS_DIR.parents[1]).resolve()
    if start:
        path = Path(start)
        if not path.is_absolute():
            path = base / path
        path = path.resolve()
        for candidate in (path, *path.parents):
            if candidate != base and base not in candidate.parents:
                break  # 元のフォルダの外まで来た
            if (candidate / "tools" / "tracekit.py").is_file() and (candidate / "trace").is_dir():
                return candidate
    return base


def use_tools_of(root: Path) -> None:
    """そのプロジェクトの検査ツールを読み込む（worktree と元のフォルダで版が違うことがある）。"""
    tools = root / "tools"
    if not (tools / "tracekit.py").is_file():
        tools = HOOKS_DIR.parents[1] / "tools"
    sys.path.insert(0, str(tools))

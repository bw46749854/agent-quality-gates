"""配布用の ZIP を作る道具のテスト（配布物には入らない）。

実行: python3 -m unittest discover -s tools/tests
"""
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools"))
import make_dist  # noqa: E402


@unittest.skipUnless(shutil.which("git") and (REPO / ".git").exists(), "git の管理下で実行します")
class MakeDistTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_archive_of_a_commit_holds_exactly_what_the_commit_holds(self):
        archive = self.tmp / "t.zip"
        make_dist.build("HEAD", archive)
        self.assertEqual(make_dist.inspect(archive, make_dist.committed_blobs("HEAD")), [])
        with zipfile.ZipFile(archive) as bundle:
            names = bundle.namelist()
        self.assertIn("tools/tracekit.py", names)
        self.assertNotIn("tools/make_dist.py", names)  # 配る人のための道具は、配布物に入れない

    def test_extraction_keeps_the_commit_hook_executable(self):
        archive = self.tmp / "t.zip"
        make_dist.build("HEAD", archive)
        make_dist.extract(archive, self.tmp / "out")
        mode = (self.tmp / "out" / "tools" / "git-hooks" / "pre-commit").stat().st_mode
        self.assertTrue(mode & 0o100)

    def test_folder_zipped_by_hand_is_rejected(self):
        # レビューに渡した ZIP で実際に起きたこと: .git と worktree とキャッシュが入り、実行権限がなく、中身は古い版
        archive = self.tmp / "by-hand.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr(".git/config", "[remote]\n")
            bundle.writestr(".claude/worktrees/x/tools/tracekit.py", "新しい版\n")
            bundle.writestr("tools/__pycache__/tracekit.cpython-313.pyc", "")
            bundle.writestr("tools/tracekit.py", "古い版\n")
            bundle.writestr("tools/git-hooks/pre-commit", "#!/bin/sh\n")
        problems = "\n".join(make_dist.inspect(archive, make_dist.committed_blobs("HEAD")))
        for expected in (".git/config", "worktrees", "__pycache__", "実行権限",
                         "コミットの内容と違います（古い版が混ざっている可能性）: tools/tracekit.py",
                         "コミットにあるのに、入っていません: README.md"):
            self.assertIn(expected, problems)


if __name__ == "__main__":
    unittest.main()

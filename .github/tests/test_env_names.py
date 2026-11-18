# goofish.py 读新环境变量名的测试（审计 2026-11-18 #168）。
#
# 跑法（在仓库根）：
#   python3 -I -B -m unittest discover -s .github/tests
# （不加 -t .：.github 不是合法的包名，加了 unittest 会报 Start directory is not importable。）
#
# 只用标准库；不联网、不起浏览器：只跑 monitor-list 和 doctor，load_playwright 换成「没装」的桩。
# HOME、XDG_*、MIYU_* 都指到临时目录。
# 放在 .github/tests/ 而不是 tests/：打包会跳过 .github，测试不会进包。

import importlib.util
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "goofish.py"


def load():
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("goofish_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Sandbox(unittest.TestCase):
    """每条测试一个临时目录，环境里去掉所有 MIYU_*。脚本 import 时就读缓存目录，所以各测试先改环境再 load()。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("MIYU_", "XDG_"))}
        env.update(HOME=str(root / "home"), XDG_CONFIG_HOME=str(root / "cfg"),
                   XDG_CACHE_HOME=str(root / "xdg-cache"), XDG_STATE_HOME=str(root / "xdg-state"),
                   XDG_DATA_HOME=str(root / "xdg-data"))
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.root = root

    def load(self):
        mod = load()

        def no_playwright():
            raise mod.Fail("测试里不起浏览器", "")

        mod.load_playwright = no_playwright
        # 没读到缓存目录时脚本会建临时目录，测完删掉
        self.addCleanup(lambda: mod.cache_dir.temporary and mod.shutil.rmtree(mod.CACHE, ignore_errors=True))
        return mod

    def call(self, mod, args):
        """照 main() 跑一次：stdin 喂 JSON，返回 (退出码, 输出)。"""
        out = io.StringIO()
        code = 0
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(args))), redirect_stdout(out):
            try:
                mod.main()
            except SystemExit as exc:
                code = exc.code or 0
        return code, out.getvalue()


class EnvNames(Sandbox):
    # 审计 #168：核心 FH-6 以后只给新名字 MIYU_CACHE_DIR、MIYU_STATE_DIR，不再给 MIYU_SCRIPT_*。
    # 脚本三处只读旧名：import 时的 CACHE、doctor 里判断缓存目录设没设、state_dir()。

    def test_cache_dir_new_name(self):
        # 浏览器会话快照应存在 MIYU_CACHE_DIR。现在 CACHE 读不到，退成一次性的临时目录。
        cache = self.root / "cache"
        os.environ["MIYU_CACHE_DIR"] = str(cache)
        self.assertNotIn("MIYU_SCRIPT_CACHE_DIR", os.environ)
        mod = self.load()
        self.assertEqual(mod.cache_dir(), str(cache))
        self.assertFalse(mod.cache_dir.temporary)
        self.assertEqual(mod.session_path(), str(cache / "session.json"))

    def test_doctor_sees_new_cache_name(self):
        # doctor 应报缓存目录就是 MIYU_CACHE_DIR、不警告。现在报「没设 MIYU_SCRIPT_CACHE_DIR」。
        cache = self.root / "cache"
        os.environ["MIYU_CACHE_DIR"] = str(cache)
        mod = self.load()
        result = mod.do_doctor({"action": "doctor"})
        row = next(c for c in result["checks"] if c["name"] == "缓存目录")
        self.assertEqual(row["detail"], str(cache), row)
        self.assertFalse(row["warn"], row)

    def test_state_dir_new_name(self):
        # 监控存在 MIYU_STATE_DIR。预先放一个监控，monitor-list 应列出来。现在报「没有状态目录」。
        state = self.root / "state"
        os.environ["MIYU_STATE_DIR"] = str(state)
        self.assertNotIn("MIYU_SCRIPT_STATE_DIR", os.environ)
        mod = self.load()
        (state / "watches").mkdir(parents=True)
        ticket = {"keyword": "RTX 4060", "created": 1700000000, "last_query": None, "request": {}, "seen": ["1", "2"]}
        (state / "watches" / (mod.watch_key("RTX 4060") + ".json")).write_text(json.dumps(ticket), encoding="utf-8")
        self.assertEqual(mod.state_dir(), str(state))
        code, out = self.call(mod, {"action": "monitor-list", "format": "json"})
        self.assertEqual(code, 0, out)
        self.assertEqual([w["keyword"] for w in json.loads(out)["watches"]], ["RTX 4060"])


if __name__ == "__main__":
    unittest.main()

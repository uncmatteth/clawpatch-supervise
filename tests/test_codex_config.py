from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from clawpatch_supervise.clawpatch_external import _supervisor_clawpatch_config


class SupervisedCodexConfigTests(unittest.TestCase):
    def test_code_mode_off(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repository"
            state = repo / ".clawpatch"
            state.mkdir(parents=True)
            repository_config = {
                "schemaVersion": 1,
                "stateDir": ".clawpatch",
                "provider": {
                    "name": "codex",
                    "model": "gpt-test",
                    "codexConfig": {
                        "model_context_window": 123_456,
                        "features.code_mode_host": True,
                    },
                },
            }
            config_path = state / "config.json"
            config_path.write_text(json.dumps(repository_config), encoding="utf-8")
            temporary_root = root / "owned-run"
            temporary_root.mkdir()

            generated_path = _supervisor_clawpatch_config(repo, temporary_root)

            self.assertEqual(json.loads(config_path.read_text(encoding="utf-8")), repository_config)
            self.assertIsNotNone(generated_path)
            generated = json.loads(generated_path.read_text(encoding="utf-8"))
            self.assertEqual(generated["provider"]["model"], "gpt-test")
            self.assertEqual(
                generated["provider"]["codexConfig"]["model_context_window"], 123_456
            )
            self.assertIs(
                generated["provider"]["codexConfig"]["features.code_mode_host"], False
            )


if __name__ == "__main__":
    unittest.main()

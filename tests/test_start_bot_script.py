import unittest
from pathlib import Path


class StartBotPythonSelectionTests(unittest.TestCase):
    def test_start_script_uses_system_python_by_default(self):
        script = Path("start_bot.sh").read_text(encoding="utf-8")

        self.assertIn('PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3}"', script)
        self.assertIn('nohup "$PYTHON_BIN" run_agents.py', script)
        self.assertIn('nohup "$PYTHON_BIN" dashboard.py', script)

    def test_process_detection_ignores_python_commands_that_only_mention_script_names(self):
        script = Path("start_bot.sh").read_text(encoding="utf-8")

        self.assertIn('$2 ~ /[Pp]ython/ && $3 ~ /(^|\\/)run_agents\\.py$/', script)
        self.assertIn('$2 ~ /[Pp]ython/ && $3 ~ /(^|\\/)dashboard\\.py$/', script)


if __name__ == "__main__":
    unittest.main()

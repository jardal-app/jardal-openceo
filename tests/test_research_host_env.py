"""Host launch must parse the same actual dotenv as Settings, without shell eval."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class HostEnvTests(unittest.TestCase):
    def test_references_quotes_and_command_text_are_data(self):
        for name in ("openceo-core-host.sh", "openceo-core-host-runner.sh"):
            with self.subTest(launcher=name):
                self._check_launcher(name)

    def _check_launcher(self, name):
        script = Path(__file__).resolve().parents[1] / "scripts" / name
        if name.endswith("runner.sh"):
            prefix = script.read_text().split('\nload_env\n', 1)[0]
        else:
            prefix = script.read_text().rsplit('\ncase "', 1)[0]
            self.assertNotIn('case "', prefix)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            envfile, canary = root / ".env", root / "must-not-exist"
            envfile.write_text('TEST_REFERENCE=synthetic-value\nRESEARCH_DEEP_API_KEY=${TEST_REFERENCE}\n'
                'TEST_QUOTES="hello #world"\nTEST_MULTILINE="a\\nb"\nTEST_COMMAND=$(touch ' + str(canary) + ')\n')
            probe = prefix + '\nENV_FILE="$1"\nVENV_PY="$2"\nload_env\n"$VENV_PY" -c '\
                + "'import os,json; print(json.dumps({k:os.environ[k] for k in [\"RESEARCH_DEEP_API_KEY\",\"TEST_QUOTES\",\"TEST_MULTILINE\",\"TEST_COMMAND\"]}))'"
            completed = subprocess.run(["bash", "-c", probe, str(script), str(envfile), sys.executable],
                                       capture_output=True, text=True, check=True)
            values = json.loads(completed.stdout)
            self.assertEqual(values["RESEARCH_DEEP_API_KEY"], "synthetic-value")
            self.assertEqual(values["TEST_QUOTES"], "hello #world")
            self.assertEqual(values["TEST_MULTILINE"], "a\nb")
            self.assertTrue(values["TEST_COMMAND"].startswith("$(touch "))
            self.assertFalse(canary.exists())

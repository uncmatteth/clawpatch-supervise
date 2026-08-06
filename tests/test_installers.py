from pathlib import Path
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class InstallerContractTests(unittest.TestCase):
    def test_linux_installer_bootstraps_latest_clawpatch_only_when_missing(self) -> None:
        script = (REPOSITORY_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")

        condition = "if ! command -v clawpatch >/dev/null 2>&1; then"
        install = "npm install --global clawpatch@latest"

        self.assertIn(condition, script)
        self.assertEqual(script.count(install), 1)
        self.assertLess(script.index(condition), script.index(install))
        self.assertIn('command -v npm >/dev/null 2>&1', script)
        self.assertGreater(script.rindex("command -v clawpatch"), script.index(install))

    def test_windows_installer_bootstraps_latest_clawpatch_only_when_missing(self) -> None:
        script = (REPOSITORY_ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")

        lookup = "$clawpatch = Get-Command clawpatch -ErrorAction SilentlyContinue"
        condition = "if ($null -eq $clawpatch)"
        install = "install --global clawpatch@latest"

        self.assertIn(lookup, script)
        self.assertIn(condition, script)
        self.assertEqual(script.count(install), 1)
        self.assertLess(script.index(condition), script.index(install))
        self.assertIn("Get-Command npm -ErrorAction SilentlyContinue", script)
        self.assertGreater(script.rindex("Get-Command clawpatch"), script.index(install))

    def test_linux_installer_bootstraps_latest_clawhub_only_when_missing(self) -> None:
        script = (REPOSITORY_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")

        condition = "if command -v clawhub >/dev/null 2>&1; then"
        install = 'npm install --prefix "$clawhub_root" --no-fund --no-audit clawhub@latest'

        self.assertIn(condition, script)
        self.assertEqual(script.count(install), 1)
        self.assertLess(script.index(condition), script.index(install))
        self.assertIn('command -v npm >/dev/null 2>&1', script)
        self.assertIn('"$clawhub_command" --cli-version', script)

    def test_windows_installer_bootstraps_latest_clawhub_only_when_missing(self) -> None:
        script = (REPOSITORY_ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")

        lookup = "$clawHubCommand = Get-Command clawhub.cmd, clawhub.exe, clawhub"
        condition = "if ($null -eq $clawHubCommand)"
        install = "install --prefix $clawHubRoot --no-fund --no-audit clawhub@latest"

        self.assertIn(lookup, script)
        self.assertIn(condition, script)
        self.assertEqual(script.count(install), 1)
        self.assertLess(script.index(condition), script.index(install))
        self.assertIn("Get-Command npm.cmd, npm.exe, npm", script)
        self.assertIn("& $clawHubPath --cli-version", script)


if __name__ == "__main__":
    unittest.main()

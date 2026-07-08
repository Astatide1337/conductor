"""Tests for CLI entrypoint."""

from typer.testing import CliRunner

from conductor.cli import app

runner = CliRunner()


class TestVersionCommand:
    def test_version(self):
        result = runner.invoke(app, ["version"])
        assert result.exit_code == 0
        assert "Astatide Conductor" in result.stdout


class TestHelp:
    def test_help(self):
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "Conductor" in result.stdout

    def test_run_help(self):
        result = runner.invoke(app, ["run", "--help"])
        assert result.exit_code == 0
        assert "host" in result.stdout.lower() or "Host" in result.stdout
        assert "port" in result.stdout.lower() or "Port" in result.stdout
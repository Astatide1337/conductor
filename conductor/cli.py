"""CLI entrypoint via Typer, following the agent-gateway deferred-import pattern."""

import typer

app = typer.Typer(name="conductor", help="Astatide Conductor — objective orchestrator")


@app.command()
def run(
    host: str = typer.Option("0.0.0.0", envvar="CONDUCTOR_SERVICE__HOST", help="Bind host"),
    port: int = typer.Option(8093, envvar="CONDUCTOR_SERVICE__PORT", help="Bind port"),
    config_path: str | None = typer.Option(None, envvar="CONDUCTOR_CONFIG", help="YAML config path"),
):
    """Run the Conductor server."""
    from conductor.config import load_config
    from conductor.server import run_server

    cfg = load_config(yaml_path=config_path, cli_host=host, cli_port=port)
    run_server(cfg)


@app.command()
def version():
    """Show version."""
    from conductor import VERSION

    print(f"Astatide Conductor v{VERSION}")


@app.command()
def doctor():
    """Check dependencies and health."""
    from conductor import VERSION
    from conductor.config import load_config
    from conductor.storage import ConductorStorage
    from conductor.logging import setup_logging

    cfg = load_config()
    setup_logging(cfg.observability.log_level, cfg.observability.log_format)
    print(f"Astatide Conductor v{VERSION}")
    print(f"  Environment: {cfg.environment}")
    print(f"  Auth mode: {cfg.auth.mode}")
    print(f"  SQLite: {cfg.storage.sqlite_path}")

    try:
        storage = ConductorStorage(cfg.storage.sqlite_path)
        storage.connect()
        print(f"  Storage: OK (schema ready)")
    except Exception as e:
        print(f"  Storage: FAILED — {e}")


if __name__ == "__main__":
    app()
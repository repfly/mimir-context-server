"""Minimal CLI for the Mimir remote proxy client."""

from __future__ import annotations

import logging
import sys

import typer

app = typer.Typer(
    name="mimir-client",
    help="Mimir Client — connect your IDE to a remote Mimir context server.",
    no_args_is_help=True,
)


@app.command()
def serve(
    remote: str = typer.Argument(
        ...,
        help="URL of the remote Mimir HTTP server (e.g. http://team-server:8421)",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging"),
) -> None:
    """Start a local MCP proxy that forwards to a remote Mimir server.

    Your IDE connects to this proxy via stdio. All queries are forwarded
    to the remote HTTP server over the network.

    \b
    Example:
      mimir-client serve http://team-server:8421
    """
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )

    from mimir_client.proxy import run_remote_mcp
    run_remote_mcp(remote)


@app.command()
def health(
    remote: str = typer.Argument(
        ...,
        help="URL of the remote Mimir HTTP server (e.g. http://team-server:8421)",
    ),
) -> None:
    """Check if a remote Mimir server is reachable and show its status."""
    import asyncio

    async def _check():
        import aiohttp
        url = remote.rstrip("/") + "/api/v1/health"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        typer.echo(f"Status:     {data.get('status', '?')}")
                        typer.echo(f"Workspace:  {data.get('workspace', '?')}")
                        typer.echo(f"Nodes:      {data.get('graph_nodes', '?')}")
                        typer.echo(f"Edges:      {data.get('graph_edges', '?')}")
                    else:
                        typer.echo(f"Server returned HTTP {resp.status}", err=True)
                        raise typer.Exit(1)
        except aiohttp.ClientError as exc:
            typer.echo(f"Cannot reach server: {exc}", err=True)
            raise typer.Exit(1)

    asyncio.run(_check())


def main():
    app()


if __name__ == "__main__":
    main()

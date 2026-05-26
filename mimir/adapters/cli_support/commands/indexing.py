"""Indexing, search, and graph exploration CLI commands."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
from pathlib import Path
from typing import Optional

import typer

from mimir.adapters.cli_support.common import DEFAULT_CONFIG, console, load_config, setup_logging
from mimir.domain.config import VectorBackend


def register(app: typer.Typer) -> None:
    @app.command()
    def index(
        mode: Optional[str] = typer.Option(None, help="Summary mode: none, heuristic"),
        clean: bool = typer.Option(False, "--clean", help="Force a full re-index (wipes existing data)"),
        workspace: Optional[str] = typer.Option(None, "--workspace", "-w", help="Named workspace from registry"),
        config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c", help="Config file path"),
        verbose: bool = typer.Option(False, "--verbose", "-v"),
    ) -> None:
        """Index all configured repositories. Uses incremental indexing by default."""
        from mimir.container import Container

        setup_logging(verbose)
        if mode == "llm":
            console.print("[red bold]Error:[/] summary_mode 'llm' has been removed. Use 'heuristic' (default) or 'none'.")
            raise typer.Exit(1)

        killed = _kill_serve_processes()
        if killed:
            import time

            console.print(
                f"[yellow]⚠ Stopped {killed} running 'mimir serve' process(es) to release database locks. "
                f"Re-run [bold]mimir serve[/] after indexing completes.[/]"
            )
            time.sleep(0.5)

        cfg, _ = load_config(config, workspace)
        if clean:
            import shutil

            chroma_dir = cfg.vector_db.persist_directory or str(cfg.session_dir / "chroma")
            chroma_path = Path(chroma_dir)
            if chroma_path.exists() and cfg.vector_db.backend is VectorBackend.CHROMA:
                shutil.rmtree(chroma_path)
                chroma_path.mkdir(parents=True, exist_ok=True)

        container = Container(cfg)
        try:
            if clean:
                container.clear_data(graph=True, sessions=False)
                console.print("[dim]Wiped graph + vector store.[/]")
                graph = asyncio.run(container.indexing.index_all(mode_override=mode))
                stats = graph.stats()
                console.print("[green]✓ Clean full index complete[/]")
                console.print(f"  Nodes: {stats['total_nodes']}")
                console.print(f"  Edges: {stats['total_edges']}")
                console.print(f"  Repos: {', '.join(stats['repos'])}")
            else:
                graph, report = asyncio.run(container.indexing.index_incremental(mode_override=mode))
                if report.get("mode") == "full_fallback":
                    stats = graph.stats()
                    console.print("[green]✓ Initial full index complete[/]")
                    console.print(f"  Nodes: {stats['total_nodes']}")
                    console.print(f"  Edges: {stats['total_edges']}")
                    console.print(f"  Repos: {', '.join(stats['repos'])}")
                else:
                    _display_incremental_report(report)
        except Exception as exc:
            console.print(f"[red bold]Indexing failed:[/] {exc}")
            raise typer.Exit(1) from exc
        finally:
            container.close()

    @app.command()
    def search(
        query: str = typer.Argument(..., help="Natural language query"),
        budget: int = typer.Option(8000, "--budget", "-b", help="Token budget"),
        repos: Optional[str] = typer.Option(None, "--repos", "-r", help="Comma-separated repo filter"),
        flat: bool = typer.Option(False, "--flat", help="Force flat search"),
        workspace: Optional[str] = typer.Option(None, "--workspace", "-w", help="Named workspace from registry"),
        config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c"),
        verbose: bool = typer.Option(False, "--verbose", "-v"),
    ) -> None:
        """Search the indexed codebase and assemble context."""
        from mimir.container import Container

        setup_logging(verbose)
        cfg, _ = load_config(config, workspace)
        container = Container(cfg)
        try:
            bundle = asyncio.run(container.retrieval.search(
                query=query,
                graph=container.load_graph(),
                token_budget=budget,
                repos=repos.split(",") if repos else None,
                flat=flat,
            ))
            console.print(f"\n[bold]{bundle.summary}[/]")
            if bundle.session_note:
                console.print(f"[dim]{bundle.session_note}[/]")
            console.print(f"[dim]Tokens: {bundle.token_count}[/]\n")
            console.print(bundle.format_for_llm())
        except Exception as exc:
            console.print(f"[red bold]Search failed:[/] {exc}")
            raise typer.Exit(1) from exc
        finally:
            container.close()

    @app.command("graph")
    def graph_cmd(
        stats: bool = typer.Option(False, "--stats", help="Show graph statistics"),
        show: Optional[str] = typer.Option(None, "--show", help="Show node details"),
        cross_repo: bool = typer.Option(False, "--cross-repo", help="Show cross-repo edges"),
        path_from: Optional[str] = typer.Option(None, "--path-from", help="Find path from"),
        path_to: Optional[str] = typer.Option(None, "--path-to", help="Find path to"),
        workspace: Optional[str] = typer.Option(None, "--workspace", "-w", help="Named workspace from registry"),
        config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c"),
        verbose: bool = typer.Option(False, "--verbose", "-v"),
    ) -> None:
        """Explore the code graph."""
        from rich.table import Table
        from mimir.container import Container

        setup_logging(verbose)
        cfg, _ = load_config(config, workspace)
        container = Container(cfg)
        try:
            graph = container.load_graph()
            if stats:
                graph_stats = graph.stats()
                table = Table(title="Graph Statistics")
                table.add_column("Metric", style="cyan")
                table.add_column("Value", style="green")
                table.add_row("Total Nodes", str(graph_stats["total_nodes"]))
                table.add_row("Total Edges", str(graph_stats["total_edges"]))
                table.add_row("Repos", ", ".join(graph_stats["repos"]))
                for kind, count in sorted(graph_stats["nodes_by_kind"].items()):
                    table.add_row(f"  {kind}", str(count))
                console.print(table)
            elif show:
                node = graph.get_node(show)
                if not node:
                    console.print(f"[red]Node not found: {show}[/]")
                    raise typer.Exit(1)
                console.print(f"[bold]{node.id}[/]")
                console.print(f"  Kind: {node.kind.value}")
                console.print(f"  Repo: {node.repo}")
                console.print(f"  Path: {node.path}")
                if node.signature:
                    console.print(f"  Signature: {node.signature}")
                if node.summary:
                    console.print(f"  Summary: {node.summary[:200]}")
                out_edges = graph.get_outgoing_edges(show)
                if out_edges:
                    console.print(f"\n  Outgoing ({len(out_edges)}):")
                    for edge in out_edges[:20]:
                        console.print(f"    → {edge.kind.value} → {edge.target}")
                in_edges = graph.get_incoming_edges(show)
                if in_edges:
                    console.print(f"\n  Incoming ({len(in_edges)}):")
                    for edge in in_edges[:20]:
                        console.print(f"    ← {edge.kind.value} ← {edge.source}")
            elif cross_repo:
                edges = graph.cross_repo_edges()
                table = Table(title=f"Cross-Repo Edges ({len(edges)})")
                table.add_column("Source", style="cyan")
                table.add_column("Kind", style="yellow")
                table.add_column("Target", style="green")
                for edge in edges:
                    table.add_row(edge.source, edge.kind.value, edge.target)
                console.print(table)
            elif path_from and path_to:
                edges = graph.shortest_path(path_from, path_to)
                if not edges:
                    console.print("[yellow]No path found[/]")
                else:
                    console.print(f"[bold]Path ({len(edges)} hops):[/]")
                    for edge in edges:
                        console.print(f"  {edge.source} --{edge.kind.value}--> {edge.target}")
            else:
                console.print("[yellow]Use --stats, --show, --cross-repo, or --path-from/--path-to[/]")
        finally:
            container.close()

    @app.command()
    def hotspots(
        top: int = typer.Option(20, "--top", "-n", help="Number of hotspots"),
        workspace: Optional[str] = typer.Option(None, "--workspace", "-w", help="Named workspace from registry"),
        config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c"),
        verbose: bool = typer.Option(False, "--verbose", "-v"),
    ) -> None:
        """Show recently and frequently changed code."""
        from rich.table import Table
        from mimir.container import Container

        setup_logging(verbose)
        cfg, _ = load_config(config, workspace)
        container = Container(cfg)
        try:
            results = container.temporal.get_hotspots(container.load_graph(), top_n=top)
            table = Table(title=f"Top {top} Hotspots")
            table.add_column("Node", style="cyan")
            table.add_column("Score", style="green")
            table.add_column("Changes", style="yellow")
            for node, score in results:
                table.add_row(node.id, f"{score:.3f}", str(node.modification_count))
            console.print(table)
        finally:
            container.close()

    @app.command()
    def quality(
        threshold: float = typer.Option(0.3, "--threshold", "-t", help="Quality score threshold for gap detection"),
        top: int = typer.Option(50, "--top", "-n", help="Maximum number of gaps to show"),
        repos: Optional[str] = typer.Option(None, "--repos", help="Comma-separated repo names to filter"),
        workspace: Optional[str] = typer.Option(None, "--workspace", "-w", help="Named workspace from registry"),
        config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c"),
        verbose: bool = typer.Option(False, "--verbose", "-v"),
    ) -> None:
        """Analyze graph quality and detect gaps in symbol resolution."""
        from rich.table import Table
        from mimir.container import Container

        setup_logging(verbose)
        cfg, _ = load_config(config, workspace)
        container = Container(cfg)
        try:
            overview = container.quality.detect_gaps(
                container.load_graph(),
                repos=repos.split(",") if repos else None,
                threshold=threshold,
                top_n=top,
            )
            table = Table(title="Graph Quality Overview")
            table.add_column("Metric", style="cyan")
            table.add_column("Value", style="green")
            table.add_row("Total Nodes", str(overview.total_nodes))
            table.add_row("Scored Nodes", str(overview.scored_nodes))
            table.add_row("Average Quality", f"{overview.avg_quality:.3f}")
            table.add_row("Gaps Detected", str(overview.gap_count))
            for bucket, count in sorted(overview.quality_distribution.items()):
                table.add_row(f"  {bucket}", str(count))
            console.print(table)

            if overview.gaps:
                console.print()
                gap_table = Table(title=f"Top {len(overview.gaps)} Gaps (quality < {threshold})")
                gap_table.add_column("Node", style="cyan", max_width=60)
                gap_table.add_column("Kind", style="yellow")
                gap_table.add_column("Score", style="red")
                gap_table.add_column("Reason", style="dim")
                for gap in overview.gaps:
                    gap_table.add_row(gap.node_id, gap.node_kind, f"{gap.quality_score:.3f}", gap.reason)
                console.print(gap_table)
            else:
                console.print("[green]No gaps detected — graph looks healthy![/]")
        finally:
            container.close()

    @app.command()
    def ask(
        query: str = typer.Argument(..., help="Natural language query"),
        budget: int = typer.Option(8000, "--budget", "-b", help="Token budget"),
        repos: Optional[str] = typer.Option(None, "--repos", "-r", help="Comma-separated repo filter"),
        flat: bool = typer.Option(False, "--flat", help="Force flat search"),
        workspace: Optional[str] = typer.Option(None, "--workspace", "-w", help="Named workspace from registry"),
        config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c", help="Config file path"),
        verbose: bool = typer.Option(False, "--verbose", "-v"),
    ) -> None:
        """Interactive semantic search CLI — retrieves context and answers via LLM."""
        from rich.markdown import Markdown
        from rich.panel import Panel
        from mimir.container import Container

        setup_logging(verbose)
        cfg, _ = load_config(config, workspace)
        container = Container(cfg)
        if not container.llm_client:
            console.print("[red bold]Error:[/] LLM Client not configured. Add an [llm] section to mimir.toml.")
            raise typer.Exit(1)
        try:
            bundle = asyncio.run(container.retrieval.search(
                query=query,
                graph=container.load_graph(),
                token_budget=budget,
                beam_width=cfg.retrieval.default_beam_width,
                repos=[repo.strip() for repo in repos.split(",")] if repos else None,
                flat=flat,
            ))
            prompt = f"Context:\n{bundle.format_for_llm()}\n\nQuestion:\n{query}"
            console.print(Panel(
                f"Assembling context from [cyan]{len(bundle.nodes)} nodes[/] across [cyan]{len(bundle.repos_involved)} repos[/]...",
                title="Mimir Semantic Search",
                border_style="blue",
            ))
            response = asyncio.run(container.llm_client.complete(prompt))
            console.print("\n")
            console.print(Markdown(response))
            console.print("\n")
            console.print(f"[dim]Sources referenced: {', '.join({n.repo + '/' + (n.path or '') for n in bundle.nodes})}[/]")
        except Exception as exc:
            console.print(f"[red bold]Search failed:[/] {exc}")
            raise typer.Exit(1) from exc
        finally:
            container.close()


def _kill_serve_processes() -> int:
    """Find and SIGTERM any running ``mimir serve`` processes."""
    current_pid = os.getpid()
    killed = 0
    try:
        result = subprocess.run(["pgrep", "-f", "mimir.*serve"], capture_output=True, text=True)
        pids = [int(pid) for pid in result.stdout.strip().splitlines() if pid.strip()]
        for pid in pids:
            if pid == current_pid:
                continue
            try:
                os.kill(pid, signal.SIGTERM)
                killed += 1
            except (ProcessLookupError, PermissionError):
                pass
    except FileNotFoundError:
        pass

    try:
        cwd = os.getcwd()
        result = subprocess.run(
            ["docker", "ps", "--filter", "status=running", "--format", "{{.ID}}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            for container_id in result.stdout.strip().splitlines():
                if not container_id.strip():
                    continue
                inspect = subprocess.run(
                    ["docker", "inspect", "--format", "{{range .Mounts}}{{.Source}}::{{.Destination}} {{end}}", container_id.strip()],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if inspect.returncode == 0 and cwd in inspect.stdout:
                    subprocess.run(["docker", "stop", container_id.strip()], capture_output=True, text=True, timeout=15)
                    killed += 1
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return killed


def _display_incremental_report(report: dict) -> None:
    console.print("[green]✓ Incremental index complete[/]")
    for repo_name, info in report.get("repos", {}).items():
        status = info.get("status", "unknown")
        if status == "up_to_date":
            console.print(f"  [dim]{repo_name}:[/] [green]up to date[/] ({info.get('commit', '')})")
        elif status == "updated":
            console.print(f"  [cyan]{repo_name}:[/] [yellow]updated[/] ({info.get('commit', '')})")
            console.print(f"    Files: +{info.get('files_added', 0)} added, ~{info.get('files_modified', 0)} modified, -{info.get('files_deleted', 0)} deleted")
            console.print(f"    Parsed: {info.get('files_parsed', 0)} files, {info.get('symbols_parsed', 0)} symbols")
            console.print(f"    Nodes: -{info.get('nodes_removed', 0)} removed")
        elif status == "full_index":
            console.print(f"  [cyan]{repo_name}:[/] [yellow]full index[/] ({info.get('reason', '')})")
        elif status == "skipped":
            console.print(f"  [dim]{repo_name}:[/] [red]skipped[/] ({info.get('reason', '')})")
    console.print(f"\n  [bold]Total:[/] -{report.get('total_removed', 0)} removed, +{report.get('total_added', 0)} added")
    console.print(f"  [bold]Graph:[/] {report.get('graph_nodes', 0)} nodes, {report.get('graph_edges', 0)} edges")

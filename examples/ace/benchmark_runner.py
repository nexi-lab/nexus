#!/usr/bin/env python3
"""Benchmark Runner for ACE Demo 3: Data Validator.

Compares baseline (no learning) vs ACE (with learning) performance.
Generates comparison charts and statistics.

Usage:
    python benchmark_runner.py --epochs 10 --trials 3
    python benchmark_runner.py --output results.json --plot
"""

import argparse
import json
import sys
from typing import Any

try:
    from rich.console import Console
    from rich.table import Table
except ImportError:
    print("❌ Error: 'rich' package not installed")
    print("Install with: pip install rich")
    sys.exit(1)

console = Console()


def run_baseline_trial(data_loader: Any) -> dict[str, float]:
    """Run baseline validation with NO learning (empty playbook).

    Args:
        data_loader: TitanicDataLoader instance

    Returns:
        Dictionary with baseline accuracy scores
    """
    from demo_3_data_validator import run_validation_task

    df = data_loader.dataset
    ground_truth = data_loader.ground_truth

    # Run with empty playbook (no learned strategies)
    result = run_validation_task(df, ground_truth, playbook_strategies=[])

    return {
        "accuracy": result["accuracy"],
        "precision": result["precision"],
        "recall": result["recall"],
        "f1": result["f1"],
    }


def run_ace_trial(data_loader: Any, epochs: int) -> dict[str, Any]:
    """Run ACE trial with learning enabled.

    Args:
        data_loader: TitanicDataLoader instance
        epochs: Number of training epochs

    Returns:
        Dictionary with final accuracy and learning history
    """
    from demo_3_data_validator import run_validation_task

    from nexus.sdk import connect

    nx = connect()
    df = data_loader.dataset
    ground_truth = data_loader.ground_truth

    # Create playbook
    playbook_name = f"data_validator_benchmark_{epochs}"

    history = []

    for epoch in range(epochs):
        # Get playbook
        playbook = nx.memory.get_playbook(playbook_name)
        if playbook is None:
            from nexus.core.ace.playbook import PlaybookManager

            session = nx.metadata.SessionLocal()
            playbook_mgr = PlaybookManager(
                session, nx.backend, nx.user_id or "system", nx.agent_id, nx.tenant_id
            )
            playbook_mgr.create_playbook(
                name=playbook_name,
                description="Benchmark validation playbook",
                scope="user",
            )
            session.close()
            playbook = nx.memory.get_playbook(playbook_name)

        strategies = playbook.get("content", {}).get("strategies", []) if playbook else []

        # Track trajectory
        traj_id = nx.memory.start_trajectory(
            task_description=f"Benchmark validation (Epoch {epoch})",
            task_type="data_validation_benchmark",
        )

        # Run validation
        result = run_validation_task(df, ground_truth, strategies)

        # Complete trajectory
        nx.memory.complete_trajectory(
            traj_id,
            status="success" if result["accuracy"] >= 0.9 else "partial",
            success_score=result["score"],
        )

        history.append(
            {
                "epoch": epoch,
                "accuracy": result["accuracy"],
                "precision": result["precision"],
                "recall": result["recall"],
                "f1": result["f1"],
            }
        )

        # Reflect and curate periodically
        if epoch > 0 and epoch % 2 == 0:
            reflections = nx.memory.batch_reflect(
                task_type="data_validation_benchmark",
                min_trajectories=2,
            )
            if reflections:
                reflection_ids = [r.get("memory_id") for r in reflections if r.get("memory_id")]
                if reflection_ids:
                    nx.memory.curate_playbook(reflection_ids, playbook_name)

    nx.close()

    final_result = history[-1]
    return {
        "final_accuracy": final_result["accuracy"],
        "final_precision": final_result["precision"],
        "final_recall": final_result["recall"],
        "final_f1": final_result["f1"],
        "history": history,
        "improvement": final_result["accuracy"] - history[0]["accuracy"],
    }


def run_benchmark(epochs: int = 10, trials: int = 3) -> dict[str, Any]:
    """Run complete benchmark comparing baseline vs ACE.

    Args:
        epochs: Number of epochs for ACE trial
        trials: Number of trials to average

    Returns:
        Benchmark results
    """
    from demo_3_data_validator import TitanicDataLoader

    console.print(
        f"\n[bold cyan]Running Benchmark: {trials} trials, {epochs} epochs each[/bold cyan]\n"
    )

    # Load data once
    console.print("[cyan]Loading dataset...[/cyan]")
    loader = TitanicDataLoader()
    loader.load()

    # Run baseline trials
    console.print(f"\n[yellow]Running {trials} baseline trials (no learning)...[/yellow]")
    baseline_results = []
    for i in range(trials):
        console.print(f"  Trial {i + 1}/{trials}...", end=" ")
        result = run_baseline_trial(loader)
        baseline_results.append(result)
        console.print(f"Accuracy: {result['accuracy']:.1%}")

    # Run ACE trials
    console.print(f"\n[green]Running {trials} ACE trials (with learning)...[/green]")
    ace_results = []
    for i in range(trials):
        console.print(f"  Trial {i + 1}/{trials} ({epochs} epochs)...", end=" ")
        result = run_ace_trial(loader, epochs)
        ace_results.append(result)
        console.print(f"Final Accuracy: {result['final_accuracy']:.1%}")

    # Calculate averages
    baseline_avg = {
        "accuracy": sum(r["accuracy"] for r in baseline_results) / trials,
        "precision": sum(r["precision"] for r in baseline_results) / trials,
        "recall": sum(r["recall"] for r in baseline_results) / trials,
        "f1": sum(r["f1"] for r in baseline_results) / trials,
    }

    ace_avg = {
        "accuracy": sum(r["final_accuracy"] for r in ace_results) / trials,
        "precision": sum(r["final_precision"] for r in ace_results) / trials,
        "recall": sum(r["final_recall"] for r in ace_results) / trials,
        "f1": sum(r["final_f1"] for r in ace_results) / trials,
        "improvement": sum(r["improvement"] for r in ace_results) / trials,
    }

    return {
        "config": {
            "epochs": epochs,
            "trials": trials,
        },
        "baseline": {
            "average": baseline_avg,
            "trials": baseline_results,
        },
        "ace": {
            "average": ace_avg,
            "trials": ace_results,
        },
        "comparison": {
            "accuracy_improvement": ace_avg["accuracy"] - baseline_avg["accuracy"],
            "relative_improvement": (ace_avg["accuracy"] - baseline_avg["accuracy"])
            / baseline_avg["accuracy"]
            * 100,
        },
    }


def display_results(results: dict[str, Any]):
    """Display benchmark results in a nice format.

    Args:
        results: Benchmark results dictionary
    """
    console.print("\n[bold]═══ Benchmark Results ═══[/bold]\n")

    # Comparison table
    table = Table(title="Baseline vs ACE Comparison")
    table.add_column("Metric", style="cyan")
    table.add_column("Baseline", style="yellow")
    table.add_column("ACE (Learned)", style="green")
    table.add_column("Improvement", style="magenta")

    baseline = results["baseline"]["average"]
    ace = results["ace"]["average"]

    table.add_row(
        "Accuracy",
        f"{baseline['accuracy']:.1%}",
        f"{ace['accuracy']:.1%}",
        f"+{results['comparison']['accuracy_improvement']:.1%}",
    )
    table.add_row(
        "Precision",
        f"{baseline['precision']:.2f}",
        f"{ace['precision']:.2f}",
        f"+{ace['precision'] - baseline['precision']:.2f}",
    )
    table.add_row(
        "Recall",
        f"{baseline['recall']:.2f}",
        f"{ace['recall']:.2f}",
        f"+{ace['recall'] - baseline['recall']:.2f}",
    )
    table.add_row(
        "F1 Score",
        f"{baseline['f1']:.2f}",
        f"{ace['f1']:.2f}",
        f"+{ace['f1'] - baseline['f1']:.2f}",
    )

    console.print(table)

    # Summary
    console.print(
        f"\n[bold green]✨ Relative Improvement: {results['comparison']['relative_improvement']:.1f}%[/bold green]"
    )
    console.print(
        f"   Averaged over {results['config']['trials']} trials, {results['config']['epochs']} epochs each\n"
    )


def save_results(results: dict[str, Any], output_path: str):
    """Save benchmark results to JSON file.

    Args:
        results: Benchmark results
        output_path: Path to save JSON file
    """
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2)
    console.print(f"[green]✓[/green] Results saved to: {output_path}")


def plot_results(results: dict[str, Any], output_path: str = "benchmark_plot.png"):
    """Plot learning curves (requires matplotlib).

    Args:
        results: Benchmark results
        output_path: Path to save plot
    """
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        console.print("[yellow]matplotlib not installed, skipping plot[/yellow]")
        return

    # Get learning history from first ACE trial
    history = results["ace"]["trials"][0]["history"]

    epochs = [h["epoch"] for h in history]
    accuracies = [h["accuracy"] for h in history]

    baseline_acc = results["baseline"]["average"]["accuracy"]

    plt.figure(figsize=(10, 6))
    plt.plot(epochs, accuracies, marker="o", label="ACE (Learning)", linewidth=2)
    plt.axhline(
        y=baseline_acc,
        color="r",
        linestyle="--",
        label=f"Baseline (No Learning): {baseline_acc:.1%}",
    )

    plt.xlabel("Epoch")
    plt.ylabel("Accuracy")
    plt.title("ACE Learning Curve: Data Validator")
    plt.legend()
    plt.grid(True, alpha=0.3)
    plt.tight_layout()

    plt.savefig(output_path, dpi=150)
    console.print(f"[green]✓[/green] Plot saved to: {output_path}")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Benchmark ACE Data Validator performance")
    parser.add_argument(
        "--epochs",
        type=int,
        default=10,
        help="Number of training epochs per trial (default: 10)",
    )
    parser.add_argument(
        "--trials",
        type=int,
        default=3,
        help="Number of trials to average (default: 3)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Save results to JSON file",
    )
    parser.add_argument(
        "--plot",
        action="store_true",
        help="Generate learning curve plot (requires matplotlib)",
    )

    args = parser.parse_args()

    try:
        # Run benchmark
        results = run_benchmark(epochs=args.epochs, trials=args.trials)

        # Display results
        display_results(results)

        # Save results
        if args.output:
            save_results(results, args.output)

        # Generate plot
        if args.plot:
            plot_results(results)

    except KeyboardInterrupt:
        console.print("\n[yellow]Benchmark interrupted by user[/yellow]")
        sys.exit(0)
    except Exception as e:
        console.print(f"\n[red]Error: {e}[/red]")
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()

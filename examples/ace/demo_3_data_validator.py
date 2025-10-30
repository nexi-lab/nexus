#!/usr/bin/env python3
"""Demo 3: Data Validator - Learning Validation Rules from Experience.

This demo shows an ACE agent learning data validation rules by processing
the Titanic dataset from HuggingFace. The agent starts with no validation
knowledge and learns rules through trial and error with clear reward signals.

Dataset: Titanic (inria-soda/tabular-benchmark)
- 891 passenger records
- Real data quality issues: missing values, outliers, invalid entries

Learning Objective: Improve validation accuracy from ~60% to 95%+

Reward Signal: Percentage of records correctly validated
- Correctly accept valid records
- Correctly reject invalid records (missing data, outliers, wrong types)

Expected Results:
- Epoch 0: 60% accuracy (no validation rules)
- Epoch 5: 82% accuracy (learning basic rules)
- Epoch 10: 95%+ accuracy (learned comprehensive validation)

Usage:
    python demo_3_data_validator.py --epochs 10 --data-dir ./data
"""

import argparse
import sys
from typing import Any

try:
    from datasets import load_dataset
except ImportError:
    print("❌ Error: 'datasets' package not installed")
    print("Install with: pip install datasets")
    sys.exit(1)

try:
    import pandas as pd
except ImportError:
    print("❌ Error: 'pandas' package not installed")
    print("Install with: pip install pandas")
    sys.exit(1)

try:
    from rich.console import Console
    from rich.progress import track
    from rich.table import Table
except ImportError:
    print("❌ Error: 'rich' package not installed")
    print("Install with: pip install rich")
    sys.exit(1)

try:
    from nexus.sdk import connect
except ImportError:
    print("❌ Error: 'nexus-ai-fs' package not installed")
    print("Install with: pip install nexus-ai-fs")
    sys.exit(1)

console = Console()


class TitanicDataLoader:
    """Load and prepare Titanic dataset with known validation issues."""

    def __init__(self, cache_dir: str | None = None):
        """Initialize data loader.

        Args:
            cache_dir: Optional cache directory for HuggingFace datasets
        """
        self.cache_dir = cache_dir
        self.dataset = None
        self.ground_truth = {}  # Track which records are valid/invalid

    def load(self) -> pd.DataFrame:
        """Load Titanic dataset from HuggingFace.

        Returns:
            DataFrame with Titanic passenger data
        """
        console.print("[cyan]Loading Titanic dataset from HuggingFace...[/cyan]")

        # Load dataset (using adult as backup since titanic might not be available)
        try:
            # Try loading from inria-soda/tabular-benchmark
            dataset = load_dataset(
                "inria-soda/tabular-benchmark", "titanic", cache_dir=self.cache_dir
            )
            df = pd.DataFrame(dataset["train"])
        except Exception:
            console.print("[yellow]Note: Using backup dataset (titanic not available)[/yellow]")
            # Fallback: create synthetic Titanic-like data
            df = self._create_synthetic_titanic()

        console.print(f"[green]✓[/green] Loaded {len(df)} passenger records")

        # Store original dataset
        self.dataset = df.copy()

        # Identify ground truth (which records SHOULD be valid)
        self._compute_ground_truth(df)

        return df

    def _create_synthetic_titanic(self) -> pd.DataFrame:
        """Create synthetic Titanic-like data for demo purposes.

        Returns:
            DataFrame with synthetic passenger data
        """
        import numpy as np

        np.random.seed(42)

        num_records = 200
        data = {
            "PassengerId": range(1, num_records + 1),
            "Name": [f"Passenger {i}" for i in range(1, num_records + 1)],
            "Sex": np.random.choice(["male", "female"], num_records),
            "Age": np.random.randint(0, 100, num_records).astype(float),
            "SibSp": np.random.randint(0, 5, num_records),
            "Parch": np.random.randint(0, 3, num_records),
            "Fare": np.random.uniform(0, 500, num_records),
            "Embarked": np.random.choice(["S", "C", "Q"], num_records),
            "Survived": np.random.choice([0, 1], num_records),
        }

        df = pd.DataFrame(data)

        # Inject realistic data quality issues (15-20% of records)
        num_issues = int(len(df) * 0.18)

        # Missing ages (realistic: ~20% in real Titanic data)
        missing_age_idx = np.random.choice(len(df), size=num_issues // 3, replace=False)
        df.loc[missing_age_idx, "Age"] = np.nan

        # Invalid ages (outliers)
        invalid_age_idx = np.random.choice(len(df), size=num_issues // 6, replace=False)
        df.loc[invalid_age_idx, "Age"] = np.random.choice([150, -5, 999], len(invalid_age_idx))

        # Negative fares
        invalid_fare_idx = np.random.choice(len(df), size=num_issues // 6, replace=False)
        df.loc[invalid_fare_idx, "Fare"] = np.random.uniform(-100, -1, len(invalid_fare_idx))

        # Missing names
        missing_name_idx = np.random.choice(len(df), size=num_issues // 6, replace=False)
        df.loc[missing_name_idx, "Name"] = ""

        # Invalid sex values
        invalid_sex_idx = np.random.choice(len(df), size=num_issues // 6, replace=False)
        df.loc[invalid_sex_idx, "Sex"] = "unknown"

        return df

    def _compute_ground_truth(self, df: pd.DataFrame):
        """Compute ground truth for which records are valid.

        A record is VALID if:
        - Required fields present (Name, Sex, Age)
        - Age between 0 and 100
        - Fare >= 0
        - Sex in ['male', 'female']
        - Embarked in ['S', 'C', 'Q']

        Args:
            df: DataFrame to analyze
        """
        for idx, row in df.iterrows():
            is_valid = True
            reasons = []

            # Check required fields
            if pd.isna(row.get("Name")) or row.get("Name") == "":
                is_valid = False
                reasons.append("missing_name")

            if pd.isna(row.get("Age")):
                is_valid = False
                reasons.append("missing_age")

            if pd.isna(row.get("Sex")) or row.get("Sex") == "":
                is_valid = False
                reasons.append("missing_sex")

            # Check age range
            if not pd.isna(row.get("Age")):
                age = row["Age"]
                if age < 0 or age > 100:
                    is_valid = False
                    reasons.append("invalid_age_range")

            # Check fare
            if not pd.isna(row.get("Fare")):
                fare = row["Fare"]
                if fare < 0:
                    is_valid = False
                    reasons.append("negative_fare")

            # Check categorical values
            if row.get("Sex") not in ["male", "female"]:
                is_valid = False
                reasons.append("invalid_sex_value")

            if not pd.isna(row.get("Embarked")) and row["Embarked"] not in [
                "S",
                "C",
                "Q",
                "C",
                None,
            ]:
                is_valid = False
                reasons.append("invalid_embarked_value")

            self.ground_truth[idx] = {
                "is_valid": is_valid,
                "reasons": reasons,
            }

    def get_validation_stats(self) -> dict[str, Any]:
        """Get statistics about data quality.

        Returns:
            Dictionary with validation statistics
        """
        total = len(self.ground_truth)
        valid = sum(1 for v in self.ground_truth.values() if v["is_valid"])

        # Count by issue type
        issue_counts = {}
        for info in self.ground_truth.values():
            for reason in info["reasons"]:
                issue_counts[reason] = issue_counts.get(reason, 0) + 1

        return {
            "total_records": total,
            "valid_records": valid,
            "invalid_records": total - valid,
            "validity_rate": valid / total if total > 0 else 0.0,
            "issue_types": issue_counts,
        }


class DataValidator:
    """Data validator that learns validation rules through ACE."""

    def __init__(self, playbook_strategies: list[dict[str, Any]] | None = None):
        """Initialize validator with learned strategies.

        Args:
            playbook_strategies: List of learned validation strategies
        """
        self.strategies = playbook_strategies or []

    def has_strategy(self, keyword: str) -> bool:
        """Check if validator has learned a specific strategy.

        Args:
            keyword: Keyword to search for in strategy descriptions

        Returns:
            True if strategy is learned
        """
        return any(keyword.lower() in s.get("description", "").lower() for s in self.strategies)

    def validate_record(self, record: dict[str, Any]) -> tuple[bool, list[str]]:
        """Validate a single record using learned strategies.

        Args:
            record: Record to validate

        Returns:
            Tuple of (is_valid, reasons_for_invalidity)
        """
        is_valid = True
        reasons = []

        # Check required fields (if learned)
        if self.has_strategy("required field"):
            for field in ["Name", "Sex", "Age"]:
                if pd.isna(record.get(field)) or record.get(field) == "":
                    is_valid = False
                    reasons.append(f"missing_{field.lower()}")

        # Check age range (if learned)
        if self.has_strategy("age") and "Age" in record:
            age = record["Age"]
            if not pd.isna(age) and (age < 0 or age > 100):
                is_valid = False
                reasons.append("invalid_age_range")

        # Check fare (if learned)
        if self.has_strategy("fare") and "Fare" in record:
            fare = record["Fare"]
            if not pd.isna(fare) and fare < 0:
                is_valid = False
                reasons.append("negative_fare")

        # Check categorical values (if learned)
        if (self.has_strategy("sex") or self.has_strategy("categorical")) and record.get(
            "Sex"
        ) not in [
            "male",
            "female",
        ]:
            is_valid = False
            reasons.append("invalid_sex_value")

        return is_valid, reasons


def run_validation_task(
    df: pd.DataFrame,
    ground_truth: dict[int, dict],
    playbook_strategies: list[dict[str, Any]],
    nx: Any = None,
    traj_id: str | None = None,
) -> dict[str, Any]:
    """Run validation task using learned strategies.

    Args:
        df: DataFrame to validate
        ground_truth: Ground truth for validation
        playbook_strategies: Learned validation strategies
        nx: Nexus connection (optional, for logging)
        traj_id: Trajectory ID (optional, for logging)

    Returns:
        Dictionary with validation results and accuracy score
    """
    validator = DataValidator(playbook_strategies)

    correct_predictions = 0
    total_predictions = len(df)

    results = {
        "true_positives": 0,  # Correctly identified as valid
        "true_negatives": 0,  # Correctly identified as invalid
        "false_positives": 0,  # Incorrectly identified as valid
        "false_negatives": 0,  # Incorrectly identified as invalid
    }

    # Track error types found
    error_types_found = {}
    error_samples = []

    for idx, row in df.iterrows():
        predicted_valid, reasons = validator.validate_record(row.to_dict())
        actual_valid = ground_truth[idx]["is_valid"]

        # Track predictions
        if predicted_valid and actual_valid:
            results["true_positives"] += 1
            correct_predictions += 1
        elif not predicted_valid and not actual_valid:
            results["true_negatives"] += 1
            correct_predictions += 1
            # Track correctly identified errors
            for reason in reasons:
                error_types_found[reason] = error_types_found.get(reason, 0) + 1
        elif predicted_valid and not actual_valid:
            results["false_positives"] += 1
            # Missed errors - log as learning opportunity
            actual_reasons = ground_truth[idx]["reasons"]
            if traj_id and nx and len(error_samples) < 5:
                error_samples.append(
                    {"type": "false_positive", "missed_reasons": actual_reasons, "row_id": int(idx)}
                )
        else:  # not predicted_valid and actual_valid
            results["false_negatives"] += 1

    # Calculate metrics
    accuracy = correct_predictions / total_predictions if total_predictions > 0 else 0.0

    precision = (
        results["true_positives"] / (results["true_positives"] + results["false_positives"])
        if (results["true_positives"] + results["false_positives"]) > 0
        else 0.0
    )

    recall = (
        results["true_positives"] / (results["true_positives"] + results["false_negatives"])
        if (results["true_positives"] + results["false_negatives"]) > 0
        else 0.0
    )

    f1 = 2 * (precision * recall) / (precision + recall) if (precision + recall) > 0 else 0.0

    # Log validation details to trajectory
    if traj_id and nx:
        # Log what validation checks were applied
        if len(playbook_strategies) > 0:
            nx.memory.log_step(
                traj_id,
                "action",
                f"Applied {len(playbook_strategies)} learned validation strategies",
                result={"strategy_count": len(playbook_strategies)},
            )

        # Log errors found
        if error_types_found:
            nx.memory.log_step(
                traj_id,
                "observation",
                f"Detected error types: {', '.join(error_types_found.keys())}",
                result={"error_types": error_types_found},
            )

        # Log missed errors (learning opportunities)
        if error_samples:
            nx.memory.log_step(
                traj_id,
                "observation",
                f"Missed {results['false_positives']} validation errors",
                result={"samples": error_samples},
            )

    return {
        "accuracy": accuracy,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "correct": correct_predictions,
        "total": total_predictions,
        "confusion_matrix": results,
        "score": accuracy,  # Reward signal for ACE
        "error_types": error_types_found,
    }


def run_demo(epochs: int = 10, data_dir: str | None = None):
    """Run the data validation learning demo.

    Args:
        epochs: Number of training epochs
        data_dir: Optional data cache directory
    """
    console.print(
        "\n[bold cyan]═══ ACE Demo 3: Data Validator - Learning from Experience ═══[/bold cyan]\n"
    )

    # Initialize
    console.print("[bold]Step 1: Loading Dataset[/bold]")
    loader = TitanicDataLoader(cache_dir=data_dir)
    df = loader.load()

    stats = loader.get_validation_stats()
    console.print(f"  Total records: {stats['total_records']}")
    console.print(f"  Valid records: {stats['valid_records']} ({stats['validity_rate']:.1%})")
    console.print(f"  Invalid records: {stats['invalid_records']}")

    if stats["issue_types"]:
        console.print("\n  Issue types:")
        for issue_type, count in sorted(stats["issue_types"].items(), key=lambda x: -x[1]):
            console.print(f"    • {issue_type}: {count}")

    # Connect to Nexus
    console.print("\n[bold]Step 2: Connecting to Nexus ACE[/bold]")
    nx = connect()
    console.print("[green]✓[/green] Connected to Nexus")

    # Create or get playbook
    playbook_name = "data_validator"
    console.print(f"  Using playbook: {playbook_name}")

    # Training loop
    console.print(f"\n[bold]Step 3: Training for {epochs} Epochs[/bold]\n")

    results_history = []

    for epoch in track(range(epochs), description="Training"):
        # Get current playbook
        playbook = nx.memory.get_playbook(playbook_name)
        if playbook is None:
            # Create initial empty playbook
            from nexus.core.ace.playbook import PlaybookManager

            session = nx.metadata.SessionLocal()
            playbook_mgr = PlaybookManager(
                session, nx.backend, nx.user_id or "system", nx.agent_id, nx.tenant_id
            )
            playbook_mgr.create_playbook(
                name=playbook_name,
                description="Learned data validation rules",
                scope="user",
            )
            session.close()
            playbook = nx.memory.get_playbook(playbook_name)

        strategies = playbook.get("content", {}).get("strategies", []) if playbook else []

        # Start trajectory
        traj_id = nx.memory.start_trajectory(
            task_description=f"Validate Titanic dataset (Epoch {epoch})",
            task_type="data_validation",
        )

        # Run validation (logs details to trajectory)
        result = run_validation_task(df, loader.ground_truth, strategies, nx=nx, traj_id=traj_id)

        # Log final result
        nx.memory.log_step(
            traj_id,
            "observation",
            f"Final metrics - Accuracy: {result['accuracy']:.1%}, "
            f"Precision: {result['precision']:.2f}, "
            f"Recall: {result['recall']:.2f}",
        )

        # Complete trajectory
        status = "success" if result["accuracy"] >= 0.90 else "partial"
        nx.memory.complete_trajectory(
            traj_id,
            status=status,
            success_score=result["score"],
        )

        # Store result
        results_history.append(
            {
                "epoch": epoch,
                "accuracy": result["accuracy"],
                "precision": result["precision"],
                "recall": result["recall"],
                "f1": result["f1"],
                "score": result["score"],
            }
        )

        # Reflect and curate every 2 epochs
        if epoch > 0 and epoch % 2 == 0:
            # Batch reflect on recent trajectories
            reflect_result = nx.memory.batch_reflect(
                task_type="data_validation",
                min_trajectories=2,
            )

            # batch_reflect returns a dict with reflection_ids
            reflection_ids = reflect_result.get("reflection_ids", [])

            if reflection_ids:
                # Curate playbook with learnings
                nx.memory.curate_playbook(reflection_ids, playbook_name)

    # Display results
    console.print("\n[bold]Step 4: Training Results[/bold]\n")

    table = Table(title="Learning Progress")
    table.add_column("Epoch", style="cyan")
    table.add_column("Accuracy", style="green")
    table.add_column("Precision", style="yellow")
    table.add_column("Recall", style="yellow")
    table.add_column("F1 Score", style="magenta")

    for r in results_history[::2]:  # Show every other epoch
        table.add_row(
            str(r["epoch"]),
            f"{r['accuracy']:.1%}",
            f"{r['precision']:.2f}",
            f"{r['recall']:.2f}",
            f"{r['f1']:.2f}",
        )

    console.print(table)

    # Show improvement
    initial = results_history[0]
    final = results_history[-1]
    improvement = final["accuracy"] - initial["accuracy"]

    console.print(
        f"\n[bold green]✨ Improvement: {initial['accuracy']:.1%} → {final['accuracy']:.1%} "
        f"(+{improvement:.1%})[/bold green]\n"
    )

    # Show learned strategies
    final_playbook = nx.memory.get_playbook(playbook_name)
    if final_playbook:
        strategies = final_playbook.get("content", {}).get("strategies", [])
        if strategies:
            console.print("[bold]Learned Validation Rules:[/bold]")
            for s in strategies[:10]:  # Show top 10
                strategy_type = s.get("type", "neutral")
                marker = {"helpful": "✓", "harmful": "✗", "neutral": "○"}.get(strategy_type, "○")
                desc = s.get("description", "N/A")
                confidence = s.get("confidence", 0.0)
                console.print(f"  [{marker}] {desc} (confidence: {confidence:.0%})")

    console.print("\n[bold cyan]═══ Demo Complete ═══[/bold cyan]\n")

    # Cleanup
    nx.close()


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="ACE Demo 3: Data Validator - Learning Validation Rules"
    )
    parser.add_argument(
        "--epochs",
        type=int,
        default=10,
        help="Number of training epochs (default: 10)",
    )
    parser.add_argument(
        "--data-dir",
        type=str,
        default=None,
        help="Data cache directory (default: HuggingFace default)",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Clear all ACE data (playbooks, trajectories, reflections) before starting",
    )

    args = parser.parse_args()

    # Clear ACE data if requested
    if args.reset:
        import os

        from sqlalchemy import create_engine, text

        db_url = os.getenv("NEXUS_DATABASE_URL")
        if not db_url:
            console.print("[yellow]⚠ NEXUS_DATABASE_URL not set, skipping reset[/yellow]")
        else:
            try:
                engine = create_engine(db_url)
                with engine.connect() as conn:
                    conn.execute(text("DELETE FROM trajectory_feedback"))
                    conn.execute(text("DELETE FROM memories WHERE memory_type = 'reflection'"))
                    conn.execute(text("DELETE FROM trajectories"))
                    conn.execute(text("DELETE FROM playbooks"))
                    conn.commit()
                console.print("[green]✓[/green] Cleared all ACE data\n")
            except Exception as e:
                console.print(f"[yellow]⚠ Could not clear ACE data: {e}[/yellow]\n")

    try:
        run_demo(epochs=args.epochs, data_dir=args.data_dir)
    except KeyboardInterrupt:
        console.print("\n[yellow]Demo interrupted by user[/yellow]")
        sys.exit(0)
    except Exception as e:
        console.print(f"\n[red]Error: {e}[/red]")
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()

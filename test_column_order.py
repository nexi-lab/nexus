#!/usr/bin/env python3
"""Test that dynamic_viewer preserves original column order."""

import io
import tempfile

import pandas as pd

import nexus


def test_column_order_preservation():
    """Test that filtered CSV maintains original column order."""

    with tempfile.TemporaryDirectory() as tmpdir:
        # Initialize NexusFS
        config = {
            "mode": "embedded",
            "data_dir": tmpdir,
            "enforce_permissions": False,
        }
        nx = nexus.connect(config=config)

        # Create test CSV with specific column order
        # Original order: col1, col2, col3, col4, col5, col6, col7
        test_data = pd.DataFrame(
            {
                "col1": [1, 2, 3],
                "col2": [10, 20, 30],
                "col3": [100, 200, 300],
                "col4": [1000, 2000, 3000],
                "col5": [10000, 20000, 30000],
                "col6": [100000, 200000, 300000],
                "col7": [1000000, 2000000, 3000000],
            }
        )

        csv_path = "/test_order.csv"
        csv_content = test_data.to_csv(index=False).encode("utf-8")
        nx.write(csv_path, csv_content)

        print("Original column order:")
        print(list(test_data.columns))
        print()

        # Test Case 1: Mixed visible and aggregated columns
        print("=" * 60)
        print("Test Case 1: Mixed visible and aggregated")
        print("=" * 60)
        column_config1 = {
            "hidden_columns": ["col5", "col7"],
            "aggregations": {"col2": "sum", "col4": "mean", "col6": "max"},
            "visible_columns": ["col1", "col3"],
        }

        nx.rebac_create(
            subject=("user", "test1"),
            relation="dynamic_viewer",
            object=("file", csv_path),
            column_config=column_config1,
        )

        context1 = {"subject": ("user", "test1")}
        content1 = nx.read(csv_path, context=context1)
        df1 = pd.read_csv(io.StringIO(content1.decode("utf-8")))

        print("Expected order: col1, sum(col2), col3, mean(col4), max(col6)")
        print(f"Actual order:   {', '.join(df1.columns)}")
        assert list(df1.columns) == ["col1", "sum(col2)", "col3", "mean(col4)", "max(col6)"]
        print("✓ Order is correct!")
        print()

        # Test Case 2: Aggregations at beginning and end
        print("=" * 60)
        print("Test Case 2: Aggregations at beginning and end")
        print("=" * 60)
        column_config2 = {
            "hidden_columns": ["col3", "col4"],
            "aggregations": {"col1": "count", "col7": "std"},
            "visible_columns": ["col2", "col5", "col6"],
        }

        nx.rebac_create(
            subject=("user", "test2"),
            relation="dynamic_viewer",
            object=("file", csv_path),
            column_config=column_config2,
        )

        context2 = {"subject": ("user", "test2")}
        content2 = nx.read(csv_path, context=context2)
        df2 = pd.read_csv(io.StringIO(content2.decode("utf-8")))

        print("Expected order: count(col1), col2, col5, col6, std(col7)")
        print(f"Actual order:   {', '.join(df2.columns)}")
        assert list(df2.columns) == ["count(col1)", "col2", "col5", "col6", "std(col7)"]
        print("✓ Order is correct!")
        print()

        # Test Case 3: Auto-calculated visible_columns
        print("=" * 60)
        print("Test Case 3: Auto-calculated visible_columns")
        print("=" * 60)
        column_config3 = {
            "hidden_columns": ["col4"],
            "aggregations": {"col2": "min"},
            # visible_columns omitted - should auto-calculate
        }

        nx.rebac_create(
            subject=("user", "test3"),
            relation="dynamic_viewer",
            object=("file", csv_path),
            column_config=column_config3,
        )

        context3 = {"subject": ("user", "test3")}
        content3 = nx.read(csv_path, context=context3)
        df3 = pd.read_csv(io.StringIO(content3.decode("utf-8")))

        print("Expected order: col1, min(col2), col3, col5, col6, col7")
        print(f"Actual order:   {', '.join(df3.columns)}")
        assert list(df3.columns) == ["col1", "min(col2)", "col3", "col5", "col6", "col7"]
        print("✓ Order is correct!")
        print()

        print("=" * 60)
        print("All column order tests passed! ✓")
        print("=" * 60)


if __name__ == "__main__":
    test_column_order_preservation()

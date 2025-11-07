#!/usr/bin/env python3
"""Integration test for dynamic_viewer permissions on read operations.

This test verifies:
1. CSV column validation in rebac_create
2. Automatic dynamic_viewer filtering in read operations
3. Integration with both local and remote clients
"""

import io
import tempfile

import pandas as pd

import nexus


def test_dynamic_viewer_read_integration():
    """Test dynamic_viewer permissions are applied during read operations."""

    # Create a temporary directory for testing
    with tempfile.TemporaryDirectory() as tmpdir:
        # Initialize NexusFS with embedded mode (permissions disabled for setup)
        config = {
            "mode": "embedded",
            "data_dir": tmpdir,
            "enforce_permissions": False,  # Disable for initial setup
        }
        nx = nexus.connect(config=config)

        # Create test CSV file
        test_data = pd.DataFrame(
            {
                "name": ["Alice", "Bob", "Charlie"],
                "email": ["alice@example.com", "bob@example.com", "charlie@example.com"],
                "age": [30, 25, 35],
                "salary": [100000, 80000, 120000],
                "password": ["secret1", "secret2", "secret3"],
                "ssn": ["111-11-1111", "222-22-2222", "333-33-3333"],
            }
        )

        csv_path = "/test_users.csv"
        csv_content = test_data.to_csv(index=False).encode("utf-8")

        # Write CSV file (permissions disabled, so no context needed)
        nx.write(csv_path, csv_content)
        print(f"✓ Created test CSV file: {csv_path}")

        # Grant user ownership (required to create permissions)
        nx.rebac_create(
            subject=("user", "admin"),
            relation="direct_owner",
            object=("file", csv_path),
        )
        print("✓ Granted admin ownership")

        # Create dynamic_viewer permission with column-level config
        column_config = {
            "hidden_columns": ["password", "ssn"],
            "aggregations": {"age": "mean", "salary": "sum"},
            "visible_columns": ["name", "email"],
        }

        tuple_id = nx.rebac_create(
            subject=("user", "alice"),
            relation="dynamic_viewer",
            object=("file", csv_path),
            column_config=column_config,
        )
        print(f"✓ Created dynamic_viewer permission: {tuple_id}")

        # Test 1: Read as admin (full access - no dynamic_viewer)
        admin_context = {"subject": ("user", "admin")}
        full_content = nx.read(csv_path, context=admin_context)
        full_df = pd.read_csv(io.StringIO(full_content.decode("utf-8")))

        print("\n" + "=" * 60)
        print("Test 1: Admin read (full access)")
        print("=" * 60)
        print(f"Columns: {list(full_df.columns)}")
        print(f"Row count: {len(full_df)}")
        assert "password" in full_df.columns, "Admin should see password column"
        assert "ssn" in full_df.columns, "Admin should see ssn column"
        assert len(full_df) == 3, "Admin should see all rows"
        print("✓ Admin has full access to all columns")

        # Test 2: Read as alice (dynamic_viewer - filtered)
        alice_context = {"subject": ("user", "alice")}
        filtered_content = nx.read(csv_path, context=alice_context)
        filtered_df = pd.read_csv(io.StringIO(filtered_content.decode("utf-8")))

        print("\n" + "=" * 60)
        print("Test 2: Alice read (dynamic_viewer - filtered)")
        print("=" * 60)
        print(f"Columns: {list(filtered_df.columns)}")
        print(f"Row count: {len(filtered_df)}")
        print("\nFiltered data:")
        print(filtered_df.to_string())

        # Verify filtering
        assert "password" not in filtered_df.columns, "Password should be hidden"
        assert "ssn" not in filtered_df.columns, "SSN should be hidden"
        assert "name" in filtered_df.columns, "Name should be visible"
        assert "email" in filtered_df.columns, "Email should be visible"
        assert "mean(age)" in filtered_df.columns, "Age aggregation should be present"
        assert "sum(salary)" in filtered_df.columns, "Salary aggregation should be present"
        assert len(filtered_df) == 3, "Should have same number of rows"

        # Verify aggregation values
        mean_age = filtered_df["mean(age)"].iloc[0]
        sum_salary = filtered_df["sum(salary)"].iloc[0]
        expected_mean_age = 30.0  # (30 + 25 + 35) / 3
        expected_sum_salary = 300000.0  # 100000 + 80000 + 120000

        assert abs(mean_age - expected_mean_age) < 0.01, f"Mean age should be {expected_mean_age}"
        assert abs(sum_salary - expected_sum_salary) < 0.01, (
            f"Sum salary should be {expected_sum_salary}"
        )

        print(f"✓ Mean age: {mean_age}")
        print(f"✓ Sum salary: {sum_salary}")
        print("✓ Alice sees only filtered data with aggregations")

        # Test 3: Test CSV column validation
        print("\n" + "=" * 60)
        print("Test 3: CSV column validation")
        print("=" * 60)

        try:
            # Try to create permission with invalid column
            nx.rebac_create(
                subject=("user", "bob"),
                relation="dynamic_viewer",
                object=("file", csv_path),
                column_config={
                    "hidden_columns": ["invalid_column"],
                },
                context=admin_context,
            )
            print("✗ Should have failed with invalid column")
            raise AssertionError("Should raise ValueError for invalid column")
        except ValueError as e:
            print(f"✓ Correctly rejected invalid column: {e}")
            assert "invalid_column" in str(e)

        # Test 4: Test with read_with_dynamic_viewer method
        print("\n" + "=" * 60)
        print("Test 4: read_with_dynamic_viewer method")
        print("=" * 60)

        # Grant Alice viewer permission (dynamic_viewer extends viewer)
        nx.rebac_create(
            subject=("user", "alice"),
            relation="direct_viewer",
            object=("file", csv_path),
        )
        print("✓ Granted Alice viewer permission")

        result = nx.read_with_dynamic_viewer(
            file_path=csv_path,
            subject=("user", "alice"),
            context=alice_context,
        )

        print(f"DEBUG: result keys = {result.keys()}")
        print(f"DEBUG: is_filtered = {result['is_filtered']}")
        print(f"DEBUG: columns_shown = {result['columns_shown']}")
        print(f"DEBUG: aggregated_columns = {result['aggregated_columns']}")
        print(f"DEBUG: config = {result['config']}")
        print(f"DEBUG: aggregations = {result['aggregations']}")

        assert result["is_filtered"], "Should indicate filtering was applied"
        assert result["config"] == column_config, "Should return column config"
        assert "mean(age)" in result["aggregated_columns"], "Should list aggregated columns"
        assert "name" in result["columns_shown"], "Should list visible columns"

        print(f"✓ is_filtered: {result['is_filtered']}")
        print(f"✓ columns_shown: {result['columns_shown']}")
        print(f"✓ aggregated_columns: {result['aggregated_columns']}")
        print(f"✓ aggregations: {result['aggregations']}")

        print("\n" + "=" * 60)
        print("All tests passed! ✓")
        print("=" * 60)


if __name__ == "__main__":
    test_dynamic_viewer_read_integration()

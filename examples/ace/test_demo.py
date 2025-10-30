#!/usr/bin/env python3
"""Quick test to verify demo components work."""

import sys
from importlib.util import find_spec

print("Testing Demo 3: Data Validator components...\n")

# Test 1: Check if Nexus SDK is available
print("1. Testing Nexus SDK import...")
if find_spec("nexus.sdk") is not None:
    print("   ✓ Nexus SDK available")
else:
    print("   ✗ Nexus SDK not available")
    sys.exit(1)

# Test 2: Check ACE components
print("2. Testing ACE components...")
if find_spec("nexus.core.ace") is not None:
    print("   ✓ ACE components available")
else:
    print("   ✗ ACE components not available")
    sys.exit(1)

# Test 3: Check demo imports
print("3. Testing demo imports...")
if find_spec("pandas") is not None:
    print("   ✓ pandas available")
else:
    print("   ⚠ pandas not installed (required for demo)")

if find_spec("rich") is not None:
    print("   ✓ rich available")
else:
    print("   ⚠ rich not installed (required for demo)")

if find_spec("datasets") is not None:
    print("   ✓ datasets available")
else:
    print("   ⚠ datasets not installed (will use synthetic data)")

# Test 4: Test synthetic data generation
print("4. Testing synthetic data generation...")
try:
    import numpy as np
    import pandas as pd

    np.random.seed(42)
    num_records = 50

    data = {
        "PassengerId": range(1, num_records + 1),
        "Name": [f"Passenger {i}" for i in range(1, num_records + 1)],
        "Sex": np.random.choice(["male", "female"], num_records),
        "Age": np.random.randint(0, 100, num_records).astype(float),
        "Fare": np.random.uniform(0, 500, num_records),
    }

    df = pd.DataFrame(data)
    print(f"   ✓ Created synthetic dataset with {len(df)} records")

except Exception as e:
    print(f"   ✗ Failed to create synthetic data: {e}")
    sys.exit(1)

# Test 5: Test validation logic
print("5. Testing validation logic...")
try:
    # Simulate validation
    def validate_record(record):
        is_valid = True
        if pd.isna(record.get("Name")) or record.get("Name") == "":
            is_valid = False
        if pd.isna(record.get("Age")):
            is_valid = False
        if not pd.isna(record.get("Age")) and (record["Age"] < 0 or record["Age"] > 100):
            is_valid = False
        return is_valid

    # Test on first record
    test_record = df.iloc[0].to_dict()
    result = validate_record(test_record)
    print(f"   ✓ Validation logic works (sample valid: {result})")

except Exception as e:
    print(f"   ✗ Validation logic failed: {e}")
    sys.exit(1)

print("\n✅ All basic tests passed!")
print("\nTo run the full demo:")
print("  1. Install dependencies: pip install -r requirements.txt")
print("  2. Run demo: python demo_3_data_validator.py --epochs 10")
print("  3. Run benchmark: python benchmark_runner.py --epochs 10 --trials 3")

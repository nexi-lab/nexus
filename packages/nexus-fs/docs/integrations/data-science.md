# Data Science

nexus-fs works with pandas, dask, and HuggingFace through the
[fsspec integration](fsspec.md). Install the fsspec extra:

```bash
pip install nexus-fs[fsspec]
```

## pandas

pandas uses fsspec under the hood for any `read_*` / `to_*` call with a
registered protocol prefix.

### Read a CSV from a mounted backend

```python
# skip-test
import pandas as pd

df = pd.read_csv("nexus:///s3/my-bucket/data.csv")
print(df.head())
```

### Write a DataFrame

```python
# skip-test
import pandas as pd

df = pd.DataFrame({"x": [1, 2, 3], "y": [4, 5, 6]})
df.to_csv("nexus:///local/data/output.csv", index=False)
```

### Read Parquet

```python
# skip-test
import pandas as pd

df = pd.read_parquet("nexus:///gcs/my-bucket/dataset.parquet")
```

### Read JSON

```python
# skip-test
import pandas as pd

df = pd.read_json("nexus:///s3/my-bucket/records.jsonl", lines=True)
```

## dask

dask uses fsspec for distributed reads. nexus-fs works as a backend
with no extra configuration.

```python
# skip-test
import dask.dataframe as dd

# Read partitioned data
ddf = dd.read_csv("nexus:///s3/my-bucket/partitions/*.csv")

# Process
result = ddf.groupby("category").sum().compute()
```

## HuggingFace

HuggingFace datasets can load from any fsspec filesystem.

```python
# skip-test
from datasets import load_dataset

dataset = load_dataset("csv", data_files="nexus:///s3/my-bucket/train.csv")
```

## Tips

- The `nexus://` prefix maps to whatever backends you have mounted.
  Mount first (via Python or CLI), then use the `nexus://` prefix in
  pandas/dask/HuggingFace.
- For large files, pandas and dask handle chunked reading automatically
  through fsspec's `open()`.
- All operations are synchronous when used through fsspec — the
  `NexusFileSystem` adapter bridges to the async nexus-fs core.

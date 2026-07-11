# Testcases

Bundled JSONL inputs for verifying the package without the full 520-report dataset.

- `sample`: three parsed reports: one libcurl C candidate, one curl CLI shell candidate, and one non-curl reject candidate.

Run with Python:

```python
from bugbounty_poc_repro import run_pipeline

try:
    run_pipeline(testcase="sample", clean=True, dry_run=True)
except Exception as e:
    print(str(e))
```

Run with Docker:

```python
try:
    run_pipeline(testcase="sample", clean=True, key_file=r"..\key.txt")
except Exception as e:
    print(str(e))
```

Dry-run the required LLM harness and claim/result judgement steps:

```python
try:
    run_pipeline(testcase="sample", clean=True, dry_run=True)
except Exception as e:
    print(str(e))
```

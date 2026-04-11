# Contributing to TryAii-DRE

## Development Setup

### Python package

```bash
cd packages/python
pip install -e ".[dev]"
pytest tests/ -v
```

### Node package

```bash
cd packages/node
npm install
npm test
```

## Shared Data

The `shared/` directory is the single source of truth for:
- Model registry (benchmarks, pricing, latency)
- Benchmark definitions and normalization ranges
- Training queries for centroid generation
- Pre-computed embedding centroids

When you update shared data, run `python scripts/sync-shared.py` to copy it into each package.

## Running Tests

```bash
# Python
cd packages/python
pytest tests/ -v

# Node
cd packages/node
npm test
```

## Pull Requests

- One PR per feature/fix
- Include tests for new functionality
- Run the test suite before submitting
- Update CHANGELOG.md for user-facing changes

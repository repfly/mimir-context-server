# Contributing

> [Back to README](../README.md)

Pull requests welcome. Run the test suite with:

```bash
pip install -e ".[dev]"
pytest
```

## Publishing to PyPI

```bash
pip install build twine

# Server package
python -m build
twine upload dist/*

# Client package
cd client
python -m build
twine upload dist/*
```

See also: [Architecture](architecture.md) for project structure.

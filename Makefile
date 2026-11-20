.PHONY: deps verify test bootstrap hooks clean help

help:
	@echo "Available commands:"
	@echo "  make deps       - Install pinned DV harness dependencies (requirements-dv-harness.txt)"
	@echo "  make verify     - Run harness, pytest suite and tooling tests (POSIX/WSL for canary and mutation)"
	@echo "  make test       - Alias to run tests"
	@echo "  make bootstrap  - Interactive repository setup"
	@echo "  make hooks      - Install local git hooks"
	@echo "  make clean      - Clean cache and temporary files"

deps:
	python -m pip install -r requirements-dv-harness.txt

verify:
	python scripts/harness.py
	python -m pytest tests -v
	cd tools && python -m unittest discover -p "test_*.py" -v

test: verify

bootstrap:
	python scripts/bootstrap.py

hooks:
	python scripts/install-hooks.py

clean:
	python -c "import pathlib, shutil; [shutil.rmtree(p) for p in pathlib.Path('.').rglob('__pycache__')]"
	python -c "import pathlib, shutil; [shutil.rmtree(p) for p in pathlib.Path('.').rglob('.pytest_cache')]"

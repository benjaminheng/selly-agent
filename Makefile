# Local entry points; CI (owner-managed, outside this repo's plans) calls these.
PY ?= python3
PY39 ?= python3.9
RUFF ?= ruff
PYRIGHT ?= pyright

DIST ?= dist
VERSION = $(shell $(PY) -c "import sys; sys.path.insert(0, 'src'); \
	import selly_agent; print(selly_agent.__version__)")
STAGE = $(DIST)/selly-agent-$(VERSION)

.PHONY: test test-3.9 lint fmt typecheck dist

test:
	$(PY) -m pytest

# The 3.9 runtime floor is checked by running the suite on a 3.9 interpreter,
# not by convention. Point PY39 at one (with pytest available) if it is not on PATH.
test-3.9:
	@if command -v $(PY39) >/dev/null 2>&1; then \
		$(PY39) -m pytest; \
	else \
		echo "SKIP: no Python 3.9 interpreter found — the 3.9 floor was NOT checked."; \
		echo "      Install one and re-run, e.g.: make test-3.9 PY39=/path/to/python3.9"; \
	fi

lint:
	$(RUFF) check .
	$(RUFF) format --check .

# Static type check (dev-only; the runtime stays stdlib-only). Scoped to the annotated
# store surface via [tool.pyright] in pyproject.toml. Point PYRIGHT at a binary if it is
# not on PATH.
typecheck:
	$(PYRIGHT)

fmt:
	$(RUFF) format .

# The release artifact: the same tree ./setup stages into versions/<v>, plus the checksum file
# that both `selly-agent update` and install.sh read the version out of. Publishing is manual
# (`gh release create`) until a cadence justifies automating it.
dist:
	@rm -rf $(STAGE) $(DIST)/selly-agent-$(VERSION).tar.gz $(DIST)/SHA256SUMS
	@mkdir -p $(STAGE)
	@cp -R bin src $(STAGE)/
	@cp README.md $(STAGE)/ 2>/dev/null || true
	@cp LICENSE $(STAGE)/ 2>/dev/null || true
	@cp setup $(STAGE)/
	@find $(STAGE) -name '__pycache__' -type d -prune -exec rm -rf {} +
	@find $(STAGE) -name '*.py[co]' -delete
# COPYFILE_DISABLE: macOS tar otherwise writes an AppleDouble `._name` entry beside every file
# carrying extended attributes, and those ship inside the published archive.
	@COPYFILE_DISABLE=1 tar -czf $(DIST)/selly-agent-$(VERSION).tar.gz -C $(DIST) selly-agent-$(VERSION)
	@rm -rf $(STAGE)
	@cd $(DIST) && { shasum -a 256 selly-agent-$(VERSION).tar.gz 2>/dev/null \
		|| sha256sum selly-agent-$(VERSION).tar.gz; } > SHA256SUMS
	@echo "$(DIST)/selly-agent-$(VERSION).tar.gz"
	@cat $(DIST)/SHA256SUMS

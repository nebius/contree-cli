SPHINXOPTS  ?=
SPHINXBUILD ?= uv run --group docs sphinx-build
SOURCEDIR   = .
BUILDDIR    = _build

.PHONY: help html clean

help:
	@$(SPHINXBUILD) -M help "$(SOURCEDIR)" "$(BUILDDIR)" $(SPHINXOPTS)

html:
	$(SPHINXBUILD) -b html "$(SOURCEDIR)" "$(BUILDDIR)/html" $(SPHINXOPTS)

mintlify-build:
	$(SPHINXBUILD) -b mintlify "$(SOURCEDIR)" "$(BUILDDIR)/mintlify" $(SPHINXOPTS)

mintlify-clean:
	rm -rf $(BUILDDIR)/mintlify

mintlify:
	$(MAKE) mintlify-clean
	$(MAKE) mintlify-build

clean:
	rm -rf $(BUILDDIR)

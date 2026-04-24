.PHONY: whitepaper whitepaper-open clean-build help

help:
	@echo "Targets:"
	@echo "  whitepaper       Render PARAM_AS_SIGNAL.md to build/param_as_signal.html"
	@echo "  whitepaper-open  Render and open in the default browser"
	@echo "  clean-build      Remove the build/ directory"

whitepaper:
	python3 tools/render_whitepaper.py

whitepaper-open: whitepaper
	open build/param_as_signal.html

clean-build:
	rm -rf build

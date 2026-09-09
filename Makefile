.PHONY: test style_check style check

# the repo's own sources, excluding the submodules under ethereum-lists/,
# coins_details/trezor_common/ and ethereum/
PY_SRC = cli.py conftest.py definitions coins_details/coins_details.py

test:
	pytest --random-order .

style_check:
	ruff --version
	@echo [RUFF]
	@ruff check $(PY_SRC)
	@ruff format --check $(PY_SRC)

style:
	@echo [RUFF]
	@ruff check --fix $(PY_SRC)
	@ruff format $(PY_SRC)

check:
	# Ignore:
	# I900 import not listed as a requirement
	# E501 line too long
	# W503 line break before binary operator
	# E203 whitespace before ':'
	flake8 --ignore=I900,E501,W503,E203 $(PY_SRC)

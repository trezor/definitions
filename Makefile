test:
	pytest --random-order .

style:
	isort **/*.py
	black **/*.py

check:
	# Ignore:
	# I900 import not listed as a requirement
    # E501 line too long
	# W503 line break before binary operator
	# E203 whitespace before ':'
	flake8 --ignore=I900,E501,W503,E203 .

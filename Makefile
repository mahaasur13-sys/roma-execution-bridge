.PHONY: install run bridge cli test lint clean

install:
	pip install -e ".[dev]"

run-bridge:
	uvicorn main:app --host 0.0.0.0 --port 8080 --reload

cli:
	python roma_cli.py run "$(TASK)"

status:
	python roma_cli.py status $(JOB_ID)

logs:
	python roma_cli.py logs $(JOB_ID) -n $(LINES)

list:
	python roma_cli.py list

health:
	python roma_cli.py health

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete

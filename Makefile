PY=.venv/bin/python
setup:
	python3 -m venv .venv
	$(PY) -m pip install -e . pytest
	npm install --prefix web
test:
	$(PY) -m pytest tests -q
	npm test --prefix web
bench:
	$(PY) -m care_ops.cli bench --incidents 2000 --redteam 400
run:
	$(PY) -m care_ops.cli serve
console:
	npm run dev --prefix web

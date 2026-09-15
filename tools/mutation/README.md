# Isolated mutation environment

Phase 4A invokes mutation testing as an external WSL backend. These dependencies must not be
installed into AutoTest's Windows `.venv` and are intentionally absent from `pyproject.toml` and
`uv.lock`.

The configured environment currently lives at `/home/ubuntu/autotest-mutation-env`. To reproduce
it inside the selected WSL distribution (Ubuntu 22.04 by default):

```sh
python3 -m venv /home/ubuntu/autotest-mutation-env
/home/ubuntu/autotest-mutation-env/bin/pip install -r /mnt/e/University/Semester_7/Intern/AutoTest/tools/mutation/requirements-mutation.txt
```

AutoTest invokes that environment's `bin/python`, `bin/pytest`, and `bin/mutmut` by absolute WSL
path. It does not activate the environment or depend on shell startup files or global `PATH`.
Override the location with CLI option `--mutation-venv`. AutoTest requires pytest 8.4.2 and Mutmut
3.7.0 exactly; their effective versions and the virtualenv path are recorded in each mutation
result.

#!/usr/bin/env bash
set -eu
ax_tests_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ax_repo=$(CDPATH= cd -- "$ax_tests_dir/../../.." && pwd)
ax_cache=${AX_UV_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/uv}/archive-v0
ax_test_pythonpath=$ax_repo
# Reuse installed cache entries; do not install or write into the cache.
for ax_dep in pytest pluggy packaging iniconfig pygments jsonschema jsonschema_specifications referencing rpds attrs attr yaml starlette anyio sniffio typing_extensions httpx httpcore h11 idna certifi; do
  for ax_init in "$ax_cache"/*/"$ax_dep"/__init__.py "$ax_cache"/*/"$ax_dep".py; do
    if [ -f "$ax_init" ]; then
      if [ "$ax_dep" = rpds ] && ! compgen -G "${ax_init%/*}/rpds.cpython-${AX_PYTHON_ABI:-312}-*.so" >/dev/null; then
        continue
      fi
      case "$ax_init" in
        */__init__.py) ax_dep_dir=${ax_init%/*/*} ;;
        *) ax_dep_dir=${ax_init%/*} ;;
      esac
      ax_test_pythonpath=$ax_test_pythonpath:$ax_dep_dir
      break
    fi
  done
done
export PYTHONPATH=$ax_test_pythonpath
export PYTHONDONTWRITEBYTECODE=1
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
: "${TMPDIR:?TMPDIR must be set}"
export TMPDIR
ax_check_dir=$(mktemp -d "$TMPDIR/ax-b16b-2-checks.XXXXXXXX")
export PYTHONPYCACHEPREFIX=$ax_check_dir/bytecode
python3 -m py_compile "$ax_repo"/plugins/timer/x-posts/*.py "$ax_repo"/plugins/timer/x-posts/nodes/x/*.py "$ax_tests_dir"/*.py "$ax_repo"/tests/fakes/b16b/x_posts/*.py
python3 -m pytest -p no:cacheprovider --basetemp "$ax_check_dir/pytest" "$ax_tests_dir" -q "$@"

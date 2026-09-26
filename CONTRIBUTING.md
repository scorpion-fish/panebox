# Contributing to PaneBox

Thanks for your interest in improving PaneBox!

## Getting started

```bash
git clone <your-fork-url> && cd panebox
python3 main.py                          # run from the checkout
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 \
                 python3-pil python3-requests python3-watchdog python3-dateutil
```

## Code style

Python follows the [Google Python Style Guide](https://google.github.io/styleguide/pyguide/)
(PEP 8 superset, 120-char lines, type hints, docstrings for public modules and
functions). Naming follows the Java/Alibaba convention already used throughout:
`PascalCase` classes, `snake_case` functions/variables, `UPPER_CASE` constants,
camelCase ONLY for JSON settings keys mirrored from the Windows original — keep
those verbatim, they are a wire format.

Enforced with ruff (config in `pyproject.toml`):

```bash
pip install ruff
ruff check . && ruff format --check .
```

Notes specific to this codebase:

- `gi.require_version(...)` must stay BEFORE its `from gi.repository import`
  line — do not let tooling reorder those pairs.
- Modules that mirror a C# original cite the source file in the module
  docstring (e.g. `# InitialFileWidgetPlacementPolicy.cs`). Keep the citation
  when porting.
- UI strings come from `strings/<culture>.json` via `panebox.i18n` — never
  hardcode user-visible text.

## Tests

Every change ships with tests. GUI tests need `Xvfb :99`; a running PaneBox
instance owns the D-Bus name and collides with them, so stop it first:

```bash
Xvfb :99 -screen 0 1280x800x24 +extension RANDR &
PANEBOX_SMOKE_DISPLAY=:99 python3 -m pytest tests/
```

## Commits & pull requests

Conventional Commits (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`,
`chore:`), subject in English, imperative mood, ≤72 characters. PRs: describe
what + why, list test results, link related issues.

## Packaging

`bash packaging/build_all.sh` builds the .deb/.AppImage/.snap artifacts into
`dist/` — never commit them.

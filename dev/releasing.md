# Releasing to PyPI

`.github/workflows/publish.yml` builds and uploads on any `v*` tag, through
PyPI trusted publishing (no API token anywhere).

## One-time setup

1. On PyPI, add a *pending* trusted publisher for project `pcode`
   (https://pypi.org/manage/account/publishing/): owner `aweis89`, repo
   `pcode`, workflow `publish.yml`, environment `pypi`. The project is created
   by the first upload.
2. On GitHub, create the `pypi` environment (Settings → Environments). Adding
   yourself as a required reviewer there makes every upload wait for a click.

## Each release

1. Bump `version` in `pyproject.toml`, commit, merge to `master`.
2. `git tag vX.Y.Z && git push origin vX.Y.Z`. The build fails if the tag and
   the pyproject version differ.

Check the artifacts locally first with `uv build -o /tmp/dist && uvx twine
check /tmp/dist/*`. The sdist is an explicit allowlist in `pyproject.toml`; a
new top-level file a source build needs must be added there.

## After the first release

Add the PyPI install to `README.md` and `docs/getting-started.md`
(`uv tool install 'pcode[claude]'`, or `pipx install`), and drop the
"no tagged releases yet" line there. The Homebrew formula still tracks
`--HEAD`; pointing it at the tagged sdist is a separate change.

# Releasing

```sh
make release                  # tag origin/master as the next patch and push the tag
make release BUMP=minor       # or BUMP=major, or VERSION=0.2.0rc1 for an exact one
```

That tag is the whole release. `.github/workflows/publish.yml` then:

1. builds the sdist and wheel, with the version read from the tag by
   `hatch-vcs` (`pyproject.toml` holds no version);
2. uploads them to PyPI through trusted publishing (no API token anywhere);
3. creates the GitHub Release with generated notes (`--prerelease` for tags
   like `v0.2.0rc1`);
4. for a plain `vX.Y.Z` tag, runs `scripts/bump_formula.py` and commits the
   new `url`/`sha256` to `Formula/pcode.rb` on `master`, so `brew upgrade`
   sees the release. Pre-release tags leave the formula alone.

`scripts/release.sh` tags what origin's `master` has, never the local checkout,
and refuses a commit whose `ci` run didn't pass (`FORCE=1` overrides it).

## Versions between releases

An untagged build gets a dev version from git, such as
`0.1.1.dev3+gabc1234`. Homebrew stages no `.git`, so the formula passes the
version in through `SETUPTOOLS_SCM_PRETEND_VERSION`: the release for a stable
install, `0.dev0+g<commit>` for `--HEAD`. Any other build from a tree without
git metadata (a GitHub tarball, say) needs the same variable or fails.

## Traps

- The formula commit is pushed with the workflow's `GITHUB_TOKEN`, which does
  not trigger other workflows, so `ci` never runs on it. It needs `master` to
  accept that push; branch protection that blocks it breaks step 4 only, and
  `python3 scripts/bump_formula.py vX.Y.Z` plus a commit fixes it by hand.
- A failed run leaves the tag in place. If nothing reached PyPI, fix the
  cause, delete the tag (`git push origin :refs/tags/vX.Y.Z` and
  `git tag -d vX.Y.Z`) and release again. Once an upload succeeded the version
  is spent, even if deleted: PyPI never accepts it twice, so release the next.
- The sdist is an explicit allowlist in `pyproject.toml`; a new top-level file
  a source build needs must be added there. Check artifacts locally with
  `uv build -o /tmp/dist && uvx twine check /tmp/dist/*`.

## One-time setup (done)

The PyPI project lists `cruxwell/pcode`, workflow `publish.yml`, environment
`pypi` as its trusted publisher, and the `pypi` environment exists on GitHub.
Adding a required reviewer to that environment makes every upload wait for a
click.

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
   like `v0.2.0rc1`), with the sdist, wheel and `pcode-X.Y.Z-src.tar.gz`
   (a `git archive` of the tag, which unlike the sdist includes `uv.lock`);
4. for a plain `vX.Y.Z` tag, runs `scripts/bump_formula.py` and pushes the
   new `url`/`sha256` to `Formula/pcode.rb` in
   [aweis89/homebrew-tap](https://github.com/aweis89/homebrew-tap) (`main`),
   pointing at that `-src.tar.gz` asset, so `brew upgrade` sees the release.
   Pre-release tags leave the formula alone.

## Counting Homebrew installs

The formula downloads a release asset rather than GitHub's
`archive/refs/tags/...` tarball because only assets keep a download count.
`make brew-stats` lists it per release, plus the total.

Each count is installs plus upgrades to that release (brew caches the
download, so a reinstall rarely adds one), not unique users. `--HEAD` installs
clone git and are never counted; releases before v0.1.3 have no such asset.

`scripts/release.sh` tags what origin's `master` has, never the local checkout,
and refuses a commit whose `ci` run didn't pass (`FORCE=1` overrides it).

## Versions between releases

An untagged build gets a dev version from git, such as
`0.1.1.dev3+gabc1234`. Homebrew stages no `.git`, so the formula passes the
version in through `SETUPTOOLS_SCM_PRETEND_VERSION_FOR_PCODE`: the release for a stable
install, `0.dev0+g<commit>` for `--HEAD`. Any other build from a tree without
git metadata (a GitHub tarball, say) needs the same variable or fails.

## Traps

- Homebrew refuses formulae from untrusted taps, but `brew install` with the
  fully qualified `aweis89/tap/pcode` trusts that formula itself. Keep every
  install and upgrade line in the docs fully qualified, or users hit a
  `brew trust` error instead.
- Test a formula change by tapping a homebrew-tap checkout
  (`brew tap aweis89/tap-test $PWD`) and installing both ways. A HEAD test needs the tap's `head` pointed at
  the local branch with `using: :git`. The v0.1.0 tarball has a static version,
  so a stable install never exercises the pretend-version path; only HEAD does.

- The formula commit is pushed to the tap with the `HOMEBREW_TAP_DEPLOY_KEY`
  secret, whose public half is a write deploy key on the tap (`GITHUB_TOKEN`
  cannot push to another repo). If step 4 fails, run
  `python3 scripts/bump_formula.py <tap>/Formula/pcode.rb vX.Y.Z` and commit
  in the tap by hand.
- The repo was `cruxwell/pcode` from v0.1.0 to v0.1.11 and doubled as its own
  tap. `tap_migrations.json` at the root moves those users' `cruxwell/pcode`
  tap installs to `aweis89/tap` on `brew update`; keep it.
- A failed run leaves the tag in place. If nothing reached PyPI, fix the
  cause, delete the tag (`git push origin :refs/tags/vX.Y.Z` and
  `git tag -d vX.Y.Z`) and release again. Once an upload succeeded the version
  is spent, even if deleted: PyPI never accepts it twice, so release the next.
- The sdist is an explicit allowlist in `pyproject.toml`; a new top-level file
  a source build needs must be added there. Check artifacts locally with
  `uv build -o /tmp/dist && uvx twine check /tmp/dist/*`.

## One-time setup (done)

The PyPI project lists `aweis89/pcode` (re-added after the move from
`cruxwell`; PyPI pins the owner, so a transfer breaks publishing), workflow `publish.yml`, environment
`pypi` as its trusted publisher, and the `pypi` environment exists on GitHub.
Adding a required reviewer to that environment makes every upload wait for a
click.

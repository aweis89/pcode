"""Point Formula/pcode.rb's stable release at a tag.

    python scripts/bump_formula.py v0.1.1            # downloads the tag's tarball
    python scripts/bump_formula.py v0.1.1 <sha256>   # hash already known

Sets `url` and `sha256` to GitHub's tarball for the tag, adding them above
`head` the first time. The publish workflow runs this after each release.
"""

import hashlib
import re
import sys
import urllib.request
from pathlib import Path

FORMULA = Path(__file__).resolve().parent.parent / "Formula" / "pcode.rb"
TARBALL = "https://github.com/cruxwell/pcode/archive/refs/tags/{tag}.tar.gz"


def tarball_sha256(url: str) -> str:
    digest = hashlib.sha256()
    with urllib.request.urlopen(url) as response:
        while chunk := response.read(1 << 16):
            digest.update(chunk)
    return digest.hexdigest()


def bump(text: str, url: str, sha256: str) -> str:
    stable = f'  url "{url}"\n  sha256 "{sha256}"\n'
    text, count = re.subn(r'  url "[^"]*"\n  sha256 "[^"]*"\n', stable, text)
    if count == 0:
        text, count = re.subn(r"(?m)^(  head )", lambda m: stable + m.group(1), text, count=1)
    if count != 1:
        raise SystemExit(f"{FORMULA}: no `url`/`sha256` pair or `head` line to update")
    return text


def main(argv: list[str]) -> None:
    if len(argv) not in (1, 2) or not re.fullmatch(r"v\d+(\.\d+)*\S*", argv[0]):
        raise SystemExit(__doc__)
    url = TARBALL.format(tag=argv[0])
    sha256 = argv[1] if len(argv) == 2 else tarball_sha256(url)
    FORMULA.write_text(bump(FORMULA.read_text(), url, sha256))
    print(f"{FORMULA.name}: {url} {sha256}")


if __name__ == "__main__":
    main(sys.argv[1:])

"""Markdown replies rendered for Gmail: styled, inline, and inert."""

import re

from pcode.email_remote import outbound
from pcode.email_remote.render import markdown_html

OWNER = "owner@example.test"
ALIAS = "owner+pcode-abc@example.test"


def test_common_markdown_becomes_styled_html():
    out = markdown_html(
        "## Done\n\n**Bold**, `code`, ~~gone~~ and [docs](https://example.test/a).\n\n"
        "- one\n- two\n\n> quoted\n\n| a | b |\n|:--|--:|\n| 1 | 2 |\n"
    )
    assert re.search(r"<h2 style=\"[^\"]+\">Done</h2>", out)
    assert "<strong>Bold</strong>" in out and "<s>gone</s>" in out
    assert re.search(r'<code style="[^"]*monospace[^"]*">code</code>', out)
    assert '<a href="https://example.test/a" style="color:#0969da">docs</a>' in out
    assert re.search(r'<ul style="[^"]+">', out) and re.search(r'<blockquote style="[^"]+">', out)
    # Table alignment survives next to the default cell style.
    assert re.search(r'<td style="[^"]*border[^"]*;text-align:right">2</td>', out)
    # Every tag carries its style inline: nothing relies on a <style> block.
    assert "<style" not in out and "class=" not in out


def test_fenced_code_is_highlighted_inline_and_unknown_languages_stay_plain():
    out = markdown_html("```python\ndef f(x):\n    return x < 1\n```\n\n```nope\na <b> c\n```\n")
    blocks = re.findall(r"<pre style=\"[^\"]+\">(.*?)</pre>", out, re.S)
    assert len(blocks) == 2
    assert '<span style="' in blocks[0] and "&lt;" in blocks[0]
    assert blocks[1] == "a &lt;b&gt; c"
    # The inline-code chip style never lands inside a code block.
    assert "<code" not in "".join(blocks)


def test_diff_lines_are_coloured():
    out = markdown_html("```diff\n-old\n+new\n```\n")
    assert re.search(r'<span style="color: #[0-9A-Fa-f]{6}">-old</span>', out)
    assert re.search(r'<span style="color: #[0-9A-Fa-f]{6}">\+new</span>', out)


def test_model_html_scripts_images_and_bad_links_are_inert():
    out = markdown_html(
        '<script>alert(1)</script>\n\n<img src="https://t.example/p.png">\n\n'
        "![pixel](https://t.example/x?secret=1) [x](javascript:alert(1)) "
        "![y](data:image/png;base64,AAAA) <a href='https://t.example'>a</a>\n"
    )
    assert "<script" not in out and "<img" not in out and "javascript:" not in out.split('"')[1::2]
    assert "&lt;script&gt;" in out and "&lt;img" in out
    # An image is a link to click, never something loaded when the email opens.
    assert '<a href="https://t.example/x?secret=1" style="color:#0969da">[image: pixel]</a>' in out
    assert not re.search(r'href="(javascript|data):', out)
    assert "<p" in out and " y " in out  # A data: image is just its alt text.


def test_only_web_links_are_live():
    out = markdown_html(
        "See [app.py](src/app.py), <mailto:x@example.test>, <y@example.test> "
        "and <https://example.test/z>.\n"
    )
    assert out.count("<a ") == 1 and 'href="https://example.test/z"' in out
    assert "app.py" in out and "x@example.test" in out and 'href="mailto' not in out


def test_table_alignment_replaces_the_default():
    out = markdown_html("| a | b |\n|:--|--:|\n| 1 | 2 |\n")
    assert out.count("text-align") == 4
    assert re.search(r'<th style="[^"]*;text-align:right">b</th>', out)


def test_large_code_stays_under_gmails_clipping_size():
    # Gmail clips HTML past ~102 KB, hiding the session detail and footer.
    code = "def f(x):\n    return {'a': [x, 1, 2.5, None]}\n" * 500
    big = outbound.compose(owner=OWNER, alias=ALIAS, subject="s", body=f"```python\n{code}```")
    assert len(big.get_body(("html",)).get_content()) < 90_000
    # Several medium blocks: each under the per-block limit, together too large.
    block = "```python\n" + "x = {'a': [1, 2.5, None]}\n" * 250 + "```\n\n"
    many = outbound.compose(owner=OWNER, alias=ALIAS, subject="s", body=block * 12)
    html = many.get_body(("html",)).get_content()
    assert "Start new task" in html and len(html) < 120_000
    small = markdown_html("```python\nx = 1\n```")
    assert '<span style="' in small


def test_compose_renders_html_and_keeps_markdown_and_trailer_in_plain_text():
    message = outbound.compose(
        owner=OWNER,
        alias=ALIAS,
        subject="Re: task",
        body="**Done.** See `app.py`.",
        trailer="Status: done\nSession s1: abc, host h1\n\n a.py | 2 +-",
    )
    plain = message.get_body(("plain",)).get_content()
    html = message.get_body(("html",)).get_content()
    assert plain.startswith("**Done.** See `app.py`.\n\n---\nStatus: done\n")
    assert "<strong>Done.</strong>" in html
    # The trailer is preformatted (a diffstat keeps its columns) and escaped.
    assert re.search(r"<pre style=\"[^\"]*\">Status: done\nSession s1: abc, host h1\n\n a.py", html)
    # The alias appears once in the HTML: the Start new task link.
    assert html.count(ALIAS) == 1 and "Start new task</a>" in html


def test_compose_without_trailer_has_no_separator():
    message = outbound.compose(owner=OWNER, alias=ALIAS, subject="s", body="Hi")
    assert message.get_body(("plain",)).get_content().startswith("Hi\n\n--\n")
    assert "<pre" not in message.get_body(("html",)).get_content()

#!/usr/bin/env python3
"""Build a two-language static website: pages as HTML fragments, one base template, hreflang pairs, a sitemap with
language alternates, and asset URLs that change when the file changes.

    bilingual-site build example/            writes example/dist/
    bilingual-site build example/ --strict   also fails on pages marked as drafts

A page is an HTML fragment in pages/<language>/ with a header comment:

    <!--
    title: About us
    description: Who we are.
    path: /about/
    alternate: /de/ueber-uns/
    -->

Placeholders in double curly braces: {{name}} from site.toml, {{include:footer}} from partials/footer.html and
{{asset:style.css}}, which becomes /assets/style.css?v=<hash of the file>. Assets can then be cached for a long
time: a changed file gets a new URL, so browsers never combine new HTML with an old stylesheet.

The build stops on anything that would be wrong on the live site: an unknown placeholder, a page without title or
path, two pages with one path, a missing asset, or a language pair that does not point back."""

from __future__ import annotations

import argparse
import hashlib
import html
import os
import re
import shutil
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

__version__ = "0.1.0"
PLACEHOLDER = re.compile(r"\{\{([\w:./-]+)\}\}")
HEADER = re.compile(r"\A<!--\n(.*?)\n-->\n", re.DOTALL)
PATH = re.compile(r"^/(?:[a-z0-9][a-z0-9._-]*/)*(?:[a-z0-9][a-z0-9._-]*\.html)?$")  # no "." or ".." segments
LANG = re.compile(r"^[a-z]{2,3}(?:-[A-Za-z]{2,4})?$")
MARKER = ".bilingual-site"  # marks an output folder this tool made, so only such a folder is ever replaced


class BuildError(Exception):
    """Something that would be wrong on the live site."""


@dataclass
class Page:
    source: Path
    lang: str
    head: dict[str, str]
    body: str
    path: str = field(init=False)

    def __post_init__(self) -> None:
        self.path = self.head["path"]

    @property
    def indexed(self) -> bool:
        return (self.head.get("noindex") != "yes" and self.head.get("draft") != "yes"
                and not self.path.endswith(".html"))


@dataclass
class Site:
    root: Path
    base_url: str
    languages: dict[str, dict]
    values: dict[str, str]

    @classmethod
    def load(cls, root: Path) -> Site:
        try:
            config = tomllib.loads((root / "site.toml").read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise BuildError(f"{root / 'site.toml'} is missing") from None
        except tomllib.TOMLDecodeError as exc:
            raise BuildError(f"site.toml: {exc}") from None
        languages = config.get("languages") or {}
        if len(languages) != 2:
            raise BuildError("site.toml needs exactly two [languages.<code>] tables")
        for code, lang in languages.items():
            if not LANG.match(code):
                raise BuildError(f"[languages.{code}]: use a language code such as en, de or pt-BR")
            if not PATH.match(str(lang.get("home", ""))):
                raise BuildError(f"[languages.{code}] home must be a path like / or /de/")
        values = {k: str(v) for k, v in config.items() if not isinstance(v, dict)}
        return cls(root, str(config.get("base_url", "")).rstrip("/"), languages, values)

    def other(self, lang: str) -> str:
        return next(code for code in self.languages if code != lang)


def read_page(source: Path, lang: str) -> Page:
    text = source.read_text(encoding="utf-8")
    found = HEADER.match(text)
    if not found:
        raise BuildError(f"{source}: the header comment is missing")
    head = {}
    for line in found.group(1).splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            raise BuildError(f"{source}: header line without a colon: {line!r}")
        head[key.strip()] = value.strip()
    for key in ("title", "path"):
        if not head.get(key):
            raise BuildError(f"{source}: {key} is missing in the header")
    if not PATH.match(head["path"]) or ("alternate" in head and not PATH.match(head["alternate"])):
        raise BuildError(f"{source}: paths start with / and end with / or .html, lower case, no spaces")
    return Page(source, lang, head, text[found.end():])


def inside(folder: Path, name: str) -> Path | None:
    """folder/name, or None if the name leads out of the folder."""
    path = (folder / name).resolve()
    return path if path.is_relative_to(folder.resolve()) else None


def fill(text: str, values: dict[str, str], site: Site, where: Path, depth: int = 0) -> str:
    def replace(match: re.Match) -> str:
        key = match.group(1)
        kind, _, name = key.partition(":")
        if kind == "include" and name:
            partial = inside(site.root / "partials", f"{name}.html")
            if depth > 5 or partial is None or not partial.is_file():
                raise BuildError(f"{where}: include {name!r} not found (or nested too deep)")
            return fill(partial.read_text(encoding="utf-8"), values, site, partial, depth + 1)
        if kind == "asset" and name:
            asset = inside(site.root / "assets", name)
            if asset is None or not asset.is_file():
                raise BuildError(f"{where}: asset {name!r} not found")
            return f"/assets/{name}?v={hashlib.sha256(asset.read_bytes()).hexdigest()[:10]}"
        if key not in values:
            raise BuildError(f"{where}: unknown placeholder {{{{{key}}}}}")
        return values[key]

    return PLACEHOLDER.sub(replace, text)


def check_pairs(pages: list[Page], site: Site) -> None:
    by_path: dict[str, Page] = {}
    for page in pages:
        if page.path in by_path:
            raise BuildError(f"{page.source} and {by_path[page.path].source} both use the path {page.path}")
        by_path[page.path] = page
    for page in pages:
        alternate = page.head.get("alternate")
        if not alternate:
            continue
        partner = by_path.get(alternate)
        if partner is None:
            raise BuildError(f"{page.source}: alternate {alternate} is not a page")
        if partner.lang == page.lang:
            raise BuildError(f"{page.source}: alternate {alternate} is in the same language")
        if partner.head.get("alternate") != page.path:
            raise BuildError(f"{page.source}: {alternate} does not point back to {page.path}")


def render(page: Page, base: str, site: Site) -> str:
    lang, other = page.lang, site.other(page.lang)
    alternate = page.head.get("alternate", "")
    links = ""
    if alternate:
        links = "\n".join(f'<link rel="alternate" hreflang="{code}" href="{site.base_url}{href}">'
                          for code, href in ((lang, page.path), (other, alternate)))
    values = site.values | {f"lang_{k}": str(v) for k, v in site.languages[lang].items()} | {
        "lang": lang,
        "other_lang": other,
        "other_label": str(site.languages[other].get("label", other.upper())),
        "other_url": alternate or str(site.languages[other]["home"]),
        "home": str(site.languages[lang]["home"]),
        "title": html.escape(page.head["title"]),
        "description": html.escape(page.head.get("description", "")),
        "canonical": site.base_url + page.path,
        "alternates": links,
        "robots": '<meta name="robots" content="noindex">' if page.head.get("noindex") == "yes" else "",
    }
    values["body"] = fill(page.body, values, site, page.source)
    return fill(base, values, site, site.root / "base.html")


def sitemap(pages: list[Page], site: Site) -> str:
    by_path = {p.path: p for p in pages}
    entries = []
    for page in pages:
        if not page.indexed:
            continue
        lines = [f"    <loc>{html.escape(site.base_url + page.path)}</loc>"]
        partner = by_path.get(page.head.get("alternate", ""))
        if partner is not None and partner.indexed:
            for p in (page, partner):
                lines.append(f'    <xhtml:link rel="alternate" hreflang="{p.lang}" '
                             f'href="{html.escape(site.base_url + p.path)}"/>')
        entries.append("  <url>\n" + "\n".join(lines) + "\n  </url>\n")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
            'xmlns:xhtml="http://www.w3.org/1999/xhtml">\n' + "".join(entries) + "</urlset>\n")


def target(out: Path, path: str) -> Path:
    file = out / path.lstrip("/") if path.endswith(".html") else out / path.lstrip("/") / "index.html"
    if not file.resolve().is_relative_to(out.resolve()):
        raise BuildError(f"the path {path} leads out of the output folder")
    return file


def prepare_out(out: Path, root: Path) -> None:
    """Replace the output folder, but only one this tool made (or an empty one), never the site or its parents."""
    out_r, root_r = out.resolve(), root.resolve()
    if out_r == root_r or root_r.is_relative_to(out_r) or out_r == Path(os.path.expanduser("~")).resolve():
        raise BuildError(f"{out} cannot be the output folder: it holds the site itself")
    if out.exists():
        if not out.is_dir() or (any(out.iterdir()) and not (out / MARKER).exists()):
            raise BuildError(f"{out} exists and was not made by this tool; choose an empty or new folder")
        shutil.rmtree(out)
    out.mkdir(parents=True)
    (out / MARKER).write_text("made by bilingual-site; the next build replaces this folder\n", encoding="utf-8")


def build(root: Path, out: Path | None = None, strict: bool = False) -> dict:
    site = Site.load(root)
    parts = urlsplit(site.base_url)
    if parts.scheme not in ("https", "http") or not parts.netloc:
        raise BuildError("site.toml: base_url must be a full address such as https://www.example.com")
    out = out or root / "dist"
    pages = [read_page(source, lang) for lang in site.languages
             for source in sorted((root / "pages" / lang).rglob("*.html"))]
    if not pages:
        raise BuildError(f"no pages under {root / 'pages'}")
    check_pairs(pages, site)
    drafts = [p.path for p in pages if p.head.get("draft") == "yes"]
    if drafts and strict:
        raise BuildError(f"pages marked as draft: {', '.join(drafts)}")
    base = (root / "base.html").read_text(encoding="utf-8")
    rendered = {page.path: render(page, base, site) for page in pages}  # everything checked before writing
    files = {target(out, path): text for path, text in rendered.items()}
    prepare_out(out, root)
    if (root / "assets").is_dir():
        shutil.copytree(root / "assets", out / "assets")
    if (root / "static").is_dir():
        shutil.copytree(root / "static", out, dirs_exist_ok=True)
    for file, text in files.items():
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text, encoding="utf-8")
    (out / "sitemap.xml").write_text(sitemap(pages, site), encoding="utf-8")
    if not (out / "robots.txt").exists():
        (out / "robots.txt").write_text(f"User-agent: *\nAllow: /\nSitemap: {site.base_url}/sitemap.xml\n",
                                        encoding="utf-8")
    return {"pages": len(pages), "indexed": sum(p.indexed for p in pages), "drafts": drafts, "out": out}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bilingual-site", description="Build a two-language static website.")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="build the site")
    b.add_argument("root", type=Path, help="folder with site.toml, base.html, pages/, assets/, static/")
    b.add_argument("--out", type=Path, help="output folder (default: <root>/dist)")
    b.add_argument("--strict", action="store_true", help="also fail on pages marked as draft")
    args = parser.parse_args(argv)
    try:
        result = build(args.root, args.out, args.strict)
    except BuildError as exc:
        print(f"bilingual-site: {exc}", file=sys.stderr)
        return 1
    print(f"{result['pages']} pages, {result['indexed']} in the sitemap, written to {result['out']}")
    if result["drafts"]:
        print(f"warning: draft pages: {', '.join(result['drafts'])}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

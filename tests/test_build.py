import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

import bilingual_site as bs

EXAMPLE = Path(__file__).resolve().parent.parent / "example"


@pytest.fixture
def site(tmp_path):
    root = tmp_path / "site"
    shutil.copytree(EXAMPLE, root, ignore=shutil.ignore_patterns("dist"))
    return root


def test_example_builds_with_pairs_sitemap_and_hashed_assets(site):
    result = bs.build(site)
    out = result["out"]
    assert (result["pages"], result["indexed"]) == (5, 4)
    page = (out / "de" / "ueber-uns" / "index.html").read_text()
    assert '<html lang="de">' in page
    assert '<link rel="alternate" hreflang="en" href="https://www.example.com/about/">' in page
    assert '<a class="switch" href="/about/" hreflang="en" lang="en">EN</a>' in page
    assert "Zum Inhalt" in page and "{{" not in page
    assert '<meta name="robots" content="noindex">' in (out / "404.html").read_text()
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9", "x": "http://www.w3.org/1999/xhtml"}
    urls = ET.parse(out / "sitemap.xml").getroot().findall("s:url", ns)
    assert sorted(u.find("s:loc", ns).text for u in urls) == [
        "https://www.example.com/", "https://www.example.com/about/", "https://www.example.com/de/",
        "https://www.example.com/de/ueber-uns/"]
    assert all(len(u.findall("x:link", ns)) == 2 for u in urls)
    assert "Sitemap: https://www.example.com/sitemap.xml" in (out / "robots.txt").read_text()
    assert (out / "_headers").exists() and (out / "assets" / "style.css").exists()


def test_changing_an_asset_changes_its_url(site):
    first = (bs.build(site)["out"] / "index.html").read_text()
    with (site / "assets" / "style.css").open("a") as f:
        f.write("\nh2 { color: red; }\n")
    second = (bs.build(site)["out"] / "index.html").read_text()
    def version(text):
        return text.split('href="/assets/style.css?v=')[1][:10]

    assert version(first) != version(second)


@pytest.mark.parametrize("change, message", [
    (lambda s: (s / "pages/de/about.html").write_text((s / "pages/de/about.html").read_text().replace(
        "alternate: /about/", "alternate: /")), "does not point back"),
    (lambda s: (s / "pages/en/extra.html").write_text("<!--\ntitle: X\npath: /about/\n-->\n<p>x</p>\n"),
     "both use the path /about/"),
    (lambda s: (s / "pages/en/about.html").write_text("<p>no header</p>"), "header comment is missing"),
    (lambda s: (s / "pages/en/extra.html").write_text("<!--\ntitle: X\npath: /x/\n-->\n{{nope}}\n"),
     "unknown placeholder {{nope}}"),
    (lambda s: (s / "pages/en/extra.html").write_text("<!--\ntitle: X\npath: /x/\n-->\n{{asset:gone.css}}\n"),
     "asset 'gone.css' not found"),
    (lambda s: (s / "pages/en/extra.html").write_text("<!--\ntitle: X\npath: /x y/\n-->\n<p>x</p>\n"),
     "paths start with /"),
    (lambda s: (s / "pages/en/extra.html").write_text("<!--\ntitle: X\npath: /x/\nalternate: /about/\n-->\n"),
     "is in the same language"),
])
def test_mistakes_stop_the_build_before_anything_is_written(site, change, message):
    change(site)
    with pytest.raises(bs.BuildError, match=message.replace("{", r"\{").replace("}", r"\}")):
        bs.build(site)
    assert not (site / "dist").exists()


def test_drafts_warn_and_fail_only_when_strict(site, capsys):
    page = site / "pages" / "en" / "about.html"
    page.write_text(page.read_text().replace("path: /about/", "path: /about/\ndraft: yes"))
    assert bs.main(["build", str(site)]) == 0
    assert "warning: draft pages: /about/" in capsys.readouterr().err
    assert bs.main(["build", str(site), "--strict"]) == 1


@pytest.mark.parametrize("header", ["path: /../../escape/", "path: /./x/", "path: /a/../b/"])
def test_paths_cannot_leave_the_output_folder(site, header):
    (site / "pages/en/extra.html").write_text(f"<!--\ntitle: X\n{header}\n-->\n<p>x</p>\n")
    with pytest.raises(bs.BuildError, match="paths start with /"):
        bs.build(site)


def test_includes_and_assets_stay_in_their_folders(site):
    (site.parent / "secret.html").write_text("secret")
    (site / "pages/en/extra.html").write_text("<!--\ntitle: X\npath: /x/\n-->\n{{include:../../secret}}\n")
    with pytest.raises(bs.BuildError, match="include '../../secret' not found"):
        bs.build(site)
    (site / "pages/en/extra.html").write_text("<!--\ntitle: X\npath: /x/\n-->\n{{asset:../site.toml}}\n")
    with pytest.raises(bs.BuildError, match="asset '../site.toml' not found"):
        bs.build(site)


def test_only_its_own_output_folder_is_replaced(site, tmp_path):
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "important.txt").write_text("keep me")
    for out in (foreign, site, site.parent):
        with pytest.raises(bs.BuildError):
            bs.build(site, out)
    assert (foreign / "important.txt").read_text() == "keep me" and (site / "site.toml").exists()
    first = bs.build(site, tmp_path / "out")["out"]
    assert (first / ".bilingual-site").exists()
    assert bs.build(site, first)["out"] == first  # its own folder can be rebuilt


def test_drafts_stay_out_of_the_sitemap(site):
    page = site / "pages" / "en" / "about.html"
    page.write_text(page.read_text().replace("path: /about/", "path: /about/\ndraft: yes"))
    assert "/about/" not in (bs.build(site)["out"] / "sitemap.xml").read_text().replace("/de/ueber-uns/", "")


def test_config_is_checked(site):
    (site / "site.toml").write_text('base_url = "https://x.example.com"\n[languages.en]\nhome = "/"\n')
    with pytest.raises(bs.BuildError, match="exactly two"):
        bs.build(site)

from __future__ import annotations

import argparse
import hashlib
import imghdr
import json
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import date, datetime
from html import escape
from pathlib import Path
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo


USER_AGENT = "Mozilla/5.0 (compatible; MalyPrincBBArchiver/1.0; +https://github.com)"
ATTR_URL_RE = re.compile(
    r'(?P<prefix>\b(?P<name>src|href|poster)\s*=\s*)(?P<quote>["\'])(?P<url>[^"\']+)(?P=quote)',
    re.IGNORECASE,
)
SRCSET_RE = re.compile(
    r'(?P<prefix>\bsrcset\s*=\s*)(?P<quote>["\'])(?P<value>[^"\']+)(?P=quote)',
    re.IGNORECASE,
)
STYLE_ATTR_RE = re.compile(
    r'(?P<prefix>\bstyle\s*=\s*)(?P<quote>["\'])(?P<value>.*?)(?P=quote)',
    re.IGNORECASE | re.DOTALL,
)
CSS_URL_RE = re.compile(r"url\((?P<quote>['\"]?)(?P<url>[^)\"']+)(?P=quote)\)")
DAY_RE = re.compile(r"Deň:\s*(?:</?strong>\s*)?(?P<day>\d{1,2})", re.IGNORECASE)
TITLE_RE = re.compile(r"<title>(?P<title>.*?)</title>", re.IGNORECASE | re.DOTALL)
COORD_RE = re.compile(r"coord\.info/(?P<code>[A-Z0-9]+)", re.IGNORECASE)
RESOURCE_SUFFIXES = {
    ".css",
    ".js",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".svg",
    ".webp",
    ".ico",
    ".bmp",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
    ".eot",
    ".pdf",
    ".mp3",
    ".mp4",
    ".webm",
    ".ogg",
}


@dataclass
class AssetRecord:
    url: str
    rel_path: Path
    full_path: Path
    content_type: str


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Archive daily Maly Princ puzzle pages.")
    parser.add_argument("--date", help="Archive date in YYYY-MM-DD format.")
    parser.add_argument("--timezone", default="Europe/Bratislava")
    parser.add_argument("--source-url", default="https://malyprinc.mikme.eu/")
    parser.add_argument("--generate-only", action="store_true")
    return parser.parse_args()


def archive_date(args: argparse.Namespace) -> date:
    if args.date:
        return datetime.strptime(args.date, "%Y-%m-%d").date()
    return datetime.now(ZoneInfo(args.timezone)).date()


def fetch_url(url: str) -> tuple[bytes, dict[str, str]]:
    with tempfile.TemporaryDirectory() as tmp_dir:
        headers_path = Path(tmp_dir) / "headers.txt"
        body_path = Path(tmp_dir) / "body.bin"
        cmd = [
            "curl",
            "-fsSL",
            "-A",
            USER_AGENT,
            "-D",
            str(headers_path),
            "-o",
            str(body_path),
            url,
        ]
        subprocess.run(cmd, check=True)
        headers = parse_headers(headers_path.read_text(encoding="utf-8", errors="replace"))
        return body_path.read_bytes(), headers


def parse_headers(headers_text: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for raw_line in headers_text.splitlines():
        line = raw_line.strip()
        if not line or ":" not in line:
            continue
        name, value = line.split(":", 1)
        parsed[name.strip().lower()] = value.strip()
    return parsed


def sanitize_piece(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value)
    cleaned = cleaned.strip("-._")
    return cleaned or "item"


def short_hash(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:10]


def guess_extension(url: str, headers: dict[str, str], content: bytes) -> str:
    parsed = urlparse(url)
    suffix = Path(parsed.path).suffix.lower()
    if suffix:
        return suffix

    content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
    extension = mimetypes.guess_extension(content_type) or ""
    if extension == ".jpe":
        extension = ".jpg"
    if extension:
        return extension

    image_type = imghdr.what(None, h=content)
    if image_type == "jpeg":
        return ".jpg"
    if image_type:
        return f".{image_type}"
    return ""


def target_rel_path(
    url: str,
    headers: dict[str, str],
    content: bytes,
    existing: set[Path],
    host_alias: str | None = None,
) -> Path:
    parsed = urlparse(url)
    host = sanitize_piece(host_alias or parsed.netloc or "root")
    path = parsed.path or "/"
    pure_path = Path(path.lstrip("/")) if path != "/" else Path("index")

    if pure_path.name:
        stem = sanitize_piece(pure_path.stem or pure_path.name)
        suffix = pure_path.suffix.lower()
    else:
        stem = "index"
        suffix = ""

    if not suffix:
        suffix = guess_extension(url, headers, content)

    if not suffix and headers.get("content-type", "").startswith("text/css"):
        suffix = ".css"

    if not suffix and headers.get("content-type", "").startswith("text/html"):
        suffix = ".html"

    if parsed.query:
        stem = f"{stem}-{short_hash(parsed.query)}"

    directory_parts = [sanitize_piece(part) for part in pure_path.parts[:-1] if part not in ("", ".")]
    candidate = Path("assets") / host
    for part in directory_parts:
        candidate /= part
    candidate /= f"{stem}{suffix}"

    if candidate not in existing:
        return candidate

    deduped = candidate.with_name(f"{candidate.stem}-{short_hash(url)}{candidate.suffix}")
    return deduped


def is_downloadable_href(url: str, source_host: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    if parsed.netloc == source_host:
        return True
    return Path(parsed.path).suffix.lower() in RESOURCE_SUFFIXES


def should_skip_url(url: str) -> bool:
    lowered = url.strip().lower()
    return (
        not lowered
        or lowered.startswith("#")
        or lowered.startswith("data:")
        or lowered.startswith("mailto:")
        or lowered.startswith("tel:")
        or lowered.startswith("javascript:")
    )


def ensure_asset(
    url: str,
    base_url: str,
    offline_root: Path,
    asset_cache: dict[str, AssetRecord],
    reserved_paths: set[Path],
) -> AssetRecord | None:
    if should_skip_url(url):
        return None

    absolute_url = urljoin(base_url, url)
    parsed = urlparse(absolute_url)
    if parsed.scheme not in ("http", "https"):
        return None

    if absolute_url in asset_cache:
        return asset_cache[absolute_url]

    content, headers = fetch_url(absolute_url)
    source_host = urlparse(base_url).netloc
    host_alias = "local" if parsed.netloc == source_host else None
    rel_path = target_rel_path(absolute_url, headers, content, reserved_paths, host_alias=host_alias)
    reserved_paths.add(rel_path)
    full_path = offline_root / rel_path
    record = AssetRecord(
        url=absolute_url,
        rel_path=rel_path,
        full_path=full_path,
        content_type=headers.get("content-type", ""),
    )
    asset_cache[absolute_url] = record
    full_path.parent.mkdir(parents=True, exist_ok=True)

    if record.content_type.startswith("text/css") or rel_path.suffix.lower() == ".css":
        css_text = content.decode("utf-8", errors="replace")
        rewritten = rewrite_css(css_text, absolute_url, record.full_path.parent, offline_root, asset_cache, reserved_paths)
        full_path.write_text(rewritten, encoding="utf-8")
    else:
        full_path.write_bytes(content)

    return record


def relative_url(from_dir: Path, to_path: Path) -> str:
    return Path(os.path.relpath(to_path, from_dir)).as_posix()


def rewrite_css(
    css_text: str,
    base_url: str,
    current_dir: Path,
    offline_root: Path,
    asset_cache: dict[str, AssetRecord],
    reserved_paths: set[Path],
) -> str:
    def replacer(match: re.Match[str]) -> str:
        raw_url = match.group("url").strip()
        asset = ensure_asset(raw_url, base_url, offline_root, asset_cache, reserved_paths)
        if not asset:
            return match.group(0)
        local_path = relative_url(current_dir, asset.full_path)
        return f'url("{local_path}")'

    return CSS_URL_RE.sub(replacer, css_text)


def rewrite_html(
    html_text: str,
    source_url: str,
    offline_root: Path,
    asset_cache: dict[str, AssetRecord],
    reserved_paths: set[Path],
) -> str:
    source_host = urlparse(source_url).netloc

    def replace_attr(match: re.Match[str]) -> str:
        attr_name = match.group("name").lower()
        raw_url = match.group("url").strip()
        if should_skip_url(raw_url):
            return match.group(0)
        absolute_url = urljoin(source_url, raw_url)
        if attr_name in ("src", "poster"):
            asset = ensure_asset(absolute_url, source_url, offline_root, asset_cache, reserved_paths)
        else:
            if not is_downloadable_href(absolute_url, source_host):
                return match.group(0)
            asset = ensure_asset(absolute_url, source_url, offline_root, asset_cache, reserved_paths)
        if not asset:
            return match.group(0)
        local = asset.rel_path.as_posix()
        return f'{match.group("prefix")}{match.group("quote")}{local}{match.group("quote")}'

    def replace_srcset(match: re.Match[str]) -> str:
        parts = []
        for chunk in match.group("value").split(","):
            segment = chunk.strip()
            if not segment:
                continue
            tokens = segment.split()
            raw_url = tokens[0]
            asset = ensure_asset(raw_url, source_url, offline_root, asset_cache, reserved_paths)
            if asset:
                tokens[0] = asset.rel_path.as_posix()
            parts.append(" ".join(tokens))
        return f'{match.group("prefix")}{match.group("quote")}{", ".join(parts)}{match.group("quote")}'

    def replace_style(match: re.Match[str]) -> str:
        current = match.group("value")
        rewritten = rewrite_css(current, source_url, offline_root, offline_root, asset_cache, reserved_paths)
        return f'{match.group("prefix")}{match.group("quote")}{rewritten}{match.group("quote")}'

    html_text = ATTR_URL_RE.sub(replace_attr, html_text)
    html_text = SRCSET_RE.sub(replace_srcset, html_text)
    html_text = STYLE_ATTR_RE.sub(replace_style, html_text)
    return html_text


def extract_day(html_text: str, fallback: int) -> int:
    match = DAY_RE.search(html_text)
    if not match:
        return fallback
    return int(match.group("day"))


def extract_title(html_text: str) -> str:
    match = TITLE_RE.search(html_text)
    if not match:
        return "Maly Princ"
    title = re.sub(r"\s+", " ", match.group("title")).strip()
    return title or "Maly Princ"


def extract_coord(html_text: str) -> str | None:
    match = COORD_RE.search(html_text)
    if not match:
        return None
    return match.group("code").upper()


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def mirror_snapshot(
    html_bytes: bytes,
    source_url: str,
    snapshot_root: Path,
    detected_day: int,
    archive_iso: str,
) -> dict:
    original_dir = snapshot_root / "original"
    offline_dir = snapshot_root / "offline"
    original_dir.mkdir(parents=True, exist_ok=True)
    offline_dir.mkdir(parents=True, exist_ok=True)

    original_path = original_dir / "index.html"
    original_path.write_bytes(html_bytes)
    source_txt_path = original_dir / "source.txt"

    html_text = html_bytes.decode("utf-8", errors="replace")
    source_txt_path.write_text(html_text, encoding="utf-8")
    asset_cache: dict[str, AssetRecord] = {}
    reserved_paths: set[Path] = set()
    offline_html = rewrite_html(html_text, source_url, offline_dir, asset_cache, reserved_paths)
    (offline_dir / "index.html").write_text(offline_html, encoding="utf-8")

    metadata = {
        "archive_date": archive_iso,
        "day": detected_day,
        "title": extract_title(html_text),
        "coord": extract_coord(html_text),
        "source_url": source_url,
        "original_path": original_path.relative_to(repo_root()).as_posix(),
        "source_txt_path": source_txt_path.relative_to(repo_root()).as_posix(),
        "offline_path": (offline_dir / "index.html").relative_to(repo_root()).as_posix(),
        "offline_pdf_path": (offline_dir / "page.pdf").relative_to(repo_root()).as_posix(),
    }
    write_json(snapshot_root / "meta.json", metadata)
    return metadata


def replace_tree(target: Path, source: Path) -> None:
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)


def collect_day_metadata(root: Path) -> dict[int, dict]:
    result: dict[int, dict] = {}
    for day in range(1, 31):
        meta_path = root / "days" / f"{day:02d}" / "meta.json"
        if meta_path.exists():
            result[day] = json.loads(meta_path.read_text(encoding="utf-8"))
    return result


def render_index(root: Path) -> None:
    metadata = collect_day_metadata(root)
    captured_count = len(metadata)
    latest = None
    if metadata:
        latest = max(metadata.values(), key=lambda item: item.get("archive_date", ""))

    cards: list[str] = []
    for day in range(1, 31):
        item = metadata.get(day)
        if item:
            title = escape(item.get("title") or f"Den {day:02d}")
            archive_iso = escape(item.get("archive_date", ""))
            coord = escape(item.get("coord") or "-")
            cards.append(
                f"""
        <article class="day-card is-ready">
          <p class="day-label">Den {day:02d}</p>
          <h2>{title}</h2>
          <p class="card-meta">GC kod: {coord}</p>
          <p class="card-meta">Archivovane: {archive_iso}</p>
          <div class="card-links">
            <a href="days/{day:02d}/original/">Povodne HTML</a>
            <a href="days/{day:02d}/original/source.txt">Zdroj TXT</a>
            <a href="days/{day:02d}/offline/">Offline verzia</a>
            <a href="days/{day:02d}/offline/page.pdf">PDF</a>
          </div>
        </article>
"""
            )
        else:
            cards.append(
                f"""
        <article class="day-card is-pending">
          <p class="day-label">Den {day:02d}</p>
          <h2>Caka na archivaciu</h2>
          <p class="card-meta">Workflow ulozi stranku po polnoci.</p>
        </article>
"""
            )

    latest_block = ""
    if latest:
        latest_day = int(latest["day"])
        latest_block = f"""
      <section class="latest-box">
        <p class="eyebrow">Posledny zachyteny den</p>
        <h2>Den {latest_day:02d}</h2>
        <p>{escape(latest.get("title") or "Maly Princ")}</p>
        <div class="card-links">
          <a href="days/{latest_day:02d}/original/">Povodne HTML</a>
          <a href="days/{latest_day:02d}/original/source.txt">Zdroj TXT</a>
          <a href="days/{latest_day:02d}/offline/">Offline verzia</a>
          <a href="days/{latest_day:02d}/offline/page.pdf">PDF</a>
        </div>
      </section>
"""

    html = f"""<!DOCTYPE html>
<html lang="sk">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>MalyPrincBB Archive</title>
  <link rel="stylesheet" href="assets/site.css">
</head>
<body>
  <main class="page-shell">
    <section class="hero">
      <div class="hero-copy">
        <p class="eyebrow">GitHub Pages archiv</p>
        <h1>Maly Princ BB</h1>
        <p class="intro">
          Denne archivovana stranka <code>malyprinc.mikme.eu</code>
          s dvoma verziami pre kazdy den: presne povodne HTML a lokalna offline kopia.
        </p>
        <div class="hero-stats">
          <div>
            <strong>{captured_count}</strong>
            <span>ulozenych dni</span>
          </div>
          <div>
            <strong>30</strong>
            <span>planovanych uloh</span>
          </div>
        </div>
      </div>
{latest_block}
    </section>

    <section class="info-strip">
      <p>Automatizacia bezi kazdy den okolo 00:10 v casovej zone Europe/Bratislava.</p>
      <p>Adresare <code>snapshots/YYYY-MM-DD</code> drzia dennu historiu, <code>days/01-30</code> drzia stabilne odkazy podla dna.</p>
    </section>

    <section class="day-grid">
{''.join(cards)}
    </section>
  </main>
</body>
</html>
"""
    (root / "index.html").write_text(html, encoding="utf-8")


def run_archive(args: argparse.Namespace) -> dict | None:
    root = repo_root()
    render_index(root)
    if args.generate_only:
        return None

    archive_day = archive_date(args)
    archive_iso = archive_day.isoformat()
    html_bytes, _headers = fetch_url(args.source_url)
    html_text = html_bytes.decode("utf-8", errors="replace")
    detected_day = extract_day(html_text, archive_day.day)
    snapshot_root = root / "snapshots" / archive_iso
    metadata = mirror_snapshot(html_bytes, args.source_url, snapshot_root, detected_day, archive_iso)

    if 1 <= detected_day <= 30:
        day_root = root / "days" / f"{detected_day:02d}"
        day_root.mkdir(parents=True, exist_ok=True)
        replace_tree(day_root / "original", snapshot_root / "original")
        replace_tree(day_root / "offline", snapshot_root / "offline")
        write_json(day_root / "meta.json", metadata)

    render_index(root)
    return metadata


def main() -> None:
    args = parse_args()
    metadata = run_archive(args)
    if metadata:
        print(json.dumps(metadata, ensure_ascii=False))


if __name__ == "__main__":
    main()

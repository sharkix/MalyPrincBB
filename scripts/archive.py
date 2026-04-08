from __future__ import annotations

import argparse
import hashlib
try:
    import imghdr
except ModuleNotFoundError:  # Python 3.13+
    imghdr = None
import json
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
from dataclasses import dataclass
from datetime import date, datetime
from html import escape
from pathlib import Path
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo


USER_AGENT = "Mozilla/5.0 (compatible; MalyPrincBBArchiver/1.0; +https://github.com)"
AUTH_SESSION_KEY = "mpbb-auth-v1"
AUTH_BYPASS_PARAM = "mpbb_render"
AUTH_HASH_RE = re.compile(r'const HASH = "([0-9a-f]{64})";')
AUTH_BLOCK_RE = re.compile(
    r'(?P<indent>^[ \t]*)<style id="mpbb-auth-style">.*?</style>\s*<script id="mpbb-auth-script">.*?</script>',
    re.IGNORECASE | re.DOTALL | re.MULTILINE,
)
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
STYLE_TAG_RE = re.compile(r"(?P<open><style\b[^>]*>)(?P<value>.*?)(?P<close></style>)", re.IGNORECASE | re.DOTALL)
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

    if imghdr is not None:
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


def is_relative_url(url: str) -> bool:
    parsed = urlparse(url.strip())
    return not parsed.scheme and not parsed.netloc and not url.startswith("//")


def absolutize_css_urls(css_text: str, base_url: str) -> str:
    def replacer(match: re.Match[str]) -> str:
        raw_url = match.group("url").strip()
        if should_skip_url(raw_url) or not is_relative_url(raw_url):
            return match.group(0)
        absolute_url = urljoin(base_url, raw_url)
        return f'url("{absolute_url}")'

    return CSS_URL_RE.sub(replacer, css_text)


def configured_auth_hash(root: Path) -> str | None:
    explicit_hash = os.environ.get("MPBB_PASSWORD_HASH", "").strip().lower()
    if explicit_hash:
        return explicit_hash

    plain_password = os.environ.get("MPBB_SITE_PASSWORD")
    if plain_password:
        return hashlib.sha256(plain_password.encode("utf-8")).hexdigest()

    index_path = root / "index.html"
    if index_path.exists():
        match = AUTH_HASH_RE.search(index_path.read_text(encoding="utf-8", errors="replace"))
        if match:
            return match.group(1)

    return None


def resolve_auth_hash(root: Path) -> str:
    auth_hash = configured_auth_hash(root)
    if auth_hash:
        return auth_hash
    raise RuntimeError(
        "Missing MPBB_SITE_PASSWORD or MPBB_PASSWORD_HASH. "
        "Configure the GitHub secret MPBB_SITE_PASSWORD or regenerate from an existing index.html."
    )


def auth_gate_markup(auth_hash: str, indent: str = "  ") -> str:
    markup = textwrap.dedent(
        f"""
<style id="mpbb-auth-style">
  body {{
    display: none !important;
  }}
</style>
<script id="mpbb-auth-script">
  (() => {{
    const HASH = "{auth_hash}";
    const KEY = "{AUTH_SESSION_KEY}";
    const BYPASS_PARAM = "{AUTH_BYPASS_PARAM}";
    const storages = [];

    try {{
      storages.push(window.localStorage);
    }} catch (error) {{
    }}

    try {{
      storages.push(window.sessionStorage);
    }} catch (error) {{
    }}

    const reveal = () => {{
      const style = document.getElementById("mpbb-auth-style");
      if (style) {{
        style.remove();
      }}
      if (document.body) {{
        document.body.style.removeProperty("display");
      }}
    }};

    const gateHtml = (message = "") => `
<main style="min-height:100vh;display:grid;place-items:center;padding:24px;background:#f6f0e1;color:#2f2518;font-family:Georgia,'Times New Roman',serif;">
  <div style="width:min(100%,26rem);padding:32px 28px;border:1px solid rgba(47,37,24,.18);border-radius:18px;background:rgba(255,250,240,.96);box-shadow:0 16px 40px rgba(47,37,24,.12);">
    <h1 style="margin:0 0 10px;font-size:2rem;text-align:center;">Maly Princ BB</h1>
    <p style="margin:0 0 20px;text-align:center;line-height:1.5;">Zadaj heslo pre odomknutie archívu. Po úspešnom prihlásení bude platiť aj v ďalších taboch.</p>
    <p style="margin:0 0 16px;color:#9f2d2d;text-align:center;font-weight:600;${{message ? '' : 'display:none;'}}">${{message}}</p>
    <form id="mpbb-auth-form" style="display:grid;gap:12px;">
      <div style="display:grid;grid-template-columns:minmax(0,1fr) auto;gap:10px;align-items:center;">
        <input id="mpbb-auth-input" type="password" autocomplete="current-password" placeholder="Heslo" style="width:100%;padding:12px 14px;border:1px solid rgba(47,37,24,.25);border-radius:12px;background:#fffdf8;color:#2f2518;font:inherit;">
        <button id="mpbb-auth-toggle" type="button" aria-pressed="false" style="padding:12px 14px;border:1px solid rgba(47,37,24,.18);border-radius:12px;background:#efe5cf;color:#2f2518;font:inherit;font-weight:600;cursor:pointer;white-space:nowrap;">Zobraziť</button>
      </div>
      <button id="mpbb-auth-submit" type="submit" style="padding:12px 16px;border:0;border-radius:12px;background:#2f2518;color:#fffdf8;font:inherit;font-weight:700;cursor:pointer;">Odomknúť</button>
    </form>
  </div>
</main>`;

    const readStoredAuth = () => {{
      for (const storage of storages) {{
        try {{
          const storedHash = storage.getItem(KEY);
          if (storedHash === HASH) {{
            return true;
          }}
          if (storedHash) {{
            storage.removeItem(KEY);
          }}
        }} catch (error) {{
        }}
      }}
      return false;
    }};

    const persistAuth = () => {{
      for (const storage of storages) {{
        try {{
          storage.setItem(KEY, HASH);
        }} catch (error) {{
        }}
      }}
    }};

    const shouldBypass = () => new URLSearchParams(window.location.search).has(BYPASS_PARAM);

    async function sha256(value) {{
      const bytes = new TextEncoder().encode(value);
      const digest = await crypto.subtle.digest("SHA-256", bytes);
      return Array.from(new Uint8Array(digest))
        .map((byte) => byte.toString(16).padStart(2, "0"))
        .join("");
    }}

    const showGate = (message = "") => {{
      const render = () => {{
        if (!document.body) {{
          return;
        }}

        const style = document.getElementById("mpbb-auth-style");
        if (style) {{
          style.remove();
        }}

        document.body.innerHTML = gateHtml(message);
        document.body.style.display = "block";

        const form = document.getElementById("mpbb-auth-form");
        const input = document.getElementById("mpbb-auth-input");
        const toggle = document.getElementById("mpbb-auth-toggle");
        const submit = document.getElementById("mpbb-auth-submit");

        if (input instanceof HTMLInputElement) {{
          input.focus();
        }}

        if (toggle instanceof HTMLButtonElement && input instanceof HTMLInputElement) {{
          toggle.addEventListener("click", () => {{
            const revealPassword = input.type === "password";
            input.type = revealPassword ? "text" : "password";
            toggle.textContent = revealPassword ? "Skryť" : "Zobraziť";
            toggle.setAttribute("aria-pressed", revealPassword ? "true" : "false");
            input.focus();
          }});
        }}

        if (!(form instanceof HTMLFormElement) || !(input instanceof HTMLInputElement)) {{
          return;
        }}

        form.addEventListener("submit", async (event) => {{
          event.preventDefault();

          if (submit instanceof HTMLButtonElement) {{
            submit.disabled = true;
          }}

          const password = input.value;
          if (!password) {{
            showGate("Zadaj heslo.");
            return;
          }}

          if (await sha256(password) === HASH) {{
            persistAuth();
            window.location.reload();
            return;
          }}

          showGate("Nesprávne heslo.");
        }});
      }};

      if (document.readyState === "loading") {{
        document.addEventListener("DOMContentLoaded", render, {{ once: true }});
        return;
      }}

      render();
    }};

    window.addEventListener("storage", (event) => {{
      if (event.key === KEY && event.newValue === HASH) {{
        window.location.reload();
      }}
    }});

    async function main() {{
      if (shouldBypass()) {{
        reveal();
        return;
      }}

      if (readStoredAuth()) {{
        reveal();
        return;
      }}

      showGate();
    }}

    main();
  }})();
</script>"""
    ).strip("\n")
    return "\n".join(f"{indent}{line}" if line else "" for line in markup.splitlines())


def inject_auth_gate(html_text: str, auth_hash: str) -> str:
    match = AUTH_BLOCK_RE.search(html_text)
    if match:
        return AUTH_BLOCK_RE.sub(auth_gate_markup(auth_hash, indent=match.group("indent")), html_text, count=1)
    if 'id="mpbb-auth-script"' in html_text or 'id="mpbb-auth-style"' in html_text:
        html_text = re.sub(
            r'<style id="mpbb-auth-style">.*?</style>',
            "",
            html_text,
            count=1,
            flags=re.IGNORECASE | re.DOTALL,
        )
        html_text = re.sub(
            r'<script id="mpbb-auth-script">.*?</script>',
            "",
            html_text,
            count=1,
            flags=re.IGNORECASE | re.DOTALL,
        )
    return re.sub(r"</head>", f"{auth_gate_markup(auth_hash)}\n</head>", html_text, count=1, flags=re.IGNORECASE)


def refresh_existing_auth_pages(root: Path, auth_hash: str) -> None:
    for html_path in root.rglob("*.html"):
        if any(part in {".git", "node_modules"} for part in html_path.parts):
            continue
        html_text = html_path.read_text(encoding="utf-8", errors="replace")
        updated = inject_auth_gate(html_text, auth_hash)
        if updated != html_text:
            html_path.write_text(updated, encoding="utf-8")


def rewrite_original_html(html_text: str, source_url: str, auth_hash: str) -> str:
    def replace_attr(match: re.Match[str]) -> str:
        raw_url = match.group("url").strip()
        if should_skip_url(raw_url) or not is_relative_url(raw_url):
            return match.group(0)
        absolute_url = urljoin(source_url, raw_url)
        return f'{match.group("prefix")}{match.group("quote")}{absolute_url}{match.group("quote")}'

    def replace_srcset(match: re.Match[str]) -> str:
        parts = []
        for chunk in match.group("value").split(","):
            segment = chunk.strip()
            if not segment:
                continue
            tokens = segment.split()
            raw_url = tokens[0]
            if is_relative_url(raw_url) and not should_skip_url(raw_url):
                tokens[0] = urljoin(source_url, raw_url)
            parts.append(" ".join(tokens))
        return f'{match.group("prefix")}{match.group("quote")}{", ".join(parts)}{match.group("quote")}'

    def replace_style_attr(match: re.Match[str]) -> str:
        rewritten = absolutize_css_urls(match.group("value"), source_url)
        return f'{match.group("prefix")}{match.group("quote")}{rewritten}{match.group("quote")}'

    def replace_style_tag(match: re.Match[str]) -> str:
        rewritten = absolutize_css_urls(match.group("value"), source_url)
        return f'{match.group("open")}{rewritten}{match.group("close")}'

    html_text = ATTR_URL_RE.sub(replace_attr, html_text)
    html_text = SRCSET_RE.sub(replace_srcset, html_text)
    html_text = STYLE_ATTR_RE.sub(replace_style_attr, html_text)
    html_text = STYLE_TAG_RE.sub(replace_style_tag, html_text)
    return inject_auth_gate(html_text, auth_hash)


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


def slovak_plural_form(count: int, singular: str, few: str, many: str) -> str:
    if count == 1:
        return singular
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return few
    return many


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def mirror_snapshot(
    html_bytes: bytes,
    source_url: str,
    snapshot_root: Path,
    detected_day: int,
    archive_iso: str,
    auth_hash: str,
) -> dict:
    original_dir = snapshot_root / "original"
    offline_dir = snapshot_root / "offline"
    original_dir.mkdir(parents=True, exist_ok=True)
    offline_dir.mkdir(parents=True, exist_ok=True)

    original_path = original_dir / "index.html"
    source_txt_path = original_dir / "source.txt"

    html_text = html_bytes.decode("utf-8", errors="replace")
    original_html = rewrite_original_html(html_text, source_url, auth_hash)
    original_path.write_text(original_html, encoding="utf-8")
    source_txt_path.write_text(html_text, encoding="utf-8")
    asset_cache: dict[str, AssetRecord] = {}
    reserved_paths: set[Path] = set()
    offline_html = rewrite_html(html_text, source_url, offline_dir, asset_cache, reserved_paths)
    offline_html = inject_auth_gate(offline_html, auth_hash)
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


def render_index(root: Path, auth_hash: str) -> None:
    metadata = collect_day_metadata(root)
    captured_count = len(metadata)
    missing_count = max(0, 30 - captured_count)
    captured_label = slovak_plural_form(captured_count, "stiahnutý deň", "stiahnuté dni", "stiahnutých dní")
    missing_label = slovak_plural_form(missing_count, "zostávajúci deň", "zostávajúce dni", "zostávajúcich dní")
    latest = None
    if metadata:
        latest = max(metadata.values(), key=lambda item: item.get("archive_date", ""))

    cards: list[str] = []
    for day in range(1, 31):
        item = metadata.get(day)
        if item:
            title = escape(item.get("title") or f"Deň {day:02d}")
            archive_iso = escape(item.get("archive_date", ""))
            coord = escape(item.get("coord") or "-")
            cards.append(
                f"""
        <article class="day-card is-ready">
          <p class="day-label">Deň {day:02d}</p>
          <h2>{title}</h2>
          <p class="card-meta">GC kód: {coord}</p>
          <p class="card-meta">Archivované: {archive_iso}</p>
          <div class="card-links">
            <a href="days/{day:02d}/original/">Pôvodné HTML</a>
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
          <p class="day-label">Deň {day:02d}</p>
          <h2>Čaká na archiváciu</h2>
          <p class="card-meta">Workflow uloží stránku po polnoci.</p>
        </article>
"""
            )

    latest_block = ""
    if latest:
        latest_day = int(latest["day"])
        latest_block = f"""
      <section class="latest-box">
        <p class="eyebrow">Posledný zachytený deň</p>
        <h2>Deň {latest_day:02d}</h2>
        <p>{escape(latest.get("title") or "Maly Princ")}</p>
        <div class="card-links">
          <a href="days/{latest_day:02d}/original/">Pôvodné HTML</a>
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
{auth_gate_markup(auth_hash)}
</head>
<body>
  <main class="page-shell">
    <section class="hero">
      <div class="hero-copy">
        <p class="eyebrow">GitHub Pages archív</p>
        <h1>Maly Princ BB</h1>
        <p class="intro">
          Denne archivovaná stránka <code>malyprinc.mikme.eu</code>
          s dvoma verziami pre každý deň: presne pôvodné HTML a lokálna offline kópia.
        </p>
        <div class="hero-stats">
          <div>
            <strong>{captured_count}</strong>
            <span>{captured_label}</span>
          </div>
          <div>
            <strong>{missing_count}</strong>
            <span>{missing_label}</span>
          </div>
        </div>
      </div>
{latest_block}
    </section>

    <section class="info-strip">
      <p>Automatizácia beží každý deň okolo 00:10 v časovej zóne Europe/Bratislava.</p>
      <p>Adresáre <code>snapshots/YYYY-MM-DD</code> držia dennú históriu, <code>days/01-30</code> držia stabilné odkazy podľa dňa.</p>
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
    auth_hash = resolve_auth_hash(root)
    render_index(root, auth_hash)
    refresh_existing_auth_pages(root, auth_hash)
    if args.generate_only:
        return None

    archive_day = archive_date(args)
    archive_iso = archive_day.isoformat()
    html_bytes, _headers = fetch_url(args.source_url)
    html_text = html_bytes.decode("utf-8", errors="replace")
    detected_day = extract_day(html_text, archive_day.day)
    snapshot_root = root / "snapshots" / archive_iso
    metadata = mirror_snapshot(html_bytes, args.source_url, snapshot_root, detected_day, archive_iso, auth_hash)

    if 1 <= detected_day <= 30:
        day_root = root / "days" / f"{detected_day:02d}"
        day_root.mkdir(parents=True, exist_ok=True)
        replace_tree(day_root / "original", snapshot_root / "original")
        replace_tree(day_root / "offline", snapshot_root / "offline")
        write_json(day_root / "meta.json", metadata)

    render_index(root, auth_hash)
    refresh_existing_auth_pages(root, auth_hash)
    return metadata


def main() -> None:
    args = parse_args()
    metadata = run_archive(args)
    if metadata:
        print(json.dumps(metadata, ensure_ascii=False))


if __name__ == "__main__":
    main()

"""Build a reproducible, allowlisted MaiBot plugin ZIP without local secrets."""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[1]
_PLACEHOLDER_WARNED = False
ROOT_FILES = (
    "plugin.py", "_manifest.json", "requirements.txt", "pyproject.toml",
    "config.example.toml", "README.md", "LICENSE", ".gitignore",
)


def validate_manifest_metadata(manifest: dict) -> None:
    """Fail packaging on missing installer-required metadata, including nested fields.

    This is a local preflight, not a replacement for the MaiBot Host validator.
    """
    required = ("manifest_version", "id", "plugin_type", "version", "name", "description",
                "author.name", "author.url", "license", "urls", "urls.repository",
                "host_application.min_version", "host_application.max_version",
                "sdk.min_version", "sdk.max_version", "capabilities",
                "i18n.default_locale", "i18n.supported_locales")
    for field in required:
        value = manifest
        for key in field.split("."):
            value = value.get(key) if isinstance(value, dict) else None
        if value is None or value == "":
            raise ValueError("Missing required manifest field: " + field)
    for field, value in (("author.url", manifest["author"]["url"]),
                         ("urls.repository", manifest["urls"]["repository"])):
        if not isinstance(value, str) or not value.startswith(("http://", "https://")):
            raise ValueError("Manifest URL must start with http:// or https://: " + field)
    # The plugin marketplace requires a public GitHub repository for new and
    # modified submissions; other hosts fail market validation.
    if not manifest["urls"]["repository"].startswith("https://github.com/"):
        raise ValueError("Marketplace requires urls.repository to be an https://github.com/ URL")
    github_links = [manifest["author"]["url"]] + [
        value for value in (manifest["urls"].get("homepage"), manifest["urls"].get("documentation"),
                            manifest["urls"].get("issues")) if value
    ]
    if any(not str(value).startswith("https://github.com/") for value in github_links):
        raise ValueError("Marketplace links must use https://github.com/")
    global _PLACEHOLDER_WARNED
    if not _PLACEHOLDER_WARNED and ("username" in manifest["urls"]["repository"]
                                    or "username" in manifest["id"]):
        _PLACEHOLDER_WARNED = True
        print("WARNING: placeholder owner 'username' remains; replace it before market submission.")


def build() -> Path:
    manifest = json.loads((ROOT / "_manifest.json").read_text(encoding="utf8"))
    validate_manifest_metadata(manifest)
    example = tomllib.loads((ROOT / "config.example.toml").read_text(encoding="utf8"))
    if example.get("image", {}).get("api_key", "") != "":
        raise RuntimeError("Release example image.api_key must be empty; refusing to package a secret")
    tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf8"))
    sources = [ROOT / name for name in ROOT_FILES]
    for folder in ("daily_life", "tests", "scripts"):
        sources.extend(sorted((ROOT / folder).glob("*.py")))
    for source in sources:
        if not source.is_file() or source.is_symlink():
            raise RuntimeError("Missing or symlinked release source: " + source.name)
        if source.suffix == ".py":
            ast.parse(source.read_text(encoding="utf8"), filename=source.name, feature_version=(3, 11))
    output = ROOT / "dist" / ("maibot-daily-life-" + manifest["version"] + ".zip")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for source in sorted(sources):
            relative = source.relative_to(ROOT).as_posix()
            item = zipfile.ZipInfo(relative, date_time=(2026, 1, 1, 0, 0, 0))
            item.compress_type = zipfile.ZIP_DEFLATED
            item.create_system = 3
            item.external_attr = 0o100644 << 16
            archive.writestr(item, source.read_bytes())
    with zipfile.ZipFile(output) as archive:
        if archive.testzip() is not None:
            raise RuntimeError("Corrupt release archive")
        entries = archive.namelist()
        if "_manifest.json" not in entries or "plugin.py" not in entries:
            raise RuntimeError("Plugin entry points missing at ZIP root")
        forbidden = {"config.toml", ".env", ".venv", "__pycache__", "dist"}
        if any(forbidden.intersection(Path(name).parts) for name in entries):
            raise RuntimeError("Private or generated files found in release")
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    print(str(output.relative_to(ROOT)))
    print(f"{len(entries)} files; {output.stat().st_size} bytes")
    print("SHA256 " + digest)
    return output


if __name__ == "__main__":
    build()

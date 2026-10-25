import gzip
import logging
import xml.etree.ElementTree as ET
import re
import string
import os
from typing import Dict

log = logging.getLogger(__name__)


def _normalize_title(title: str) -> str:
    """Normalize a title: lower‑case, remove punctuation, collapse whitespace.
    This mirrors the normalization used for MAL status keys.
    """
    if not title:
        return ""
    # Lower case
    title = title.lower()
    # Remove punctuation
    title = title.translate(str.maketrans("", "", string.punctuation))
    # Collapse whitespace
    title = re.sub(r"\s+", " ", title).strip()
    return title


def _parse_file(path: str) -> Dict[str, str]:
    """Parse a single MAL XML gzip file and return a mapping of normalized title → status.
    The XML format (MyAnimeList export) contains either <anime> or <manga> entries.
    Expected fields:
        - <series_title> for anime entries
        - <manga_title> for manga entries (some exports also use <series_title>)
        - <my_status>  (e.g. "Completed", "Watching", "Plan to Watch")
    """
    status_map: Dict[str, str] = {}
    if not os.path.isfile(path):
        return status_map
    try:
        with gzip.open(path, "rb") as f:
            data = f.read()
        root = ET.fromstring(data)
    except Exception as exc:
        # Corrupt file – return empty mapping, but say so: an empty map would
        # otherwise silently turn real titles into "not yet out".
        log.warning("mal_status: could not parse %s (%s); using empty "
                    "status map", path, exc)
        return status_map

    # Process <anime> entries
    for entry in root.findall(".//anime"):
        title = entry.findtext("series_title") or entry.findtext("title")
        status = entry.findtext("my_status")
        if title and status:
            norm = _normalize_title(title)
            if norm:
                status_map[norm] = status

    # Process <manga> entries
    for entry in root.findall(".//manga"):
        # Some exports use <series_title>, others <manga_title>
        title = (
            entry.findtext("series_title")
            or entry.findtext("manga_title")
            or entry.findtext("title")
        )
        status = entry.findtext("my_status")
        if title and status:
            norm = _normalize_title(title)
            if norm:
                status_map[norm] = status
    return status_map


def load_mal_status() -> Dict[str, str]:
    """Load MAL status from both anime.xml.gz and manga.xml.gz under config/mal.
    Returns a dict mapping normalized title → status string.
    """
    base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config", "mal")
    anime_path = os.path.join(base_dir, "anime.xml.gz")
    manga_path = os.path.join(base_dir, "manga.xml.gz")
    status_map: Dict[str, str] = {}
    status_map.update(_parse_file(anime_path))
    status_map.update(_parse_file(manga_path))
    return status_map


if __name__ == "__main__":
    mal = load_mal_status()
    # Show up to 5 example title/status pairs
    for i, (title, status) in enumerate(mal.items()):
        if i >= 5:
            break
        print(f"{title!r}: {status!r}")

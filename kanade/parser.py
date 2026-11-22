"""Filename parser for AnimeThemes-style basenames.

AnimeThemes naming pattern (all .webm):
    <CompressedTitle>-<OP|ED><number?><part letter?><v<version>?>(-<Tag>)*.webm
    ShingekiNoKyojin-OP1.webm
    BokuNoHeroAcademia-ED3v2.webm
    Oresuki-ED1-NCBD1080.webm
    GrisaiaNoKajitsuVN-ED1a.webm
    9Jikan9Nin9NoTobira-OP1.webm
Folder layout: <year>/<season>/file, <decade>s/file, misc/file.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import PurePath

VIDEO_EXTS = {".webm", ".mkv", ".mp4", ".m4v", ".avi", ".mov"}
SEASONS = {"winter": "Winter", "spring": "Spring", "summer": "Summer", "fall": "Fall", "autumn": "Fall"}

_THEME_RE = re.compile(
    r"^(?P<prefix>.+?)-(?P<type>OP|ED)(?P<num>\d*)(?P<part>[a-z])?(?:v(?P<ver>\d+))?(?P<tags>(?:-[A-Za-z0-9]+)*)$",
    re.IGNORECASE,
)
_CAMEL_RE = re.compile(
    r"(?<=[a-z])(?=[A-Z])"          # fooBar
    r"|(?<=[A-Z])(?=[A-Z][a-z])"     # ABCDef -> ABC Def
    r"|(?<=[A-Za-z])(?=\d)"          # Season2 -> Season 2
    r"|(?<=\d)(?=[A-Za-z]{2,})"      # 2Nd -> 2 Nd
    r"|(?<=\d)(?=S\d)"               # 00S2 -> 00 S2
)


@dataclass
class ParsedName:
    filename: str
    stem: str
    prefix: str = ""
    title_guess: str = ""
    type: str | None = None
    number: int = 1
    part: str = ""
    version: int = 1
    tags: list[str] = field(default_factory=list)
    season_number: int | None = None
    ok: bool = False

    @property
    def slug(self) -> str:
        if not self.type:
            return ""
        s = f"{self.type}{self.number}{self.part}"
        if self.version > 1:
            s += f"v{self.version}"
        return s


def decamel(text: str) -> str:
    """'BocchiTheRock' -> 'Bocchi The Rock', 'KiniroMosaicS2' -> 'Kiniro Mosaic S2'."""
    text = text.replace("_", " ").replace(".", " ")
    out = _CAMEL_RE.sub(" ", text)
    # glue back season markers like 'S 2' -> 'S2'
    out = re.sub(r"\bS (\d+)\b", r"S\1", out)
    out = re.sub(r"\s+", " ", out).strip()
    return out


def parse_filename(filename: str) -> ParsedName:
    p = PurePath(filename)
    stem = p.stem
    res = ParsedName(filename=p.name, stem=stem)
    m = _THEME_RE.match(stem)
    if not m:
        res.prefix = stem
        res.title_guess = decamel(stem)
        return res
    res.ok = True
    res.prefix = m.group("prefix")
    res.type = m.group("type").upper()
    res.number = int(m.group("num")) if m.group("num") else 1
    res.part = (m.group("part") or "").lower()
    res.version = int(m.group("ver")) if m.group("ver") else 1
    tags = m.group("tags") or ""
    res.tags = [t for t in tags.split("-") if t]
    res.title_guess = decamel(res.prefix)
    sm = re.search(r"S(\d+)$", res.prefix)
    if sm:
        res.season_number = int(sm.group(1))
    return res


def search_title(parsed: ParsedName) -> str:
    """A cleaner query for metadata search: drop trailing season markers like 'S2'."""
    t = parsed.title_guess
    t = re.sub(r"\bS(\d+)$", r"Season \1", t)
    return t


@dataclass
class FolderInfo:
    folder: str = ""
    year: int | None = None
    season: str | None = None
    decade: int | None = None


def parse_folder(rel_parts: tuple[str, ...]) -> FolderInfo:
    """rel_parts are the directory components below the archive root."""
    info = FolderInfo()
    if not rel_parts:
        return info
    top = rel_parts[0]
    info.folder = top
    if re.fullmatch(r"(19|20)\d\d", top):
        info.year = int(top)
    else:
        dm = re.fullmatch(r"(\d0)s", top, re.IGNORECASE)
        if dm:
            d = int(dm.group(1))
            info.decade = 1900 + d if d >= 30 else 2000 + d
    for part in rel_parts[1:]:
        s = SEASONS.get(part.lower())
        if s:
            info.season = s
            break
    return info


def norm(text: str | None) -> str:
    """Aggressive normalisation for comparisons: lowercase ascii alnum only."""
    if not text:
        return ""
    t = unicodedata.normalize("NFKD", text)
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    t = t.lower()
    t = t.replace("&", "and")
    return re.sub(r"[^a-z0-9]+", "", t)


def norm_words(text: str | None) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKD", text)
    t = "".join(ch for ch in t if not unicodedata.combining(ch)).lower()
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()

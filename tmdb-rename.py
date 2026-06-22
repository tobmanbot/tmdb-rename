#!/usr/bin/env python3
"""
tmdb-rename.py — Rename scene release filenames to Kodi/TMDB-compatible format.

Ziel-Format: Movie.Title.(Year).mkv
Kodi erkennt Filme via TMDB mit Original-Titel + Jahr unabhängig von der
Spracheinstellung (Deutsch/Englisch).

Verwendung:
  python3 tmdb-rename.py /quelle                                           # Dry-Run
  python3 tmdb-rename.py /quelle --execute                                 # in-place umbenennen
  python3 tmdb-rename.py /quelle --execute /ziel                           # nach /ziel verschieben
  python3 tmdb-rename.py /quelle --keep-original-title en,de               # Original-Titel für EN/DE-Filme erzwingen
  python3 tmdb-rename.py /quelle --keep-original-title latin               # Original-Titel für alle lateinischsprachigen Filme
  python3 tmdb-rename.py /quelle --keep-original-title latin,ja            # Latin + Japanisch
  python3 tmdb-rename.py /quelle --force-ffprobe                           # MKV-Titel als primäre Suchquelle
  python3 tmdb-rename.py /quelle --locale de-DE                            # TMDB-Suchergebnisse auf Deutsch
  python3 tmdb-rename.py /quelle --format "{title_de}{sep}{title_en}{sep}({year})"  # Zweisprachig
  python3 tmdb-rename.py /quelle --undo backup.json                        # Dry-Run Undo
  python3 tmdb-rename.py /quelle --undo backup.json --execute              # Undo ausführen

Format-Platzhalter (--format):
  {title}          Primärtitel (aus choose_title / --keep-original-title)
  {year}           Erscheinungsjahr
  {sep}            Konfigurierter Separator (--sep), Standard "."
  {lang}           original_language-Code (z.B. "de", "en", "fr")
  {original_title} Unberührter original_title aus TMDB
  {title_XX}       Lokalisierter Titel (XX = ISO-639-1-Code); Locales werden
                   automatisch aus dem Template erkannt — kein --lang nötig
  sep gilt nur *innerhalb* von Titelwerten; Template-Struktur bleibt literal.

TMDB API-Key: https://www.themoviedb.org/settings/api (kostenlos)
Alternativ als Umgebungsvariable: export TMDB_API_KEY=xxx
Cache wird automatisch in <verzeichnis>/.tmdb-rename-cache.json gespeichert.
"""

import os
import re
import sys
import time
import json
import shutil
import argparse
import datetime
import subprocess
from urllib.request import urlopen, Request
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError

# ─── Konfiguration ────────────────────────────────────────────────────────────

TMDB_BASE    = "https://api.themoviedb.org/3"
VIDEO_EXTS   = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".wmv"}
SIDECAR_EXTS = {".srt", ".nfo", ".sub", ".idx", ".ass", ".ssa"}

SCENE_GARBAGE = re.compile(
    r"\b("
    r"german|english|french|spanish|italian|dutch|swedish|danish|norwegian|"
    r"dl|dual|dubbed|fs|md|ld|hd|uhd|fhd|"
    r"web|web[-.]dl|webrip|bluray|blu[-.]ray|bdrip|bdremux|dvdrip|hdtv|"
    r"h\.?264|h\.?265|x264|x265|hevc|avc|xvid|divx|"
    r"1080p|720p|480p|2160p|4k|uhd|hdr|hdr10|dv|dolby|"
    r"aac|ac3|eac3|dts|truehd|atmos|flac|mp3|"
    r"remux|remastered|extended|unrated|theatrical|proper|"
    r"internal|limited|retail|doku|"
    r"amzn|nf|dsnp|hmax|atvp|pcok|pmtp|magenta|"
    r"imax|3d|sbs|hsbs|wvf|mge|wayne|nomad|details|lizardsquad"
    r")\b",
    re.IGNORECASE,
)

TECH_ANCHORS = re.compile(
    r"\b("
    r"german|english|french|dubbed|"
    r"1080p|720p|2160p|4k|hdr|"
    r"bluray|blu[-.]ray|web[-.]?dl|webrip|bdrip|hdtv|"
    r"h\.?264|h\.?265|x264|x265|hevc|xvid|"
    r"ac3|eac3|dts|truehd|aac"
    r")\b",
    re.IGNORECASE,
)


# ─── Cache ────────────────────────────────────────────────────────────────────

def cache_load(path: str) -> dict:
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def cache_save(path: str, cache: dict) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"  ⚠ Cache konnte nicht gespeichert werden: {e}")


# ─── TMDB ─────────────────────────────────────────────────────────────────────

def tmdb_get(path: str, params: dict, api_key: str) -> dict:
    params["api_key"] = api_key
    url = f"{TMDB_BASE}{path}?{urlencode(params)}"
    req = Request(url, headers={"User-Agent": "tmdb-rename/1.0"})
    try:
        with urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.reason}") from e
    except URLError as e:
        raise RuntimeError(f"Netzwerkfehler: {e.reason}") from e


def tmdb_search_raw(
    query: str,
    year: str | None,
    api_key: str,
    limit: int = 5,
    locale: str | None = None,
) -> list[dict]:
    """Gibt bis zu `limit` Rohergebnisse zurück (für interaktive Auswahl)."""
    params: dict = {"query": query.strip(), "include_adult": "false"}
    if year:
        params["year"] = year
    if locale:
        params["language"] = locale
    data = tmdb_get("/search/movie", params, api_key)
    return data.get("results", [])[:limit]


def umlaut_variants(query: str) -> list[str]:
    """
    Erzeugt Varianten mit substituierten Umlauten als Fallback.
    Scene-Releases ersetzen häufig: ü→ue, ä→ae, ö→oe, ß→ss.
    Wir versuchen beide Richtungen.
    """
    variants = []

    # Richtung 1: ASCII-Digraphen → Umlaute (z.B. "mueller" → "müller")
    v = query
    for src, dst in [
        ("ue", "ü"), ("UE", "Ü"), ("Ue", "Ü"),
        ("ae", "ä"), ("AE", "Ä"), ("Ae", "Ä"),
        ("oe", "ö"), ("OE", "Ö"), ("Oe", "Ö"),
    ]:
        v = v.replace(src, dst)
    if v != query:
        variants.append(v)
    # Zusätzlich ss → ß (separat, da riskanter)
    v2 = v.replace("ss", "ß")
    if v2 != v:
        variants.append(v2)

    # Richtung 2: Umlaute → ASCII-Digraphen
    v3 = query
    for src, dst in [
        ("ü", "ue"), ("Ü", "Ue"),
        ("ä", "ae"), ("Ä", "Ae"),
        ("ö", "oe"), ("Ö", "Oe"),
        ("ß", "ss"),
    ]:
        v3 = v3.replace(src, dst)
    if v3 != query:
        variants.append(v3)

    return variants


def tmdb_search(
    query: str,
    year: str | None,
    api_key: str,
    locale: str | None = None,
) -> tuple[dict | None, str]:
    """
    Automatische Suche mit Fallback-Strategien.
    Gibt (besten Treffer | None, verwendete Query) zurück.
    """
    def _first(q: str, y: str | None) -> dict | None:
        q = q.strip()
        if not q:
            return None
        results = tmdb_search_raw(q, y, api_key, limit=1, locale=locale)
        return results[0] if results else None

    parts = re.split(r" - | – | \| ", query)
    first = parts[0].strip()
    last  = parts[-1].strip() if len(parts) > 1 else ""

    # Basis-Strategien
    candidates: list[tuple[str, str | None]] = [
        (query, year),
        (query, None),
        (first, year),
        (first, None),
        (last,  year),
        (last,  None),
    ]

    # Umlaut-Varianten für query und first als weitere Fallbacks
    for base, y in [(query, year), (query, None), (first, year), (first, None)]:
        for variant in umlaut_variants(base):
            candidates.append((variant, y))

    seen: set[tuple[str, str | None]] = set()
    for q, y in candidates:
        if not q:
            continue
        key = (q.lower(), y)
        if key in seen:
            continue
        seen.add(key)
        result = _first(q, y)
        if result:
            return result, q

    return None, query


def is_latin_script(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return True
    latin = sum(1 for c in letters if ord(c) < 0x0370)
    return (latin / len(letters)) >= 0.8


def get_localized_title(tmdb_id: int, locale: str, api_key: str) -> str:
    """Holt den Filmtitel in der angegebenen Sprache (z.B. 'de-DE', 'en-US')."""
    try:
        data = tmdb_get(f"/movie/{tmdb_id}", {"language": locale}, api_key)
        return data.get("title", "")
    except RuntimeError:
        return ""


def get_english_title(tmdb_id: int, api_key: str) -> str:
    """Rückwärtskompatibilität — ruft get_localized_title mit en-US auf."""
    return get_localized_title(tmdb_id, "en-US", api_key)


def get_additional_titles(
    tmdb_id: int,
    locales: set[str],
    api_key: str,
    delay: float = 0.15,
) -> dict[str, str]:
    """
    Holt Filmtitel für mehrere Locales (für --lang).
    Gibt dict {code: title} zurück, z.B. {"de": "Das Boot", "en": "The Boat"}.
    TMDB akzeptiert sowohl '"de"' als auch '"de-DE"' als language-Parameter.
    """
    titles: dict[str, str] = {}
    for code in sorted(locales):  # sorted für deterministischen API-Aufruf
        title = get_localized_title(tmdb_id, code, api_key)
        if title:
            titles[code] = title
        time.sleep(delay)
    return titles


def choose_title(
    result: dict,
    api_key: str,
    keep_original_langs: set[str] | None = None,
    locale: str | None = None,
) -> str:
    """
    Wählt den besten Primärtitel für den Dateinamen.

    keep_original_langs (z.B. {"en", "de"} oder {"latin"}):
      - "latin" in der Menge → original_title erzwungen für alle Sprachen mit
        lateinischem Schriftsystem (Script-Check statt Sprachcode-Vergleich)
      - original_language in der Menge → original_title erzwungen
        (Fallback auf locale/Englisch wenn nicht-lateinisch)
      - original_language NICHT in der Menge → TMDB-Titel aus Suchergebnis
        (beeinflusst durch --locale)
    locale: TMDB-Sprache für Fallback-Abruf (Standard: en-US)
    Ohne keep_original_langs: bisheriges Verhalten
      (Latin-Script → original_title, sonst locale-/englischer Titel).
    """
    original        = result.get("original_title", "")
    orig_lang       = result.get("original_language", "")
    fallback_locale = locale or "en-US"

    if keep_original_langs is not None:
        # "latin"-Keyword: Original-Titel für alle Sprachen mit lateinischem Schrift
        in_set = orig_lang in keep_original_langs
        if not in_set and "latin" in keep_original_langs:
            in_set = is_latin_script(original)

        if in_set:
            # Original-Sprache gewünscht: original_title bevorzugen
            if is_latin_script(original):
                return original
            # Nicht-lateinisches Original → Fallback auf locale
            loc_title = get_localized_title(result["id"], fallback_locale, api_key)
            return loc_title or result.get("title", original)
        else:
            # Andere Sprache: TMDB-Titel aus Suchergebnis (durch --locale beeinflusst)
            return result.get("title", "") or original

    # Standardverhalten (kein Flag)
    if is_latin_script(original):
        return original
    loc_title = get_localized_title(result["id"], fallback_locale, api_key)
    return loc_title or result.get("title", original)


# ─── Titel-Vergleich ─────────────────────────────────────────────────────────

_SUPERSCRIPT_TRANS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")

# Zahlwörter (EN/DE/FR/ES/IT) → Ziffern für sprachübergreifenden Titelvergleich
_NUM_WORDS: dict[str, str] = {
    "one": "1",   "eins": "1",  "une": "1",   "uno": "1",   "una": "1",
    "two": "2",   "zwei": "2",  "deux": "2",  "dos": "2",   "due": "2",
    "three": "3", "drei": "3",  "trois": "3", "tres": "3",  "tre": "3",
    "four": "4",  "vier": "4",  "quatre": "4","cuatro": "4","quattro": "4",
    "five": "5",  "funf": "5",  "cinq": "5",  "cinco": "5", "cinque": "5",
    "six": "6",   "sechs": "6", "six": "6",   "seis": "6",  "sei": "6",
    "seven": "7", "sieben": "7","sept": "7",  "siete": "7", "sette": "7",
    "eight": "8", "acht": "8",  "huit": "8",  "ocho": "8",  "otto": "8",
    "nine": "9",  "neun": "9",  "neuf": "9",  "nueve": "9", "nove": "9",
    "ten": "10",  "zehn": "10", "dix": "10",  "diez": "10", "dieci": "10",
}

# "Teil", "Partie" usw. → "part" für sprachübergreifenden Vergleich
_PART_SYNONYMS: frozenset[str] = frozenset({
    "teil", "partie", "parte", "kapitel", "chapter",
})


def _norm_title_cmp(s: str) -> str:
    """Normalisiert Titel für unscharfen Vergleich.
    Superscript→ASCII-Ziffern, Separatoren+Satzzeichen→Leerzeichen,
    Zahlwörter→Ziffern, Part-Synonyme→'part', Kleinschreibung."""
    s = s.translate(_SUPERSCRIPT_TRANS)
    s = re.sub(r"[.\-–—_:,!?'\"]+", " ", s)
    s = re.sub(r"\s+", " ", s).lower().strip()
    words = [_NUM_WORDS.get(w, w) for w in s.split()]
    words = ["part" if w in _PART_SYNONYMS else w for w in words]
    return " ".join(words)


def titles_similar(a: str, b: str) -> bool:
    """
    True wenn zwei Titel inhaltlich gleich gelten:
    - Exakt (nach Normalisierung)
    - Ohne Whitespace identisch (z.B. "Accountant²" ≡ "Accountant 2")
    - Präfix-Beziehung: einer beginnt mit dem anderen, gefolgt von Leerzeichen
      oder Trenner (z.B. "Together" ⊂ "Together - Unzertrennlich",
      "The Toxic Avenger" ⊂ "The Toxic Avenger Unrated")
    """
    an, bn = _norm_title_cmp(a), _norm_title_cmp(b)
    if an == bn:
        return True
    # Ohne Whitespace (für Superscript-/Zahlenvarianten)
    if re.sub(r"\s+", "", an) == re.sub(r"\s+", "", bn):
        return True
    # Präfix-Check
    for shorter, longer in ((an, bn), (bn, an)):
        if longer.startswith(shorter):
            rest = longer[len(shorter):]
            if not rest or rest[0] in " \t-–:":
                return True
    return False


# ─── Dateiname-Parsing ────────────────────────────────────────────────────────

# ─── ffprobe ──────────────────────────────────────────────────────────────────

_FFPROBE_PATH: str | None = shutil.which("ffprobe")  # None wenn nicht installiert


def get_mkv_title(filepath: str) -> str | None:
    """
    Liest den TITLE-Tag aus dem MKV-Container via ffprobe.
    Gibt None zurück wenn: ffprobe nicht installiert, kein Tag, Fehler.
    """
    if not _FFPROBE_PATH:
        return None
    try:
        out = subprocess.check_output(
            [
                _FFPROBE_PATH, "-v", "quiet",
                "-print_format", "json",
                "-show_format",
                filepath,
            ],
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        data = json.loads(out)
        title = data.get("format", {}).get("tags", {}).get("title", "")
        # Auch Großschreibung prüfen
        if not title:
            tags = {k.lower(): v for k, v in data.get("format", {}).get("tags", {}).items()}
            title = tags.get("title", "")
        return title.strip() or None
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            json.JSONDecodeError, OSError):
        return None


def parse_scene_filename(filename: str) -> tuple[str, str | None]:
    """
    Marker-Strategie:
      1. Release-Group-Suffix ("- GroupName" am Ende) → entfernen
      2. Jahr (1900–2099) → primärer Schnitt-Anker
      3. Tech-Anker ("german", "1080p" …) → Fallback ohne Jahr
      4. Residual-Garbage bereinigen
    """
    stem = os.path.splitext(filename)[0]
    normalized = stem.replace(".", " ").replace("_", " ")

    # Marker 1: Release-Group
    normalized = re.sub(r"\s+-\s*[A-Za-z0-9]{2,20}$", "", normalized).strip()

    # Marker 2: Jahr
    year_match = re.search(r"\b(19\d{2}|20\d{2})\b", normalized)
    year = year_match.group(1) if year_match else None

    if year_match:
        raw_title = normalized[: year_match.start()]
    else:
        # Marker 3: Tech-Anker
        tech_match = TECH_ANCHORS.search(normalized)
        raw_title = normalized[: tech_match.start()] if tech_match else normalized

    # Marker 4: Bereinigung
    raw_title = SCENE_GARBAGE.sub(" ", raw_title)
    raw_title = re.sub(r"[({\[\-–—\s]+$", "", raw_title).strip()
    raw_title = re.sub(r"^[-–—\s]+", "", raw_title).strip()
    raw_title = re.sub(r"\s{2,}", " ", raw_title)

    return raw_title, year


def sanitize_for_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00]', "", name)
    name = re.sub(r"\s{2,}", " ", name).strip()
    return name


DEFAULT_FORMAT = "{title}{sep}({year})[{sep}-{sep}{title_de}]"


def apply_format(
    fmt: str,
    title: str,
    year: str,
    sep: str,
    ext: str,
    extra_titles: dict[str, str] | None = None,
    lang: str = "",
    original_title: str = "",
) -> tuple[str, str]:
    """
    Wendet das Format-Template auf die Titelbestandteile an.
    Gibt (new_stem, new_name) zurück.

    sep gilt nur *innerhalb* von Titelwerten ({title}, {title_XX}, {original_title}).
    Template-Struktur (Leerzeichen/Zeichen zwischen Platzhaltern) bleibt literal.
    {sep} im Template wird durch den konfigurierten Separator ersetzt.

    Platzhalter:
      {title}          Primärtitel (aus choose_title)
      {year}           Erscheinungsjahr
      {sep}            Konfigurierter Separator (--sep)
      {lang}           original_language-Code (z.B. "de", "en")
      {original_title} Unberührter original_title aus TMDB
      {title_XX}       Lokalisierter Titel für Locale XX (auto-erkannt)

    Optional-Blöcke:
      Teile des Templates in [...] werden weggelassen, wenn alle enthaltenen
      {title_XX}-Werte dem Primärtitel entsprechen oder leer sind.
      Beispiel: "{title}{sep}({year})[-{title_de}]"
        de == primär  → "The.Movie.(2024)"
        de != primär  → "The.Movie.(2024)-Das.Film"
    """
    def title_val(s: str) -> str:
        """Bereinigt Titelwert und ersetzt interne Leerzeichen durch sep."""
        s = sanitize_for_filename(s)
        if sep:
            s = s.replace(" ", sep)
            s = re.sub(re.escape(sep) + r"+", sep, s)
            s = s.strip(sep)
        else:
            s = s.strip()
        return s

    primary = title_val(title)

    # Optional-Blöcke [...] auswerten:
    # Enthält ein Block nur {title_XX}, die dem Primärtitel entsprechen oder leer sind,
    # wird der gesamte Block weggelassen. Andernfalls wird der Block-Inhalt übernommen.
    def resolve_optional_block(m: re.Match) -> str:
        content = m.group(1)
        codes = re.findall(r"\{title_([a-zA-Z_-]+)\}", content)
        if not codes:
            return content  # kein lokalisierter Platzhalter → immer einschließen
        for code in codes:
            loc_raw = (extra_titles or {}).get(code, "")
            if loc_raw and not titles_similar(title, loc_raw):
                # Mindestens einer weicht inhaltlich ab → Block einschließen und rendern
                rendered = content
                rendered = rendered.replace("{sep}", sep)
                rendered = rendered.replace("{title}", primary)
                rendered = rendered.replace("{year}", year)
                rendered = rendered.replace("{lang}", lang)
                rendered = rendered.replace("{original_title}", title_val(original_title))
                if extra_titles:
                    for c, t in extra_titles.items():
                        rendered = rendered.replace(f"{{title_{c}}}", title_val(t))
                rendered = re.sub(r"\{title_[a-zA-Z_-]+\}", primary, rendered)
                return rendered
        # Alle gleich oder leer → Block weglassen
        return ""

    result = re.sub(r"\[([^\[\]]*)\]", resolve_optional_block, fmt)

    # {sep} zuerst ersetzen (Struktur-Platzhalter, kein Titelwert)
    result = result.replace("{sep}", sep)
    # Titelwerte einsetzen (sep gilt nur innerhalb)
    result = result.replace("{title}",          primary)
    result = result.replace("{year}",           year)
    result = result.replace("{lang}",           lang)
    result = result.replace("{original_title}", title_val(original_title))

    if extra_titles:
        for code, loc_title in extra_titles.items():
            result = result.replace(f"{{title_{code}}}", title_val(loc_title))

    # Unbekannte {title_XX}-Platzhalter → Primärtitel als Fallback
    result = re.sub(r"\{title_[a-zA-Z_-]+\}", primary, result)

    # Illegale Dateiname-Zeichen aus Template-Struktur entfernen
    result = re.sub(r'[<>:"/\\|?*\x00]', "", result)
    # Mehrfache Leerzeichen normalisieren
    result = re.sub(r" {2,}", " ", result).strip()

    return result, f"{result}{ext}"


def build_new_name(
    chosen: str,
    year: str,
    sep: str,
    ext: str,
    fmt: str = DEFAULT_FORMAT,
    extra_titles: dict[str, str] | None = None,
    lang: str = "",
    original_title: str = "",
) -> tuple[str, str]:
    """Baut den neuen Dateinamen via Format-Template. Gibt (new_stem, new_name) zurück."""
    return apply_format(fmt, chosen, year, sep, ext, extra_titles, lang, original_title)


def unique_dest(dest_dir: str, new_stem: str, ext: str) -> tuple[str, str]:
    """
    Gibt (stem, name) zurück. Falls <dest_dir>/<new_stem><ext> bereits existiert,
    wird .(2), .(3) … angehängt bis ein freier Name gefunden wird.
    """
    candidate_stem = new_stem
    candidate_name = f"{new_stem}{ext}"
    n = 2
    while os.path.exists(os.path.join(dest_dir, candidate_name)):
        candidate_stem = f"{new_stem}.({n})"
        candidate_name = f"{candidate_stem}{ext}"
        n += 1
    return candidate_stem, candidate_name


def write_nfo(dest_dir: str, new_stem: str, title: str, year: str, tmdb_id: int) -> None:
    """Schreibt eine Kodi-NFO-Datei mit TMDB-ID neben die Videodatei."""
    nfo_path = os.path.join(dest_dir, f"{new_stem}.nfo")
    content = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>\n'
        '<movie>\n'
        f'  <title>{title}</title>\n'
        f'  <year>{year}</year>\n'
        f'  <uniqueid type="tmdb" default="true">{tmdb_id}</uniqueid>\n'
        '</movie>\n'
    )
    with open(nfo_path, "w", encoding="utf-8") as f:
        f.write(content)


def find_sidecar_files(directory: str, stem: str) -> list[str]:
    sidecars = []
    for f in os.listdir(directory):
        fext  = os.path.splitext(f)[1].lower()
        fstem = os.path.splitext(f)[0]
        if fext in SIDECAR_EXTS and fstem.startswith(stem):
            sidecars.append(f)
    return sidecars


# ─── Live-Modus ───────────────────────────────────────────────────────────────

def live_mode(
    failed: list[tuple[str, str, str]],
    dest_dir: str,
    move_mode: bool,
    root_dir: str,
    skip_source_dirs: bool,
    sep: str,
    api_key: str,
    cache: dict,
    execute: bool,
    nfo: bool = False,
    keep_original_langs: set[str] | None = None,
    fmt: str = DEFAULT_FORMAT,
    lang_locales: set[str] | None = None,
    locale: str | None = None,
) -> list[dict]:
    """
    Interaktiver Modus für nicht erkannte Dateien.
    Gibt Liste der dabei erfolgreich aufgelösten Einträge zurück.
    """
    if not failed:
        return []

    resolved: list[dict] = []

    def _skip_as_correct(fn: str, file_dir: str, file_dst_dir: str) -> None:
        """
        Überspringen als 'korrekt benannt': Datei aus 'failed' herausnehmen.
        Im Move-Mode wird sie zusätzlich unverändert nach dest_dir verschoben.
        """
        fn_stem = os.path.splitext(fn)[0]
        sc_list = [f for f in find_sidecar_files(file_dir, fn_stem) if f != fn]
        sc_log  = [{"old": sc, "new": sc} for sc in sc_list]
        if move_mode and execute:
            try:
                os.makedirs(file_dst_dir, exist_ok=True)
                os.rename(os.path.join(file_dir, fn), os.path.join(file_dst_dir, fn))
                for sc in sc_list:
                    os.rename(os.path.join(file_dir, sc), os.path.join(file_dst_dir, sc))
                resolved.append({"old": fn, "new": fn, "sidecars": sc_log})
                print("  ✓ verschoben (unverändert).")
            except OSError as e:
                print(f"  ✗ Fehler: {e}")
        elif move_mode:
            resolved.append({"old": fn, "new": fn, "sidecars": sc_log})
            print(f"  → {fn}  (Dry-Run, würde verschieben ohne Umbenennung)")
        else:
            resolved.append({"old": fn, "new": fn, "sidecars": sc_log})
            print("  ✓ übersprungen (als korrekt benannt markiert).")

    print(f"\n{'═' * 80}")
    print(f"LIVE-MODUS — {len(failed)} nicht erkannte Datei(en)")
    print("Befehle: <Suchbegriff> [+JAHR]  |  s = überspringen (korrekt)  |  i = ignorieren  |  q = beenden")
    print(f"{'═' * 80}\n")

    for subdir_lm, filename, reason in failed:
        stem, ext = os.path.splitext(filename)
        ext = ext.lower()
        if move_mode:
            if skip_source_dirs:
                file_dest_dir_lm = dest_dir
            else:
                rel = os.path.relpath(subdir_lm, root_dir)
                file_dest_dir_lm = os.path.normpath(os.path.join(dest_dir, rel))
        else:
            file_dest_dir_lm = subdir_lm
        parsed_title, parsed_year = parse_scene_filename(filename)

        print(f"Datei:   {filename}")
        print(f"Geparst: '{parsed_title}'  Jahr: {parsed_year or '—'}")
        print(f"Grund:   {reason}")

        while True:
            try:
                raw = input(f"\nSuche [{parsed_title}]: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n→ Live-Modus abgebrochen.")
                return resolved

            if raw.lower() == "q":
                print("→ Live-Modus beendet.")
                return resolved
            if raw.lower() == "s":
                _skip_as_correct(filename, subdir_lm, file_dest_dir_lm)
                break
            if raw.lower() == "i":
                print("  ignoriert.")
                break
            if raw == "":
                # Leere Eingabe → Standard-Query nochmal versuchen
                raw = parsed_title

            # Jahr aus Query extrahieren falls angegeben: "titel +2024"
            year_override = parsed_year
            year_in_query = re.search(r"\+(\d{4})$", raw)
            if year_in_query:
                year_override = year_in_query.group(1)
                raw = raw[: year_in_query.start()].strip()

            if not api_key:
                print("  ⚠ Kein API-Key — Suche nicht möglich. [s]=korrekt  [i]=ignorieren  [q]=beenden")
                continue

            try:
                results = tmdb_search_raw(raw, year_override, api_key, limit=7, locale=locale)
            except RuntimeError as e:
                print(f"  API-Fehler: {e}")
                continue

            if not results:
                print("  Keine Treffer.")
                continue

            # Ergebnisse anzeigen
            print()
            for i, r in enumerate(results, 1):
                rd   = r.get("release_date", "")[:4] or "????"
                orig = r.get("original_title", "")
                loc  = r.get("title", "")
                extra = f" / {loc}" if loc != orig else ""
                print(f"  [{i}] {orig}{extra} ({rd})")
            print("  [0] Erneut suchen")
            print("  [s] Überspringen (korrekt benannt)")
            print("  [i] Ignorieren (unbekannt)")

            try:
                choice = input("Auswahl: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n→ Live-Modus abgebrochen.")
                return resolved

            if choice == "0" or choice == "":
                continue
            if choice.lower() == "s":
                _skip_as_correct(filename, subdir_lm, file_dest_dir_lm)
                break
            if choice.lower() == "i":
                print("  ignoriert.")
                break
            if not choice.isdigit() or not (1 <= int(choice) <= len(results)):
                print("  Ungültige Auswahl.")
                continue

            result = results[int(choice) - 1]
            chosen        = choose_title(result, api_key, keep_original_langs, locale=locale)
            release_date  = result.get("release_date", "")
            tmdb_year     = release_date[:4] if release_date else year_override or "????"
            orig_lang_lm  = result.get("original_language", "")
            orig_title_lm = result.get("original_title", "")

            extra_titles_lm: dict[str, str] = {}
            if lang_locales and api_key:
                extra_titles_lm = get_additional_titles(result["id"], lang_locales, api_key)

            new_stem, new_name = build_new_name(
                chosen, tmdb_year, sep, ext,
                fmt=fmt, extra_titles=extra_titles_lm,
                lang=orig_lang_lm, original_title=orig_title_lm,
            )

            # Cache aktualisieren
            cache[filename] = {
                "id":                result["id"],
                "original_title":    orig_title_lm,
                "original_language": orig_lang_lm,
                "title":             result.get("title", ""),
                "release_date":      release_date,
                "chosen_title":      chosen,
                "localized_titles":  extra_titles_lm,
            }

            print(f"  → {new_name}")

            # Bereits korrekt benannt? (Ziel == Quelle, kein effektiver Move)
            if new_name == filename and os.path.realpath(file_dest_dir_lm) == os.path.realpath(subdir_lm):
                print("  ✓ bereits korrekt benannt — keine Umbenennung nötig.")
                resolved.append({"old": filename, "new": filename, "sidecars": []})
                break

            sidecars = [f for f in find_sidecar_files(subdir_lm, stem) if f != filename]

            final_stem, final_name = unique_dest(file_dest_dir_lm, new_stem, ext)
            if final_name != new_name:
                print(f"  ⚠ Duplikat → {final_name}")

            sc_log = [
                {"old": sc, "new": f"{final_stem}{sc[len(stem):]}"}  
                for sc in sidecars
            ]

            if execute:
                try:
                    src = os.path.join(subdir_lm, filename)
                    dst = os.path.join(file_dest_dir_lm, final_name)
                    os.makedirs(file_dest_dir_lm, exist_ok=True)
                    os.rename(src, dst)
                    for sc in sidecars:
                        sc_suffix = sc[len(stem):]
                        sc_new    = f"{final_stem}{sc_suffix}"
                        os.rename(
                            os.path.join(subdir_lm, sc),
                            os.path.join(file_dest_dir_lm, sc_new),
                        )
                    if nfo:
                        write_nfo(file_dest_dir_lm, final_stem, chosen, tmdb_year, result["id"])
                    resolved.append({"old": filename, "new": final_name, "old_dir": subdir_lm, "new_dir": file_dest_dir_lm, "sidecars": sc_log})
                    print("  ✓ verschoben.")
                except OSError as e:
                    print(f"  ✗ Fehler: {e}")
            else:
                resolved.append({"old": filename, "new": final_name, "old_dir": subdir_lm, "new_dir": file_dest_dir_lm, "sidecars": sc_log})
                print("  (Dry-Run, nicht verschoben)")
            break

        print()

    return resolved


# ─── Undo ─────────────────────────────────────────────────────────────────────

def undo_renames(backup_path: str, execute: bool) -> None:
    if not os.path.isfile(backup_path):
        print(f"FEHLER: Backup-Datei nicht gefunden: {backup_path}")
        sys.exit(1)

    with open(backup_path, encoding="utf-8") as f:
        data = json.load(f)

    src_dir   = data.get("source_directory") or data.get("directory", "")
    dest_dir  = data.get("dest_directory", src_dir)
    move_mode = data.get("move_mode", False)
    renames   = data["renames"]

    print(f"Backup vom:  {data['created']}")
    print(f"Quelle:      {src_dir}")
    if move_mode:
        print(f"Verschoben nach: {dest_dir}")
    print(f"Einträge:    {len(renames)}")
    print(f"Modus:       {'★ EXECUTE' if execute else 'DRY-RUN'}")
    print("─" * 80)

    ok = err = 0
    for entry in renames:
        old, new = entry["old"], entry["new"]
        sidecars     = entry.get("sidecars", [])
        entry_old_dir = entry.get("old_dir", src_dir)
        entry_new_dir = entry.get("new_dir", dest_dir)
        current = os.path.join(entry_new_dir, new)
        restore = os.path.join(entry_old_dir, old)
        if not os.path.exists(current):
            print(f"  ⚠ nicht vorhanden (schon zurück?): {new}")
            err += 1
            continue
        arrow = f"{entry_new_dir}/{new}" if move_mode else new
        print(f"  {arrow}")
        print(f"    → {entry_old_dir}/{old}")
        for sc in sidecars:
            print(f"    → sidecar: {sc['new']} → {sc['old']}")
        if execute:
            try:
                os.rename(current, restore)
                for sc in sidecars:
                    sc_cur = os.path.join(entry_new_dir, sc["new"])
                    sc_res = os.path.join(entry_old_dir, sc["old"])
                    if os.path.exists(sc_cur):
                        os.rename(sc_cur, sc_res)
                    else:
                        print(f"    ⚠ Sidecar nicht vorhanden: {sc['new']}")
                ok += 1
            except OSError as e:
                print(f"    ✗ Fehler: {e}")
                err += 1
        else:
            ok += 1

    print("═" * 80)
    verb = "Wiederhergestellt" if execute else "Würde wiederherstellen"
    print(f"{verb}: {ok}  |  Fehler: {err}")
    if not execute:
        print("→ Mit --execute wirklich zurücksetzen.")


# ─── Rekursive Dateiliste ────────────────────────────────────────────────────

def collect_video_files(root_dir: str, max_depth: int | None) -> list[tuple[str, str]]:
    """
    Sammelt Videodateien rekursiv in root_dir und Unterverzeichnissen.
    max_depth=None → alle Ebenen; max_depth=N → max N Ebenen tief
    (1 = nur direkte Unterverzeichnisse).
    Gibt sortierte Liste von (subdir_abs, filename) zurück.
    """
    result: list[tuple[str, str]] = []
    root_depth = root_dir.rstrip(os.sep).count(os.sep)
    for dirpath, dirnames, filenames in os.walk(root_dir):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        current_depth = dirpath.rstrip(os.sep).count(os.sep) - root_depth
        if max_depth is not None and current_depth >= max_depth:
            dirnames.clear()
        for f in sorted(filenames):
            if os.path.splitext(f)[1].lower() in VIDEO_EXTS and not f.startswith("._"):
                result.append((dirpath, f))
    return result


# ─── Hauptprogramm ────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rename Scene-Release-Dateien auf Kodi/TMDB-kompatibles Format."
    )
    parser.add_argument("directory", help="Quellverzeichnis mit den Filmdateien")
    parser.add_argument(
        "--execute",
        nargs="?",
        const="",
        metavar="DESTDIR",
        help="Umbenennung durchführen. Ohne Argument: in-place. "
             "Mit Verzeichnis: dorthin verschieben.",
    )
    parser.add_argument(
        "--api-key",
        help="TMDB API-Key (alternativ: Umgebungsvariable TMDB_API_KEY)",
    )
    parser.add_argument(
        "--cache",
        metavar="DATEI",
        help="Pfad zur Cache-Datei (Standard: <verzeichnis>/.tmdb-rename-cache.json)",
    )
    parser.add_argument(
        "--sep",
        default=".",
        metavar="ZEICHEN",
        help="Trennzeichen zwischen Titelwörtern (Standard: '.', Alternative: ' ')",
    )
    parser.add_argument(
        "--nfo",
        action="store_true",
        help="NFO-Datei mit TMDB-ID neben jede Videodatei schreiben (löst Doppelgänger-Problem)",
    )
    parser.add_argument(
        "--keep-original-title",
        metavar="LANGS",
        default="latin",
        help="Komma-separierte ISO-639-1-Codes (z.B. 'en,de') und/oder das Keyword 'latin'. "
             "'latin' → Original-Titel für alle Sprachen mit lateinischem Schriftsystem "
             "(deckt z.B. es, fr, it, pt, pl, nl, … auf einmal ab). "
             "ISO-Codes → Original-Titel nur für diese Sprachen. "
             "Kombinierbar, z.B. 'latin,ja'. "
             "Alle anderen Sprachen erhalten den TMDB-Standardtitel (Englisch). "
             "Ohne Flag: bisheriges Verhalten (Latin-Script → Originaltitel, sonst Englisch).",
    )
    parser.add_argument(
        "--locale",
        metavar="LOCALE",
        help="TMDB-Sprache für Suchanfragen und Titel-Fallback, z.B. 'de-DE' oder 'de'. "
             "Beeinflusst den 'title'-Wert in TMDB-Suchergebnissen (Standard: en-US). "
             "Nützlich zusammen mit --keep-original-title für nicht-gelistete Sprachen.",
    )
    parser.add_argument(
        "--format",
        metavar="TEMPLATE",
        default=DEFAULT_FORMAT,
        help=f"Dateiname-Template mit Platzhaltern (Standard: '{DEFAULT_FORMAT}'). "
             "Verfügbar: {{title}}, {{year}}, {{sep}}, {{lang}}, {{original_title}}, {{title_XX}}. "
             "sep gilt nur *innerhalb* von Titelwerten, Template-Struktur bleibt literal. "
             "Locales werden automatisch aus {{title_XX}}-Platzhaltern erkannt. "
             "Beispiel: '{{title_de}}{{sep}}{{title_en}}{{sep}}({{year}})'",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.3,
        help="Pause zwischen API-Anfragen in Sekunden (Standard: 0.3)",
    )
    parser.add_argument(
        "--force-ffprobe",
        action="store_true",
        help="MKV-Metadaten-Titel (TITLE-Tag) als primäre Suchquelle verwenden statt "
             "als Fallback. ffprobe muss installiert sein. Ohne Flag: ffprobe wird nur "
             "genutzt, wenn die normale Suche keinen Treffer liefert.",
    )
    parser.add_argument(
        "--skip-found",
        action="store_true",
        help="Dateien überspringen, die schon im Format 'Titel(Jahr).ext' sind",
    )
    parser.add_argument(
        "--undo",
        metavar="BACKUP_JSON",
        help="Umbenennung rückgängig machen anhand einer Backup-JSON-Datei",
    )
    parser.add_argument(
        "--skip-source-dirs",
        action="store_true",
        help="Bei --execute /ziel: alle Dateien flach ins Zielverzeichnis verschieben (altes Verhalten). "
             "Ohne diesen Flag wird die Unterverzeichnis-Struktur im Zielverzeichnis gespiegelt.",
    )
    parser.add_argument(
        "--recursive", "-r",
        nargs="?",
        default=False,
        const=None,
        type=int,
        metavar="N",
        help="Unterverzeichnisse einbeziehen. Ohne N: alle Ebenen. "
             "Mit N: max N Ebenen tief (z.B. -r1 = nur direkte Unterverzeichnisse, "
             "-r2 = zwei Ebenen tief).",
    )
    args = parser.parse_args()

    # ── --force-ffprobe prüfen ─────────────────────────────────────────────
    if args.force_ffprobe and not _FFPROBE_PATH:
        print("⚠ --force-ffprobe: ffprobe nicht gefunden — Suche läuft normal über Dateinamen.")
        print("  ffprobe installieren: sudo pacman -S ffmpeg  (oder apt install ffmpeg)")

    # ── keep-original-title parsen ──────────────────────────────────────────
    keep_original_langs: set[str] | None = None
    if args.keep_original_title:
        keep_original_langs = {
            c.strip().lower()
            for c in args.keep_original_title.split(",")
            if c.strip()
        }
        print(f"Original-Titel erzwungen für: {', '.join(sorted(keep_original_langs))}")

    # ── --locale parsen ──────────────────────────────────────────────────────
    locale: str | None = getattr(args, "locale", None) or None
    if locale:
        print(f"TMDB-Locale: {locale}")

    # ── --format parsen + Locales auto-erkennen ───────────────────────────────
    fmt_str: str = getattr(args, "format", DEFAULT_FORMAT) or DEFAULT_FORMAT
    if fmt_str != DEFAULT_FORMAT:
        print(f"Format-Template: {fmt_str}")
    # Locales automatisch aus {title_XX}-Platzhaltern im Template ableiten
    lang_locales: set[str] | None = None
    detected_locales = set(re.findall(r"\{title_([a-zA-Z_-]+)\}", fmt_str))
    if detected_locales:
        lang_locales = detected_locales
        print(f"Erkannte Locales im Format: {', '.join(sorted(lang_locales))}")

    # lang_locales wird erst später benötigt, hier nur definieren

    # ── Undo-Modus ────────────────────────────────────────────────────────────
    if args.undo:
        undo_renames(args.undo, args.execute is not None)
        return

    # ── Verzeichnis ───────────────────────────────────────────────────────────
    directory = os.path.realpath(args.directory)
    if not os.path.isdir(directory):
        print(f"FEHLER: Kein Verzeichnis: {directory}")
        sys.exit(1)

    # ── Cache laden ───────────────────────────────────────────────────────────
    cache_path = args.cache or os.path.join(directory, ".tmdb-rename-cache.json")
    cache = cache_load(cache_path)

    # ── API-Key bestimmen ─────────────────────────────────────────────────────
    api_key = args.api_key or os.environ.get("TMDB_API_KEY", "")
    if not api_key:
        if cache:
            print(f"⚠ Kein API-Key — nur Cache wird verwendet ({len(cache)} Einträge).")
        else:
            print("⚠ Kein API-Key und kein Cache — nur bereits korrekt benannte Dateien werden verarbeitet.")
            print("  Für TMDB-Suche: --api-key KEY  oder  export TMDB_API_KEY=KEY")

    # ── Modus bestimmen ───────────────────────────────────────────────────────
    execute   = args.execute is not None
    move_mode = execute and args.execute != ""
    dest_dir  = os.path.realpath(args.execute) if move_mode else directory

    if move_mode:
        os.makedirs(dest_dir, exist_ok=True)

    if execute and move_mode:
        modus = f"★ EXECUTE → verschieben nach: {dest_dir}"
    elif execute:
        modus = "★ EXECUTE — in-place umbenennen"
    else:
        modus = "DRY-RUN (nur Vorschau)"

    # ── Dateien sammeln ───────────────────────────────────────────────────────
    if args.recursive is not False:
        video_items = collect_video_files(directory, args.recursive)
        if args.recursive is None:
            recursive_info = " (rekursiv, alle Ebenen)"
        else:
            ebenen = "Ebene" if args.recursive == 1 else "Ebenen"
            recursive_info = f" (rekursiv, max {args.recursive} {ebenen})"
    else:
        video_items = [
            (directory, f) for f in sorted(
                f for f in os.listdir(directory)
                if os.path.splitext(f)[1].lower() in VIDEO_EXTS
                and not f.startswith("._")  # AppleDouble-Dateien (macOS) ignorieren
            )
        ]
        recursive_info = ""
    total = len(video_items)

    print(f"Verzeichnis:  {directory}{recursive_info}")
    print(f"Videodateien: {total}")
    print(f"Cache:        {cache_path} ({len(cache)} Einträge)")
    print(f"Modus:        {modus}")
    print("─" * 80)

    renamed = skipped = errors = unchanged = 0
    rename_log:      list[dict]  = []
    already_correct: list[str]  = []
    failed:          list[tuple[str, str, str]] = []
    w = len(str(total))
    cache_dirty = False

    for idx, (subdir, filename) in enumerate(video_items, 1):
        stem, ext = os.path.splitext(filename)
        ext = ext.lower()
        if move_mode:
            if args.skip_source_dirs:
                file_dest_dir = dest_dir
            else:
                rel = os.path.relpath(subdir, directory)
                file_dest_dir = os.path.normpath(os.path.join(dest_dir, rel))
        else:
            file_dest_dir = subdir

        pct = idx / total * 100
        rel = os.path.relpath(os.path.join(subdir, filename), directory)
        print(f"[{idx:>{w}}/{total}] {pct:5.1f}%  {rel[:65]:<65}", end="\r", flush=True)

        # Bereits im Zielformat?
        already_clean = bool(re.match(r"^.+\(\d{4}\)$", stem))
        if already_clean and args.skip_found:
            if move_mode:
                sidecars = [f for f in find_sidecar_files(subdir, stem) if f != filename]
                sc_log = [{"old": sc, "new": sc} for sc in sidecars]
                if execute:
                    try:
                        os.makedirs(file_dest_dir, exist_ok=True)
                        os.rename(os.path.join(subdir, filename), os.path.join(file_dest_dir, filename))
                        for sc in sidecars:
                            sc_src = os.path.join(subdir, sc)
                            sc_dst = os.path.join(file_dest_dir, sc)
                            if not os.path.exists(sc_dst):
                                os.rename(sc_src, sc_dst)
                        renamed += 1
                        rename_log.append({"old": filename, "new": filename, "old_dir": subdir, "new_dir": file_dest_dir, "sidecars": sc_log})
                    except OSError as e:
                        errors += 1
                else:
                    renamed += 1
                    rename_log.append({"old": filename, "new": filename, "old_dir": subdir, "new_dir": file_dest_dir, "sidecars": sc_log})
            else:
                already_correct.append(os.path.relpath(os.path.join(subdir, filename), directory))
                unchanged += 1
            continue

        parsed_title, parsed_year = parse_scene_filename(filename)
        if not parsed_title:
            failed.append((subdir, filename, "kein Titel parsebar"))
            skipped += 1
            continue

        # ── Cache-Lookup ──────────────────────────────────────────────────────
        extra_titles:      dict[str, str] = {}
        orig_lang:         str = ""
        original_title_raw: str = ""
        cached = cache.get(filename)
        if cached:
            tmdb_year          = (cached.get("release_date") or "")[:4] or parsed_year or "????"
            result_id          = cached.get("id", "cache")
            orig_lang          = cached.get("original_language", "")
            original_title_raw = cached.get("original_title", "")
            # chosen_title immer neu ableiten (reagiert auf --keep-original-title-Änderungen)
            cached_result_stub = {
                "id":                result_id,
                "original_title":    original_title_raw,
                "original_language": orig_lang,
                "title":             cached.get("title", ""),
            }
            chosen = choose_title(cached_result_stub, api_key, keep_original_langs, locale=locale)
            # Lokalisierte Zusatztitel aus Cache laden / fehlende nachholen
            if lang_locales:
                cached_locs = dict(cached.get("localized_titles") or {})
                missing = lang_locales - set(cached_locs.keys())
                if missing and api_key:
                    new_locs = get_additional_titles(result_id, missing, api_key)
                    cached_locs.update(new_locs)
                    cache[filename]["localized_titles"] = cached_locs
                    cache_dirty = True
                extra_titles = cached_locs
        elif api_key:
            # ── TMDB-Suche ────────────────────────────────────────────────────
            filepath = os.path.join(subdir, filename)
            mkv_title: str | None = None
            if ext == ".mkv":
                mkv_title = get_mkv_title(filepath)

            # Suchreihenfolge je nach --force-ffprobe:
            # Mit Flag + MKV-Titel: nur ffprobe-Titel, kein Dateiname-Fallback
            # Mit Flag, aber kein MKV-Titel (ffprobe leer/nicht da): normale Suche
            if args.force_ffprobe and mkv_title:
                search_queries = [(mkv_title, None)]
            else:
                search_queries = [(parsed_title, parsed_year), (parsed_title, None)]

            result = None
            used_query = parsed_title
            try:
                for sq, sy in search_queries:
                    if not sq:
                        continue
                    result, used_query = tmdb_search(sq, sy, api_key, locale=locale)
                    time.sleep(args.delay)
                    if result:
                        break

                # Fallback: ffprobe (nur wenn nicht --force-ffprobe, da dort schon drin)
                if not result and not args.force_ffprobe and ext == ".mkv":
                    if mkv_title:
                        result, used_query = tmdb_search(mkv_title, None, api_key, locale=locale)
                        time.sleep(args.delay)
                    elif not _FFPROBE_PATH:
                        # ffprobe wäre hilfreich gewesen, ist aber nicht da
                        print(f"\n  ℹ {filename}")
                        print( "    Tipp: ffprobe installieren (ffmpeg-Paket) für MKV-Metadaten-Fallback")
            except RuntimeError as e:
                failed.append((subdir, filename, f"API-Fehler: {e}"))
                errors += 1
                continue

            if not result:
                failed.append((subdir, filename, f"nicht gefunden — geparst: '{parsed_title}' ({parsed_year or '—'})"))
                errors += 1
                continue

            # Jahres-Sanity-Check: Dateiname hat explizites Jahr → TMDB-Ergebnis muss passen
            if parsed_year:
                result_year = (result.get("release_date") or "")[:4]
                if result_year and abs(int(result_year) - int(parsed_year)) > 1:
                    failed.append((
                        subdir,
                        filename,
                        f"Jahreskonflikt: Dateiname={parsed_year}, TMDB={result_year} "
                        f"({result.get('original_title', '')})",
                    ))
                    errors += 1
                    continue

            chosen             = choose_title(result, api_key, keep_original_langs, locale=locale)
            release_date       = result.get("release_date", "")
            tmdb_year          = release_date[:4] if release_date else parsed_year or "????"
            result_id          = result.get("id", "")
            orig_lang          = result.get("original_language", "")
            original_title_raw = result.get("original_title", "")

            # Lokalisierte Zusatztitel holen (für --lang / {title_XX})
            if lang_locales and result_id:
                extra_titles = get_additional_titles(result_id, lang_locales, api_key)

            # In Cache schreiben
            cache[filename] = {
                "id":                result_id,
                "original_title":    original_title_raw,
                "original_language": orig_lang,
                "title":             result.get("title", ""),
                "release_date":      release_date,
                "chosen_title":      chosen,
                "localized_titles":  extra_titles,
            }
            cache_dirty = True
        else:
            # Kein API-Key, nicht im Cache
            if already_clean:
                # Dateiname bereits im Zielformat — direkt verschieben ohne TMDB-Lookup
                if move_mode:
                    sidecars = [f for f in find_sidecar_files(subdir, stem) if f != filename]
                    sc_log = [{"old": sc, "new": sc} for sc in sidecars]
                    if execute:
                        try:
                            os.makedirs(file_dest_dir, exist_ok=True)
                            os.rename(os.path.join(subdir, filename), os.path.join(file_dest_dir, filename))
                            for sc in sidecars:
                                sc_src = os.path.join(subdir, sc)
                                sc_dst = os.path.join(file_dest_dir, sc)
                                if not os.path.exists(sc_dst):
                                    os.rename(sc_src, sc_dst)
                            renamed += 1
                            rename_log.append({"old": filename, "new": filename, "old_dir": subdir, "new_dir": file_dest_dir, "sidecars": sc_log})
                        except OSError as e:
                            failed.append((subdir, filename, str(e)))
                            errors += 1
                    else:
                        renamed += 1
                        rename_log.append({"old": filename, "new": filename, "old_dir": subdir, "new_dir": file_dest_dir, "sidecars": sc_log})
                else:
                    already_correct.append(os.path.relpath(os.path.join(subdir, filename), directory))
                    unchanged += 1
                continue
            failed.append((subdir, filename, "nicht im Cache — kein API-Key"))
            errors += 1
            continue

        new_stem, new_name = build_new_name(
            chosen, tmdb_year, args.sep, ext,
            fmt=fmt_str, extra_titles=extra_titles,
            lang=orig_lang, original_title=original_title_raw,
        )

        # Bereits korrekt benannt? (Zielname == Quellname, in-place)
        if new_name == filename and not move_mode:
            already_correct.append(os.path.relpath(os.path.join(subdir, filename), directory))
            unchanged += 1
            continue

        sidecars = [f for f in find_sidecar_files(subdir, stem) if f != filename]

        # Duplikat-Schutz: .(2), .(3) … anhängen falls Ziel belegt
        final_stem, final_name = unique_dest(file_dest_dir, new_stem, ext)

        sc_log = [
            {"old": sc, "new": f"{final_stem}{sc[len(stem):]}"}  
            for sc in sidecars
        ]

        if execute:
            try:
                src = os.path.join(subdir, filename)
                dst = os.path.join(file_dest_dir, final_name)
                os.makedirs(file_dest_dir, exist_ok=True)
                os.rename(src, dst)
                for sc in sidecars:
                    sc_suffix = sc[len(stem):]
                    sc_new    = f"{final_stem}{sc_suffix}"
                    sc_src    = os.path.join(subdir, sc)
                    sc_dst    = os.path.join(file_dest_dir, sc_new)
                    if not os.path.exists(sc_dst):
                        os.rename(sc_src, sc_dst)
                if args.nfo:
                    write_nfo(file_dest_dir, final_stem, chosen, tmdb_year, result_id)
                renamed += 1
                rename_log.append({"old": filename, "new": final_name, "old_dir": subdir, "new_dir": file_dest_dir, "sidecars": sc_log})
            except OSError as e:
                failed.append((subdir, filename, str(e)))
                errors += 1
        else:
            renamed += 1
            rename_log.append({"old": filename, "new": final_name, "old_dir": subdir, "new_dir": file_dest_dir, "sidecars": sc_log})

    # ── Cache speichern ───────────────────────────────────────────────────────
    if cache_dirty or (execute and rename_log):
        cache_save(cache_path, cache)

    # ── Interaktive Nachbearbeitung fehlgeschlagener Einträge ─────────────────
    live_resolved: list[tuple[str, str]] = []
    if failed:
        live_resolved = live_mode(
            failed, dest_dir, move_mode, directory, args.skip_source_dirs, args.sep, api_key, cache, execute,
            nfo=args.nfo,
            keep_original_langs=keep_original_langs,
            fmt=fmt_str,
            lang_locales=lang_locales,
            locale=locale,
        )
        if live_resolved:
            rename_log.extend(live_resolved)
            renamed += len(live_resolved)
            # Nur noch wirklich nicht aufgelöste in failed lassen
            resolved_files = {e["old"] for e in live_resolved}
            failed = [(sd, fn, r) for sd, fn, r in failed if fn not in resolved_files]
            cache_save(cache_path, cache)

    # ── Backup schreiben ──────────────────────────────────────────────────────
    backup_path = ""
    if execute and rename_log:
        ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M")
        backup_path = os.path.join(directory, f".tmdb-rename-backup-{ts}.json")
        backup_data = {
            "created":          ts,
            "source_directory": directory,
            "dest_directory":   dest_dir,
            "move_mode":        move_mode,
            "renames":          rename_log,
        }
        with open(backup_path, "w", encoding="utf-8") as f:
            json.dump(backup_data, f, ensure_ascii=False, indent=2)

    # ── Zusammenfassung ───────────────────────────────────────────────────────
    print(" " * 80, end="\r")
    print("═" * 80)

    if execute and move_mode:
        verb = "Verschoben"
    elif execute:
        verb = "Umbenannt"
    else:
        verb = "Würde umbenennen"
    print(f"{verb}: {renamed}  |  Unverändert: {unchanged}  |  Fehler: {errors + skipped}")

    if already_correct:
        print(f"\nBereits korrekt benannt ({len(already_correct)}):")
        for fn in already_correct:
            print(f"  ✓ {fn}  (keine Umbenennung nötig)")

    if rename_log:
        if execute and move_mode:
            label = f"Verschobene Dateien (→ {dest_dir})"
        elif execute:
            label = "Umbenannte Dateien"
        else:
            label = "Vorschau"
        print(f"\n{label} ({len(rename_log)}):")
        for entry in rename_log:
            old_dir_e = entry.get("old_dir", directory)
            new_dir_e = entry.get("new_dir", dest_dir)
            rel_old = os.path.relpath(os.path.join(old_dir_e, entry['old']), directory)
            rel_new = os.path.relpath(os.path.join(new_dir_e, entry['new']), dest_dir)
            print(f"  {rel_old}")
            print(f"    → {rel_new}")

    if failed:
        hint = ""
        print(f"\nFehlgeschlagen ({len(failed)}){hint}:")
        for _, fn, reason in failed:
            print(f"  ✗ {fn}")
            print(f"    {reason}")

    if execute and backup_path:
        print(f"\nBackup: {backup_path}")
        print(f"Undo:   python3 {os.path.basename(__file__)} {directory} --undo {backup_path} --execute")
    elif not execute and renamed > 0:
        dest_hint = f" {dest_dir}" if move_mode else ""
        print(f"\n→ Mit --execute{dest_hint} wirklich {'verschieben' if move_mode else 'umbenennen'}.")


if __name__ == "__main__":
    main()

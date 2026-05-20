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
  python3 tmdb-rename.py /quelle --live                                    # fehlgeschlagene interaktiv nachbearbeiten
  python3 tmdb-rename.py /quelle --keep-original-title en,de               # Original-Titel für EN/DE-Filme erzwingen
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
import argparse
import datetime
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

    keep_original_langs (z.B. {"en", "de"}):
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
        if orig_lang in keep_original_langs:
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


def _norm_title_cmp(s: str) -> str:
    """Normalisiert Titel für unscharfen Vergleich.
    Superscript→ASCII-Ziffern, Separatoren+Satzzeichen→Leerzeichen, Kleinschreibung."""
    s = s.translate(_SUPERSCRIPT_TRANS)
    s = re.sub(r"[.\-–—_:,!?'\"]+", " ", s)
    return re.sub(r"\s+", " ", s).lower().strip()


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
    raw_title = re.sub(r"[-–—\s]+$", "", raw_title).strip()
    raw_title = re.sub(r"^[-–—\s]+", "", raw_title).strip()
    raw_title = re.sub(r"\s{2,}", " ", raw_title)

    return raw_title, year


def sanitize_for_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00]', "", name)
    name = re.sub(r"\s{2,}", " ", name).strip()
    return name


DEFAULT_FORMAT = "{title}{sep}({year})"


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
    failed: list[tuple[str, str]],
    directory: str,
    dest_dir: str,
    sep: str,
    api_key: str,
    cache: dict,
    execute: bool,
    nfo: bool = False,
    keep_original_langs: set[str] | None = None,
    fmt: str = DEFAULT_FORMAT,
    lang_locales: set[str] | None = None,
    locale: str | None = None,
) -> list[tuple[str, str]]:
    """
    Interaktiver Modus für nicht erkannte Dateien.
    Gibt Liste der dabei erfolgreich aufgelösten (old, new) zurück.
    """
    if not failed:
        return []
    if not api_key:
        print("⚠ Live-Modus benötigt einen API-Key.")
        return []

    resolved: list[tuple[str, str]] = []

    print(f"\n{'═' * 80}")
    print(f"LIVE-MODUS — {len(failed)} nicht erkannte Datei(en)")
    print("Befehle: <Suchbegriff> [+JAHR]  |  s = überspringen  |  q = beenden")
    print(f"{'═' * 80}\n")

    for filename, reason in failed:
        stem, ext = os.path.splitext(filename)
        ext = ext.lower()
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
            if raw.lower() == "s" or raw == "":
                if raw == "":
                    # Leere Eingabe → Standard-Query nochmal versuchen
                    raw = parsed_title
                else:
                    print("  übersprungen.")
                    break

            # Jahr aus Query extrahieren falls angegeben: "titel +2024"
            year_override = parsed_year
            year_in_query = re.search(r"\+(\d{4})$", raw)
            if year_in_query:
                year_override = year_in_query.group(1)
                raw = raw[: year_in_query.start()].strip()

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

            try:
                choice = input("Auswahl: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n→ Live-Modus abgebrochen.")
                return resolved

            if choice == "0" or choice == "":
                continue
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

            sidecars = [f for f in find_sidecar_files(directory, stem) if f != filename]

            final_stem, final_name = unique_dest(dest_dir, new_stem, ext)
            if final_name != new_name:
                print(f"  ⚠ Duplikat → {final_name}")

            if execute:
                try:
                    src = os.path.join(directory, filename)
                    dst = os.path.join(dest_dir, final_name)
                    os.rename(src, dst)
                    for sc in sidecars:
                        sc_suffix = sc[len(stem):]
                        sc_new    = f"{final_stem}{sc_suffix}"
                        os.rename(
                            os.path.join(directory, sc),
                            os.path.join(dest_dir, sc_new),
                        )
                    if nfo:
                        write_nfo(dest_dir, final_stem, chosen, tmdb_year, result["id"])
                    resolved.append((filename, final_name))
                    print("  ✓ verschoben.")
                except OSError as e:
                    print(f"  ✗ Fehler: {e}")
            else:
                resolved.append((filename, final_name))
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
        current = os.path.join(dest_dir, new)
        restore = os.path.join(src_dir,  old)
        if not os.path.exists(current):
            print(f"  ⚠ nicht vorhanden (schon zurück?): {new}")
            err += 1
            continue
        arrow = f"{dest_dir}/{new}" if move_mode else new
        print(f"  {arrow}")
        print(f"    → {src_dir}/{old}")
        if execute:
            try:
                os.rename(current, restore)
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
        "--live",
        action="store_true",
        help="Fehlgeschlagene Dateien nach dem Lauf interaktiv nachbearbeiten",
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
        default="de,en",
        help="Komma-separierte ISO-639-1-Codes (z.B. 'en,de'). "
             "Filme dieser Originalsprachen werden mit ihrem TMDB-Originaltitel benannt. "
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
        "--skip-found",
        action="store_true",
        help="Dateien überspringen, die schon im Format 'Titel(Jahr).ext' sind",
    )
    parser.add_argument(
        "--undo",
        metavar="BACKUP_JSON",
        help="Umbenennung rückgängig machen anhand einer Backup-JSON-Datei",
    )
    args = parser.parse_args()

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

    # live_mode benötigt lang_locales erst später, hier nur definieren

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
    if not api_key and not cache:
        print("FEHLER: Kein API-Key und kein Cache vorhanden.")
        print("  Option A: --api-key KEY")
        print("  Option B: export TMDB_API_KEY=KEY")
        print("  API-Key holen: https://www.themoviedb.org/settings/api")
        sys.exit(1)
    if not api_key:
        print(f"⚠ Kein API-Key — nur Cache wird verwendet ({len(cache)} Einträge).")

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
    video_files = sorted(
        f for f in os.listdir(directory)
        if os.path.splitext(f)[1].lower() in VIDEO_EXTS
        and not f.startswith("._")   # AppleDouble-Dateien (macOS) ignorieren
    )
    total = len(video_files)

    print(f"Verzeichnis:  {directory}")
    print(f"Videodateien: {total}")
    print(f"Cache:        {cache_path} ({len(cache)} Einträge)")
    print(f"Modus:        {modus}")
    print("─" * 80)

    renamed = skipped = errors = unchanged = 0
    rename_log: list[tuple[str, str]] = []
    failed:     list[tuple[str, str]] = []
    w = len(str(total))
    cache_dirty = False

    for idx, filename in enumerate(video_files, 1):
        stem, ext = os.path.splitext(filename)
        ext = ext.lower()

        pct = idx / total * 100
        print(f"[{idx:>{w}}/{total}] {pct:5.1f}%  {filename[:65]:<65}", end="\r", flush=True)

        # Bereits im Zielformat?
        already_clean = bool(re.match(r"^.+\(\d{4}\)$", stem))
        if already_clean and args.skip_found:
            unchanged += 1
            continue

        parsed_title, parsed_year = parse_scene_filename(filename)
        if not parsed_title:
            failed.append((filename, "kein Titel parsebar"))
            skipped += 1
            continue

        # ── Cache-Lookup ──────────────────────────────────────────────────────
        extra_titles:      dict[str, str] = {}
        orig_lang:         str = ""
        original_title_raw: str = ""
        cached = cache.get(filename)
        if cached:
            chosen             = cached["chosen_title"]
            tmdb_year          = (cached.get("release_date") or "")[:4] or parsed_year or "????"
            result_id          = cached.get("id", "cache")
            orig_lang          = cached.get("original_language", "")
            original_title_raw = cached.get("original_title", "")
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
            try:
                result, _ = tmdb_search(parsed_title, parsed_year, api_key, locale=locale)
                time.sleep(args.delay)
            except RuntimeError as e:
                failed.append((filename, f"API-Fehler: {e}"))
                errors += 1
                continue

            if not result:
                failed.append((filename, f"nicht gefunden — geparst: '{parsed_title}' ({parsed_year or '—'})"))
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
            failed.append((filename, "nicht im Cache — kein API-Key"))
            errors += 1
            continue

        new_stem, new_name = build_new_name(
            chosen, tmdb_year, args.sep, ext,
            fmt=fmt_str, extra_titles=extra_titles,
            lang=orig_lang, original_title=original_title_raw,
        )

        if new_name == filename:
            unchanged += 1
            continue

        sidecars = [f for f in find_sidecar_files(directory, stem) if f != filename]

        # Duplikat-Schutz: .(2), .(3) … anhängen falls Ziel belegt
        final_stem, final_name = unique_dest(dest_dir, new_stem, ext)

        if execute:
            try:
                src = os.path.join(directory, filename)
                dst = os.path.join(dest_dir, final_name)
                os.rename(src, dst)
                for sc in sidecars:
                    sc_suffix = sc[len(stem):]
                    sc_new    = f"{final_stem}{sc_suffix}"
                    sc_src    = os.path.join(directory, sc)
                    sc_dst    = os.path.join(dest_dir, sc_new)
                    if not os.path.exists(sc_dst):
                        os.rename(sc_src, sc_dst)
                if args.nfo:
                    write_nfo(dest_dir, final_stem, chosen, tmdb_year, result_id)
                renamed += 1
                rename_log.append((filename, final_name))
            except OSError as e:
                failed.append((filename, str(e)))
                errors += 1
        else:
            renamed += 1
            rename_log.append((filename, final_name))

    # ── Cache speichern ───────────────────────────────────────────────────────
    if cache_dirty or (execute and rename_log):
        cache_save(cache_path, cache)

    # ── Live-Modus ────────────────────────────────────────────────────────────
    live_resolved: list[tuple[str, str]] = []
    if args.live and failed:
        live_resolved = live_mode(
            failed, directory, dest_dir, args.sep, api_key, cache, execute,
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
            resolved_files = {fn for fn, _ in live_resolved}
            failed = [(fn, r) for fn, r in failed if fn not in resolved_files]
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
            "renames":          [{"old": old, "new": new} for old, new in rename_log],
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

    if rename_log:
        if execute and move_mode:
            label = f"Verschobene Dateien (→ {dest_dir})"
        elif execute:
            label = "Umbenannte Dateien"
        else:
            label = "Vorschau"
        print(f"\n{label} ({len(rename_log)}):")
        for old, new in rename_log:
            print(f"  {old}")
            print(f"    → {new}")

    if failed:
        hint = "  (mit --live interaktiv nachbearbeiten)" if not args.live else ""
        print(f"\nFehlgeschlagen ({len(failed)}){hint}:")
        for fn, reason in failed:
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

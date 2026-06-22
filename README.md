# tmdb-rename

**Scene-Release-Dateien automatisch in Kodi/TMDB-kompatible Namen umbenennen.**

Sucht Filme via [TMDB API](https://www.themoviedb.org/settings/api) und benennt sie ins Format:

```
Movie.Title.(2024).mkv
Movie.Title.(2024).-Deutscher.Titel.mkv   # mit --format-Template
```

Kodi erkennt Dateien in diesem Format zuverlässig — unabhängig von der Spracheinstellung.

---

## Voraussetzungen

- Python 3.10+
- TMDB API-Key (kostenlos: https://www.themoviedb.org/settings/api)
- Optional: `ffprobe` (aus dem `ffmpeg`-Paket) für MKV-Metadaten-Fallback

---

## Installation

```bash
git clone https://github.com/tobmanbot/tmdb-rename.git
cd tmdb-rename
```

API-Key setzen (einmalig):

```bash
export TMDB_API_KEY=dein_key_hier
```

---

## Standard-Workflow

Der typische Ablauf arbeitet im **aktuellen Verzeichnis** mit **in-place-Umbenennung**:

```bash
cd /pfad/zu/filmen

# Schritt 1: Dry-Run — TMDB-Suche, Vorschau, Live-Mode für unbekannte Dateien
python3 tmdb-rename.py --api-key XYZ

# Schritt 2: Umbenennung wirklich durchführen (Cache wird genutzt, kein API-Key nötig)
python3 tmdb-rename.py --execute
```

Oder in einem Schritt:

```bash
python3 tmdb-rename.py --api-key XYZ --execute
```

Nach `--execute` gibt das Programm automatisch den passenden Undo-Befehl aus:

```
Backup: .tmdb-rename-backup-2024-01-15_20-30.json
Undo:   python3 tmdb-rename.py . --undo .tmdb-rename-backup-2024-01-15_20-30.json --execute
```

### Dateien in ein anderes Verzeichnis verschieben

`--execute` ohne Argument → **in-place umbenennen**  
`--execute /zielverzeichnis` → **umbenennen und dorthin verschieben**

```bash
python3 tmdb-rename.py --api-key XYZ --execute /media/filme
```

---

## Optionen

### Grundlegendes

| Option | Beschreibung |
|---|---|
| `--execute [ZIELDIR]` | Umbenennung durchführen. Ohne Argument: **in-place** (Standard). Mit Pfad: dorthin verschieben. |
| `--api-key KEY` | TMDB API-Key (alternativ: Umgebungsvariable `TMDB_API_KEY`) |
| `--sep ZEICHEN` | Trennzeichen innerhalb von Titelwörtern (Standard: `.`, Alternative: ` `) |
| `--delay SEKUNDEN` | Pause zwischen API-Anfragen (Standard: `0.3`) |

### Rekursion

```bash
# Alle Unterverzeichnisse einbeziehen
python3 tmdb-rename.py /filme --recursive

# Nur eine Ebene tief
python3 tmdb-rename.py /filme -r1

# Zwei Ebenen tief
python3 tmdb-rename.py /filme -r2
```

Ohne `--recursive` werden nur Dateien direkt im angegebenen Verzeichnis verarbeitet.

### Titelauswahl

```bash
# Original-Titel für alle Sprachen mit lateinischem Schriftsystem
python3 tmdb-rename.py /filme --keep-original-title latin

# Original-Titel nur für bestimmte Sprachen (ISO-639-1)
python3 tmdb-rename.py /filme --keep-original-title en,de

# Kombiniert: Latin-Sprachen + Japanisch
python3 tmdb-rename.py /filme --keep-original-title latin,ja

# TMDB-Suchanfragen auf Deutsch
python3 tmdb-rename.py /filme --locale de-DE
```

Standardverhalten (ohne `--keep-original-title`): Lateinisches Original → `original_title`, alle anderen Sprachen (Japanisch, Arabisch, Kyrillisch, …) → englischer Titel.

### Format-Templates

Das Ausgabeformat ist vollständig konfigurierbar:

```bash
# Standard-Format (zweisprachig Englisch + Deutsch)
python3 tmdb-rename.py /filme --format "{title}{sep}({year})[{sep}-{sep}{title_de}]"

# Nur Originaltitel + Jahr
python3 tmdb-rename.py /filme --format "{title}{sep}({year})"

# Deutsch + Englisch
python3 tmdb-rename.py /filme --format "{title_de}{sep}({year}){sep}[{title_en}]"

# Mit TMDB-Sprachcode
python3 tmdb-rename.py /filme --format "{title}{sep}({year}){sep}[{lang}]"
```

**Verfügbare Platzhalter:**

| Platzhalter | Beschreibung |
|---|---|
| `{title}` | Primärtitel (beeinflusst durch `--keep-original-title`) |
| `{year}` | Erscheinungsjahr |
| `{sep}` | Konfigurierter Separator (`--sep`) |
| `{lang}` | Sprachcode des Originals (z.B. `de`, `en`, `fr`) |
| `{original_title}` | Unberührter `original_title` aus TMDB |
| `{title_XX}` | Lokalisierter Titel für Locale `XX` (z.B. `{title_de}`, `{title_en}`, `{title_fr}`) |

**Optionale Blöcke `[...]`:** Teile des Templates in eckigen Klammern werden weggelassen, wenn alle darin enthaltenen `{title_XX}`-Werte mit dem Primärtitel übereinstimmen oder leer sind.

```
{title}{sep}({year})[{sep}-{sep}{title_de}]

→  Wenn Deutsch == Primärtitel:   The.Dark.Knight.(2008)
→  Wenn Deutsch abweicht:         The.Dark.Knight.(2008).-The.Dark.Knight.Rises
```

### Weitere Optionen

```bash
# MKV-Container-Titel als primäre Suchquelle (statt Dateiname)
python3 tmdb-rename.py /filme --force-ffprobe

# Kodi NFO-Datei mit TMDB-ID neben jede Videodatei schreiben
python3 tmdb-rename.py /filme --nfo

# Dateien überspringen, die schon im Format "Titel(Jahr).ext" sind
python3 tmdb-rename.py /filme --skip-found

# Bei --execute /ziel: Unterverzeichnisstruktur nicht spiegeln (flach kopieren)
python3 tmdb-rename.py /quelle --execute /ziel --skip-source-dirs

# Eigene Cache-Datei angeben
python3 tmdb-rename.py /filme --cache /pfad/zum/cache.json
```

---

## Cache

Ergebnisse werden automatisch in `.tmdb-rename-cache.json` im Quellverzeichnis gespeichert.
Beim nächsten Lauf werden gecachte Dateien nicht erneut via API gesucht.

Vorteile:
- Spart API-Anfragen
- Funktioniert offline für bereits bekannte Dateien
- Reagiert dynamisch auf `--keep-original-title`-Änderungen (Titel wird neu berechnet)

---

## Undo

Jede `--execute`-Operation erstellt automatisch eine Backup-JSON-Datei:

```bash
# Vorschau: Was würde zurückgesetzt?
python3 tmdb-rename.py /filme --undo .tmdb-rename-backup-2024-01-15_20-30.json

# Wirklich zurücksetzen
python3 tmdb-rename.py /filme --undo .tmdb-rename-backup-2024-01-15_20-30.json --execute
```

---

## Live-Modus

Dateien, die nicht automatisch erkannt werden konnten, landen im interaktiven Live-Modus:

```
Datei:   Irgendwas.German.1080p.BluRay.mkv
Geparst: 'Irgendwas'  Jahr: —
Grund:   nicht gefunden — geparst: 'Irgendwas' (—)

Suche [Irgendwas]: Der König der Löwen +1994
  [1] The Lion King / Der König der Löwen (1994)
  [2] ...
  [0] Erneut suchen
  [s] Überspringen (korrekt benannt)
  [i] Ignorieren

Auswahl: 1
```

Befehle:
- Freitext → neue Suche, optional `+JAHR` anhängen (z.B. `Titelname +2001`)
- `s` → als korrekt benannt überspringen (im Move-Mode: unverändert verschieben)
- `i` → ignorieren
- `q` → Live-Modus beenden

---

## Sidecar-Dateien

Untertitel und Metadaten-Dateien (`.srt`, `.sub`, `.idx`, `.ass`, `.ssa`, `.nfo`) werden automatisch zusammen mit der Videodatei umbenannt/verschoben, sofern sie den gleichen Dateinamen-Stamm haben.

---

## Beispiele

```bash
# Alle Filme rekursiv scannen, zweisprachige Namen, Dry-Run
python3 tmdb-rename.py /media/filme -r \
    --format "{title}{sep}({year})[{sep}-{sep}{title_de}]" \
    --keep-original-title latin

# Gleiche Aktion, wirklich ausführen
python3 tmdb-rename.py /media/filme -r --execute \
    --format "{title}{sep}({year})[{sep}-{sep}{title_de}]" \
    --keep-original-title latin

# Sortierte Bibliothek: aus Inbox in Zielordner verschieben
python3 tmdb-rename.py ~/Downloads/filme --execute /media/filme/

# Nur für japanische und lateinische Filme Originaltitel erzwingen
python3 tmdb-rename.py /filme --keep-original-title latin,ja --execute
```

---

## Lizenz

MIT

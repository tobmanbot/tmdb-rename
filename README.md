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

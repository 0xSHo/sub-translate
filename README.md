# sub_translate

Übersetzt Untertitel neuer Filme automatisch ins Deutsche und legt sie als `Film.de.srt` neben den Film, sodass Jellyfin, Plex oder Kodi sie direkt finden. Optional werden fertige Filme danach auf einen Medienserver verschoben.

Gedacht für Windows mit einer Nvidia-Grafikkarte: Die Übersetzung läuft lokal über ein Sprachmodell (Ollama), ohne Kosten und ohne Zeichenlimit.

## Ablauf pro Film

1. **`Film.de.srt` existiert schon:** fertig.
2. **Deutsche Text-Untertitel im Film:** nichts zu tun.
3. **Untertiteldatei mit Sprachkürzel daneben** (`Film.en.srt`, `Film.fr.srt` …): übersetzen.
4. **Text-Untertitelspur im Film:** auslesen und übersetzen, Englisch bevorzugt, sonst andere Sprachen.
5. **Nur Bild-Untertitel (Blu-ray PGS):** per OCR (Tesseract) erkennen, dann übersetzen.
6. **Keine Untertitel:** überspringen (im Log vermerkt).

Die Zeitstempel kommen immer aus der Spur, die zum Film gehört. So gibt es kein verrutschtes Timing wie bei heruntergeladenen Untertiteln.

**Übersetzer**, in dieser Reihenfolge (`--use`):

| Übersetzer | Kosten | Qualität | Hinweis |
|---|---|---|---|
| Ollama (lokal, z. B. `gemma3:12b`) | kostenlos | sehr gut, versteht Zusammenhang | braucht eine Grafikkarte mit ca. 12 GB+ |
| Azure Translator (F0) | 2 Mio. Zeichen/Monat frei | gut | nur Rückfallebene |
| DeepL API | begrenztes Freikontingent | sehr gut | nur Rückfallebene |
| Argos Translate | kostenlos, offline | mäßig | letzte Rückfallebene |

Ist Ollama nur kurz weg (z. B. nach dem Hochfahren), wartet das Skript bis zu 60 Minuten, statt die Kontingente von Azure oder DeepL zu verbrauchen.

## Weitere Funktionen

- **Dauerbetrieb** mit `--watch`: Der Ordner wird alle 5 Minuten geprüft. Laufende Downloads (jünger als 3 Minuten) und Samples (unter 100 MB) werden übersprungen.
- **Spielpause:** Während ein Spiel oder Vollbildprogramm läuft oder die GPU ausgelastet ist, wartet das Skript und gibt den Grafikspeicher frei.
- **Verschieben** mit `--move-to`: Fertige Filme samt Untertiteln wandern auf den Medienserver. Das ist sicher bei Netzabbrüchen, denn es wird erst kopiert, dann geprüft und erst danach gelöscht.
- **Notensymbole ♪** bleiben erhalten, Kursivschrift bei Ollama, Azure und DeepL ebenfalls.
- **Robust:** Sperrdateien gegen doppelte Bearbeitung, Merkliste für nicht übersetzbare Filme, Log mit Größenbegrenzung.

## Installation (Windows)

```powershell
# 1. Programme
winget install Python.Python.3.12
winget install Gyan.FFmpeg
winget install Ollama.Ollama
winget install --id UB-Mannheim.TesseractOCR      # optional, für Bild-Untertitel

# 2. Neues PowerShell-Fenster öffnen, dann:
git clone https://github.com/<BENUTZER>/<REPO>.git C:\subtrans
cd C:\subtrans
py -3.12 -m venv venv
.\venv\Scripts\pip install -r requirements.txt

# 3. Sprachmodell laden (ca. 8 GB)
ollama pull gemma3:12b
```

Für parallele Anfragen an Ollama (deutlich schneller) einmalig setzen und Ollama danach neu starten:

```powershell
setx OLLAMA_NUM_PARALLEL 5
setx OLLAMA_FLASH_ATTENTION 1
setx OLLAMA_KV_CACHE_TYPE q8_0
```

**Deutsch für OCR** (nur für `--ocr-german`), in einer Administrator-PowerShell:

```powershell
Invoke-WebRequest "https://github.com/tesseract-ocr/tessdata/raw/main/deu.traineddata" `
  -OutFile "C:\Program Files\Tesseract-OCR\tessdata\deu.traineddata"
```

### Zugangsdaten (optional)

Die Keys liegen als Textdateien neben dem Skript und sind per `.gitignore` vom Repo ausgeschlossen:

- `azure_key.txt`: Zeile 1 Key, Zeile 2 Region (z. B. `westeurope`)
- `deepl_key.txt`: der DeepL-Key

Alternativ gehen die Umgebungsvariablen `AZURE_TRANSLATOR_KEY`, `AZURE_TRANSLATOR_REGION` und `DEEPL_API_KEY`. Ohne Keys werden diese Übersetzer einfach übersprungen.

## Benutzung

```powershell
# einzelner Film
.\venv\Scripts\python sub_translate.py "D:\Filme\Film.mkv"

# ganzer Ordner einmalig
.\venv\Scripts\python sub_translate.py "D:\Filme" --prefer-sdh

# Erstdurchlauf über eine große Bibliothek ohne Azure/DeepL-Verbrauch
.\venv\Scripts\python sub_translate.py "D:\Filme" --use ollama,argos --log-file erstlauf.txt
```

### Dauerbetrieb als geplante Aufgabe

```powershell
.\install_task.ps1 -Watch "C:\Users\<NAME>\Desktop\Filme" -MoveTo "\\SERVER\Medien\Filme"
Start-ScheduledTask -TaskName SubTranslate
Get-Content .\log.txt -Tail 30 -Encoding UTF8 -Wait
```

Die Aufgabe startet bei jeder Anmeldung, läuft ohne Fenster und ohne Zeitlimit. Für das Ziel besser den Netzwerkpfad (`\\SERVER\...`) als einen Laufwerksbuchstaben verwenden, denn verbundene Laufwerke sind für geplante Aufgaben nicht immer sichtbar.

**Neustart, z. B. nach einem Update:** `Stop-ScheduledTask` beendet nur den Starter der Python-Umgebung, das Skript selbst läuft weiter. Deshalb zusätzlich den Prozess beenden:

```powershell
Stop-ScheduledTask -TaskName SubTranslate
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
  Where-Object CommandLine -like "*sub_translate*" |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
git pull                                   # bei Updates
Start-ScheduledTask -TaskName SubTranslate
```

Startet versehentlich eine zweite Instanz, beendet sie sich selbst mit „Dauerbetrieb läuft bereits …“.

**Anderer Ordner:** Das Skript enthält keinen festen Pfad. Es reicht, `install_task.ps1` mit dem neuen `-Watch` erneut auszuführen und danach wie oben neu zu starten. Ist der Ordner beim Start noch nicht da (z. B. externes Laufwerk), wartet das Skript darauf.

## Übersetzung prüfen

```powershell
.\venv\Scripts\python sub_translate.py "D:\Filme\Film.mkv" --compare --prefer-sdh
```

Schreibt `Film.vergleich.txt` ins aktuelle Verzeichnis: Original und Deutsch Einblendung für Einblendung, mit Zeitstempeln und Hinweis auf abweichende Zeiten. Damit lässt sich prüfen, ob die richtige Spur übersetzt wurde und ob jede deutsche Zeile zu ihrer Originalzeile passt. Bei `--compare` dieselben Spur-Optionen (`--prefer-sdh`) wie im Dauerbetrieb angeben.

Ollama übersetzt standardmäßig **jede Einblendung in einer eigenen Anfrage** (die Nachbarzeilen gehen nur als Kontext mit). Dadurch kann kein Text in eine andere Einblendung rutschen und sich ein Versatz nicht über den Film fortpflanzen. Mit `--ollama-batch 25` werden mehrere Zeilen pro Anfrage übersetzt: schneller, aber mit Restrisiko. Dann muss Ollama in jedem Feld den Anfang der Originalzeile abschreiben, und bei Abweichung wird der Abschnitt neu angefragt.

## Wichtige Optionen

| Option | Bedeutung |
|---|---|
| `--watch` | dauerhaft laufen, alle `--interval` Sekunden prüfen (Standard 300) |
| `--use ollama,azure,deepl,argos` | Übersetzer und Reihenfolge |
| `--ollama-model gemma3:12b` | Ollama-Modell |
| `--ollama-batch 1` | Zeilen pro Ollama-Anfrage (1 = sicher, größer = schneller) |
| `--ollama-parallel 3` | gleichzeitige Anfragen (passend zu `OLLAMA_NUM_PARALLEL`) |
| `--ollama-keep-alive 1m` | wie lange das Modell nach der letzten Anfrage geladen bleibt |
| `--ollama-grace-min 60` | so lange auf Ollama warten, bevor ausgewichen wird |
| `--prefer-sdh` | SDH-Spur (mit ♪ und [Musik]) bevorzugen |
| `--compare` | nur prüfen: Original und `.de.srt` gegenüberstellen |
| `--move-to PFAD` | fertige Filme samt Untertiteln dorthin verschieben |
| `--move-skipped` | auch nicht übersetzbare Filme verschieben |
| `--ocr-german` | deutsche Bild-Untertitel per OCR in eine `.de.srt` umwandeln (vermeidet Transcoding in Jellyfin) |
| `--no-ocr` | keine Texterkennung |
| `--no-game-pause` | nicht pausieren, wenn gespielt wird |
| `--gpu-busy-percent 40` | ab dieser GPU-Auslastung als „Spiel läuft" werten |
| `--min-size-mb 100` / `--min-age 180` | kleinere bzw. jüngere Dateien ignorieren |
| `--log-file PFAD` | Log in Datei (max. 5 MB, 3 ältere Versionen) |

Alle Optionen: `python sub_translate.py --help`

## Grenzen

- OCR nur für Blu-ray-Untertitel (PGS) in lateinischer, kyrillischer oder griechischer Schrift. DVD-Untertitel (VobSub) und asiatische Schriften werden übersprungen.
- Bei OCR gehen Kursivschrift und Position im Bild verloren. Notensymbole werden gelegentlich falsch gelesen.
- Fest ins Bild eingebrannte Untertitel kann kein Skript auslesen.
- Das Auslesen einer Spur liest die ganze Videodatei. Über das Netzwerk dauert das bei großen Remuxen einige Minuten.

## Fehlersuche

| Symptom | Ursache / Lösung |
|---|---|
| Nach dem Start der Aufgabe keine Zeile im Log | Startfehler; Skript einmal direkt mit `python.exe` statt `pythonw.exe` ausführen, dann ist die Meldung sichtbar |
| `übersprungen: kleiner als --min-size-mb` | kurze Episoden: `--min-size-mb 20` |
| `Zielordner … nicht erreichbar` | Netzlaufwerk aus oder Laufwerksbuchstabe statt `\\SERVER\…` verwendet |
| Häufig `übersetzt mit Azure` statt Ollama | Ollama läuft nicht (Autostart prüfen, `ollama ps`) |
| Umlaute im Log kaputt (`Ã¼`) | Anzeigeproblem: `Get-Content … -Encoding UTF8` |

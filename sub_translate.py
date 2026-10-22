#!/usr/bin/env python3
"""Übersetzt Untertitel neuer Videos automatisch ins Deutsche.

Ablauf pro Video (mkv/mp4/m4v):
  1. Gibt es schon Film.de.srt?                         -> überspringen
  2. Hat der Film schon eine deutsche Spur eingebaut?    -> überspringen
  3. Liegt eine Untertiteldatei mit Sprachkürzel daneben
     (Film.en.srt, Film.fr.srt, Film.ja.srt ...)?        -> die nehmen (Englisch bevorzugt)
  4. Sonst: Textspur aus dem Video extrahieren (ffmpeg),
     Englisch bevorzugt, sonst eine andere Sprache
  5. Übersetzen, in dieser Reihenfolge (änderbar mit --use):
       Ollama (lokales Sprachmodell) -> Azure Translator -> DeepL -> Argos (offline).
     Ist ein Übersetzer nicht eingerichtet, nicht erreichbar oder sein Kontingent leer,
     übernimmt der nächste die ganze Datei.
     Keys: azure_key.txt (Zeile 1 Key, Zeile 2 Region) und deepl_key.txt neben dem Skript,
     oder AZURE_TRANSLATOR_KEY / AZURE_TRANSLATOR_REGION / DEEPL_API_KEY.
  6. Film.de.srt neben das Video schreiben (Jellyfin findet sie automatisch)

Bild-Untertitel (Blu-ray PGS) werden, wenn sonst nichts da ist, per OCR (Tesseract) erkannt
und dann übersetzt. Mit --ocr-german werden auch deutsche Bild-Untertitel in eine .de.srt
umgewandelt. DVD-Untertitel (VobSub) und asiatische Schriften werden nicht per OCR erkannt.

Benötigt: ffmpeg/ffprobe, pip install srt requests argostranslate
Aufruf:   python sub_translate.py --watch   (Standardordner: Desktop/Filme des Benutzers)
"""
import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from logging.handlers import RotatingFileHandler
from pathlib import Path

import requests
import srt

# Standardordner, wenn beim Aufruf kein Ordner angegeben wird
DEFAULT_FOLDER = Path.home() / "Desktop" / "Filme"

VIDEO_EXT = {".mkv", ".mp4", ".m4v"}
TEXT_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}
BATCH = 40
NOTES = "♪♫♬♩"
RETRY_HOURS = 24  # vorübergehende Probleme (z. B. Sprache unbekannt ohne DeepL) erneut versuchen

# ISO-639-2/1-Kürzel und englische Namen -> 2-Buchstaben-Code
LANG = {
    "eng": "en", "en": "en", "english": "en",
    "ger": "de", "deu": "de", "de": "de", "german": "de", "deutsch": "de",
    "fre": "fr", "fra": "fr", "fr": "fr", "french": "fr",
    "spa": "es", "es": "es", "spanish": "es",
    "ita": "it", "it": "it", "italian": "it",
    "por": "pt", "pt": "pt", "portuguese": "pt",
    "dut": "nl", "nld": "nl", "nl": "nl", "dutch": "nl",
    "swe": "sv", "sv": "sv", "swedish": "sv",
    "dan": "da", "da": "da", "danish": "da",
    "nor": "nb", "nob": "nb", "no": "nb", "nb": "nb", "norwegian": "nb",
    "fin": "fi", "fi": "fi", "finnish": "fi",
    "pol": "pl", "pl": "pl", "polish": "pl",
    "cze": "cs", "ces": "cs", "cs": "cs", "czech": "cs",
    "hun": "hu", "hu": "hu", "hungarian": "hu",
    "rum": "ro", "ron": "ro", "ro": "ro", "romanian": "ro",
    "bul": "bg", "bg": "bg", "bulgarian": "bg",
    "gre": "el", "ell": "el", "el": "el", "greek": "el",
    "rus": "ru", "ru": "ru", "russian": "ru",
    "ukr": "uk", "uk": "uk", "ukrainian": "uk",
    "tur": "tr", "tr": "tr", "turkish": "tr",
    "ara": "ar", "ar": "ar", "arabic": "ar",
    "heb": "he", "he": "he", "hebrew": "he",
    "hin": "hi", "hi": "hi", "hindi": "hi",
    "tha": "th", "th": "th", "thai": "th",
    "vie": "vi", "vi": "vi", "vietnamese": "vi",
    "ind": "id", "id": "id", "indonesian": "id",
    "chi": "zh", "zho": "zh", "zh": "zh", "cmn": "zh", "yue": "zh",
    "chinese": "zh", "mandarin": "zh", "cantonese": "zh",
    "jpn": "ja", "ja": "ja", "japanese": "ja",
    "kor": "ko", "ko": "ko", "korean": "ko",
}
# Quellsprachen, die DeepL kennt (sonst erkennt DeepL die Sprache selbst)
DEEPL_SRC = {"ar", "bg", "cs", "da", "de", "el", "en", "es", "fi", "fr", "hu", "id", "it", "ja",
             "ko", "nb", "nl", "pl", "pt", "ro", "ru", "sv", "tr", "uk", "zh"}

# Unter Windows bei pythonw.exe kein schwarzes Fenster für ffmpeg/ffprobe aufpoppen lassen
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

log = logging.getLogger("subtrans")


class QuotaExceeded(Exception):
    pass


class Unsupported(Exception):
    pass


class NoBackend(Exception):
    pass


class OllamaDown(Unsupported):
    """Ollama antwortet nicht (Verbindungsfehler)."""


class Postpone(Exception):
    """Film später erneut versuchen, statt auf kostenpflichtige Übersetzer auszuweichen."""


# --------------------------------------------------------------------------
# Übersetzer
# --------------------------------------------------------------------------
class DeepL:
    name = "DeepL"

    def __init__(self, key):
        host = "api-free.deepl.com" if key.endswith(":fx") else "api.deepl.com"
        self.url = f"https://{host}/v2/translate"
        self.headers = {"Authorization": f"DeepL-Auth-Key {key}"}

    def translate(self, texts, src):
        data = [("text", t) for t in texts] + [
            ("target_lang", "DE"),
            ("tag_handling", "html"),
            ("split_sentences", "nonewlines"),
            ("preserve_formatting", "1"),
        ]
        if src in DEEPL_SRC:
            data.append(("source_lang", src.upper()))
        for attempt in range(5):
            r = requests.post(self.url, headers=self.headers, data=data, timeout=60)
            if r.status_code in (456, 403):  # Kontingent leer / Key ungültig
                raise QuotaExceeded(f"DeepL HTTP {r.status_code}")
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return [t["text"] for t in r.json()["translations"]]
        raise RuntimeError("DeepL: zu viele Anfragen (429)")


LANG_NAMES = {
    "en": "English", "fr": "French", "es": "Spanish", "it": "Italian", "pt": "Portuguese",
    "nl": "Dutch", "sv": "Swedish", "da": "Danish", "nb": "Norwegian", "fi": "Finnish",
    "pl": "Polish", "cs": "Czech", "hu": "Hungarian", "ro": "Romanian", "bg": "Bulgarian",
    "el": "Greek", "ru": "Russian", "uk": "Ukrainian", "tr": "Turkish", "ar": "Arabic",
    "he": "Hebrew", "hi": "Hindi", "th": "Thai", "vi": "Vietnamese", "id": "Indonesian",
    "zh": "Chinese", "ja": "Japanese", "ko": "Korean",
}


def clean_single(text, src_line):
    """Antwort im Einzelmodus säubern; None, wenn sie unbrauchbar aussieht."""
    note = re.compile(r"^\s*[(*\[]?\s*(hinweis|anmerkung|erklärung|erläuterung|note|explanation|"
                      r"übersetzung|translation|alternativ|alternative)\b", re.I)
    t = re.sub(r"^(german|deutsch|übersetzung|translation)\s*:\s*", "", text.strip(), flags=re.I).strip()
    # 1) Erklärungszeilen entfernen
    t = "\n".join(l for l in t.splitlines() if l.strip() and not note.match(l)).strip()
    # 2) umschließende Anführungszeichen entfernen, wenn das Original keine hat
    for a, b in [('"', '"'), ("„", "“"), ("“", "”"), ("«", "»"), ("»", "«"), ("'", "'")]:
        if len(t) > 2 and t.startswith(a) and t.endswith(b) and not src_line.strip().startswith(a):
            t = t[len(a):-len(b)].strip()
    lines = [l for l in t.splitlines() if l.strip()]
    src_lines = max(1, len([l for l in src_line.splitlines() if l.strip()]))
    if not lines or len(lines) > src_lines + 1:   # leer oder mit Erklärungen
        return None
    return "\n".join(lines)


def _plain_len(t):
    return len(re.sub(r"<[^>]+>|[\s%s]" % NOTES, "", t))


def _norm(t):
    return re.sub(r"[\W_]+", "", re.sub(r"<[^>]+>", "", t)).lower()


def anchor_ok(echo, src):
    """Hat das Modell den Anfang der richtigen Originalzeile abgeschrieben?"""
    e, s_ = _norm(echo), _norm(src)
    if not s_:
        return True
    if not e:
        return False
    k = min(len(e), len(s_), 10)
    return k >= min(3, len(s_)) and s_.startswith(e[:k])


def shifted(src, out):
    """Erkennt verschobenen Text: Bei korrekter Übersetzung ist das Längenverhältnis
    Deutsch/Original von Zeile zu Zeile ähnlich. Rutscht Text in die Nachbarzeile, wird eine
    Zeile auffällig lang und die andere auffällig kurz. Vergleich mit dem Median des Abschnitts,
    damit es für jede Ausgangssprache funktioniert (auch Chinesisch -> Deutsch)."""
    ratios = [(_plain_len(o) / _plain_len(s_)) for s_, o in zip(src, out) if _plain_len(s_) >= 12]
    if len(ratios) < 3:
        return False
    med = sorted(ratios)[len(ratios) // 2]
    if med <= 0:
        return False
    for s_, o in zip(src, out):
        if _plain_len(s_) >= 12:
            r = _plain_len(o) / _plain_len(s_)
            if r > med * 2.6 or r < med / 2.6:
                return (s_, o)   # auffälliges Paar (für die Meldung)
    return None


class Ollama:
    """Lokales Sprachmodell über Ollama. Übersetzt mit Kontext der vorherigen Zeilen,
    mehrere Abschnitte gleichzeitig (Ollama braucht dafür OLLAMA_NUM_PARALLEL >= parallel)."""
    name = "Ollama"
    batch = 1          # Zeilen pro Anfrage; 1 = jede Einblendung einzeln (kein Verrutschen möglich)
    context_lines = 4  # vorherige Zeilen als Kontext
    after_lines = 2    # folgende Zeilen als Kontext
    whole_file = True  # bekommt die ganze Datei auf einmal, damit parallel gearbeitet werden kann

    def __init__(self, model, url="http://localhost:11434", parallel=3, grace_min=60, keep_alive="1m",
                 batch=1):
        self.model = model
        self.keep_alive = keep_alive
        self.base = url.rstrip("/")
        self.url = self.base + "/api/chat"
        self.parallel = max(1, parallel)
        self.batch = max(1, batch)
        # Im Einzelmodus sind die Anfragen klein: weniger Kontext pro Anfrage spart Grafikspeicher und
        # erlaubt mehr parallele Anfragen. Für alle Anfragen gleich halten, sonst lädt Ollama das Modell neu.
        self.num_ctx = 2048 if self.batch == 1 else 4096
        self.grace = grace_min * 60
        self.down_since = None
        self.name = f"Ollama ({model})"
        self._lock = threading.Lock()
        self.start_file()

    # --- Erreichbarkeit -------------------------------------------------
    def mark_down(self):
        with self._lock:
            if self.down_since is None:
                self.down_since = time.time()

    def available(self):
        try:
            ok = requests.get(self.base + "/api/tags", timeout=3).status_code == 200
        except requests.RequestException:
            ok = False
        if ok:
            self.down_since = None
        else:
            self.mark_down()
        return ok

    def within_grace(self):
        """True, solange Ollama noch nicht länger als die Wartezeit weg ist."""
        return self.down_since is not None and time.time() - self.down_since < self.grace

    def unload(self):
        """Modell sofort aus dem Grafikspeicher entfernen."""
        try:
            requests.post(self.base + "/api/generate", json={"model": self.model, "keep_alive": 0}, timeout=10)
        except requests.RequestException:
            pass

    def start_file(self, total=0):
        self.total = total
        self.done = 0
        self.retries = 0
        self.started = time.time()

    def _post(self, body):
        try:
            r = requests.post(self.url, json=body, timeout=600)
        except requests.RequestException as e:
            self.mark_down()
            raise OllamaDown(f"Ollama nicht erreichbar ({e.__class__.__name__}). Läuft Ollama?")
        if r.status_code == 404:
            raise Unsupported(f"Modell fehlt, einmal ausführen: ollama pull {self.model}")
        if r.status_code >= 400:
            raise Unsupported(f"Ollama HTTP {r.status_code}: {r.text[:200]}")
        content = r.json().get("message", {}).get("content", "")
        return re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()

    def _ask_single(self, line, src, context, after):
        """Eine Einblendung, Antwort als reiner Text (ohne JSON): deutlich weniger zu schreiben,
        daher schneller. Verrutschen ist unmöglich, weil es nur diese eine Zeile gibt."""
        lang = LANG_NAMES.get(src, "the source language")
        system = (
            f"You translate film subtitles from {lang} into natural, idiomatic German as used in "
            "professional German subtitles. You get ONE subtitle line plus neighbouring lines for context. "
            "Translate ONLY that one line, exactly what is said in it; never add content from the context "
            "lines (an incomplete German fragment is fine if the sentence continues). Keep HTML tags like "
            "<i>...</i>, music symbols like ♪ and line breaks. Reply with the German translation only: "
            "no quotes, no notes, no explanations."
        )
        parts = []
        if context:
            parts.append("Previous lines (context, do not translate):\n" + "\n".join(f"- {c}" for c in context))
        parts.append("LINE TO TRANSLATE:\n" + line)
        if after:
            parts.append("Next lines (context, do not translate):\n" + "\n".join(f"- {c}" for c in after))
        body = {
            "model": self.model, "stream": False, "keep_alive": self.keep_alive,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": "\n\n".join(parts)}],
            "options": {"temperature": 0.2, "num_ctx": self.num_ctx,
                        "num_predict": min(400, 40 + 2 * len(line))},
        }
        return clean_single(self._post(body), line)

    def _ask(self, lines, src, context, after=()):
        if len(lines) == 1:
            out = self._ask_single(lines[0], src, context, after)
            return [out] if out else None
        lang = LANG_NAMES.get(src, "the source language (detect it)")
        system = (
            f"You are a professional subtitle translator. Translate film subtitles from {lang} "
            "into natural, idiomatic German as used in professional German subtitles.\n"
            "Rules:\n"
            "- 'lines' maps keys (l1, l2, ...) to subtitle lines. Return an object with exactly the same keys. "
            "Each key holds an object with two fields: 'src' = the first three words of THAT key's line "
            "copied character by character in the ORIGINAL language (do NOT translate 'src'; for languages "
            "without spaces: its first 5 characters), and 'de' = the German translation of THAT key's line.\n"
            "- Each key is shown on screen at its own fixed time. Translate EVERY key on its own, "
            "exactly what is said in that key. NEVER move words, names or meaning into a neighbouring key, "
            "even if a sentence continues in the next key. An incomplete German fragment is fine and "
            "expected in that case. Never leave a key empty.\n"
            "- Keep HTML tags like <i>...</i> and music symbols like ♪ where they are.\n"
            "- Keep line breaks inside an item if sensible; keep it short and readable.\n"
            "- 'context_before' and 'context_after' show the neighbouring lines ONLY for understanding. "
            "Never translate them and never put any of their content into a key.\n"
            "- Output only the JSON object, no explanations."
        )
        keys = [f"l{i + 1}" for i in range(len(lines))]
        user = json.dumps({"context_before": list(context), "lines": dict(zip(keys, lines)),
                           "context_after": list(after)}, ensure_ascii=False)
        # Feste Schlüssel: Ollama erzwingt beim Erzeugen genau diese Felder in dieser Reihenfolge,
        # das Modell kann also keine Zeilen zusammenlegen oder weglassen.
        schema = {
            "type": "object",
            "properties": {k: {"type": "object",
                               "properties": {"src": {"type": "string"}, "de": {"type": "string"}},
                               "required": ["src", "de"], "additionalProperties": False} for k in keys},
            "required": keys,
            "additionalProperties": False,
        }
        body = {
            "model": self.model,
            "stream": False,
            # Modell nach kurzer Leerlaufzeit aus dem Speicher entladen (Standard 1 min, --ollama-keep-alive)
            "keep_alive": self.keep_alive,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "format": schema,
            # num_predict begrenzt die Antwortlänge: verhindert, dass das Modell bei JSON-Ausgabe
            # endlos Leerzeichen erzeugt (bekanntes Problem, kostet sonst Minuten pro Abschnitt)
            # 4096 Token reichen für 25 Zeilen samt Antwort locker und sparen Grafikspeicher,
            # der bei parallelen Anfragen pro Anfrage reserviert wird
            "options": {"temperature": 0.2, "num_ctx": self.num_ctx,
                        "num_predict": min(4000, 200 + 2 * sum(len(l) for l in lines) + 20 * len(lines))},
        }
        try:
            r = requests.post(self.url, json=body, timeout=600)
        except requests.RequestException as e:
            self.mark_down()
            raise OllamaDown(f"Ollama nicht erreichbar ({e.__class__.__name__}). Läuft Ollama?")
        if r.status_code == 404:
            raise Unsupported(f"Modell fehlt, einmal ausführen: ollama pull {self.model}")
        if r.status_code >= 400:
            raise Unsupported(f"Ollama HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
        if data.get("eval_count"):
            log.debug("    Ollama: %s Tokens in %.1f s", data["eval_count"], data.get("eval_duration", 0) / 1e9)
        content = data.get("message", {}).get("content", "")
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()
        try:
            obj = json.loads(content)
            out = [obj[k]["de"] for k in keys]
            echoes = [obj[k]["src"] for k in keys]
        except (json.JSONDecodeError, KeyError, TypeError):
            log.debug("    Ollama: Antwort nicht lesbar: %r", content[:300])
            return None
        if not all(isinstance(x, str) for x in out):
            return None
        # Leere Übersetzung für eine Zeile mit Text = Modell hat Text verschoben -> neu fragen
        if any(not x.strip() and has_letters(src_line) for x, src_line in zip(out, lines)):
            return None
        bad = [] if len(lines) == 1 else [i for i, (e, s_) in enumerate(zip(echoes, lines)) if not anchor_ok(e, s_)]
        if bad:
            i = bad[0]
            log.info("    Ollama: Zeile %d passt nicht zu ihrem Original, frage neu (erwartet %r, geliefert %r)",
                     i + 1, lines[i][:30], echoes[i][:30])
            return None
        odd = shifted(lines, out) if len(lines) > 1 else None
        if odd:
            log.info("    Ollama: Länge auffällig, Text evtl. verschoben, frage neu (%r -> %r)",
                     odd[0][:40], odd[1][:40])
            return None
        return out

    def _translate_chunk(self, lines, src, context, after=()):
        out = self._ask(lines, src, context, after)
        if out is None:  # ungültige Antwort: Abschnitt halbieren und neu fragen
            with self._lock:
                self.retries += 1
            log.info("    Ollama: ungültige Antwort für %d Zeilen, frage in kleineren Teilen neu", len(lines))
            if len(lines) == 1:
                out = self._ask(lines, src, context, after)
                if out is None:
                    # Eine einzelne Zeile lässt nicht den ganzen Film zum nächsten Übersetzer wandern
                    log.warning("    Ollama: Zeile bleibt unübersetzt: %r", lines[0][:80])
                    return lines
                return out
            mid = len(lines) // 2
            second_ctx = (context + lines[:mid])[-self.context_lines:]
            return (self._translate_chunk(lines[:mid], src, context, lines[mid:mid + self.after_lines])
                    + self._translate_chunk(lines[mid:], src, second_ctx, after))
        return out

    def _job(self, chunk, src, context, after):
        res = self._translate_chunk(chunk, src, context, after)
        with self._lock:
            before = self.done
            self.done += len(chunk)
            # Fortschritt höchstens in 10-%-Schritten loggen (bei 1 Zeile pro Anfrage sonst zu viel)
            step = max(1, self.total // 10) if self.total else 1
            if self.done // step != before // step or self.done >= self.total:
                elapsed = time.time() - self.started
                eta = elapsed / self.done * (self.total - self.done) if self.total and self.done else 0
                log.info("    Ollama: %d/%d Zeilen (bisher %.1f min, noch ca. %.0f min)",
                         self.done, self.total, elapsed / 60, eta / 60)
        return res

    def translate(self, texts, src):
        # Schutz-Tags für DeepL braucht das Modell nicht
        texts = [re.sub(r"</?span[^>]*>", "", t) for t in texts]
        jobs = []
        for i in range(0, len(texts), self.batch):
            # Kontext = vorherige Originalzeilen; die sind vorab bekannt, daher sind
            # die Abschnitte voneinander unabhängig und können parallel laufen
            context = texts[max(0, i - self.context_lines):i]
            after = texts[i + self.batch:i + self.batch + self.after_lines]
            jobs.append((texts[i:i + self.batch], context, after))
        pool = ThreadPoolExecutor(max_workers=self.parallel)
        try:
            futures = [pool.submit(self._job, chunk, src, ctx, aft) for chunk, ctx, aft in jobs]
            out = []
            for f in futures:
                out += f.result()  # Reihenfolge bleibt erhalten
            return out
        except BaseException:
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        finally:
            pool.shutdown(wait=False)


# Azure verwendet für Chinesisch eigene Codes; dort lieber automatisch erkennen lassen
AZURE_SRC = DEEPL_SRC - {"zh"} | {"he", "hi", "th", "vi"}


class Azure:
    name = "Azure"

    def __init__(self, key, region=None):
        self.url = "https://api.cognitive.microsofttranslator.com/translate"
        self.headers = {"Ocp-Apim-Subscription-Key": key, "Content-Type": "application/json"}
        if region:
            self.headers["Ocp-Apim-Subscription-Region"] = region

    def translate(self, texts, src):
        params = {"api-version": "3.0", "to": "de", "textType": "html"}
        if src in AZURE_SRC:
            params["from"] = src
        body = [{"Text": t} for t in texts]
        for attempt in range(6):
            try:
                r = requests.post(self.url, params=params, headers=self.headers, json=body, timeout=60)
            except requests.RequestException as e:
                raise Unsupported(f"Azure nicht erreichbar ({e.__class__.__name__})")
            if r.status_code == 401:
                raise Unsupported("Azure-Key oder Region ungültig")
            if r.status_code == 403:  # Freikontingent aufgebraucht
                raise QuotaExceeded(f"Azure HTTP 403: {r.text[:200]}")
            if r.status_code == 429:
                time.sleep(min(60, 5 * 2 ** attempt))
                continue
            r.raise_for_status()
            return [item["translations"][0]["text"] for item in r.json()]
        raise Unsupported("Azure: zu viele Anfragen (429)")


class Argos:
    name = "Argos"

    def __init__(self):
        self._tr = None
        self._pkg = None
        self._index_loaded = False
        self._routes = {}

    def _load(self):
        if self._tr is None:
            import argostranslate.package as pkg
            import argostranslate.translate as tr
            self._pkg, self._tr = pkg, tr

    def _ensure(self, a, b):
        """Sprachpaket a->b installieren, falls nötig. True wenn verfügbar."""
        if any(p.from_code == a and p.to_code == b for p in self._pkg.get_installed_packages()):
            return True
        if not self._index_loaded:
            self._pkg.update_package_index()
            self._index_loaded = True
        for p in self._pkg.get_available_packages():
            if p.from_code == a and p.to_code == b:
                log.info("  Argos: lade Sprachpaket %s->%s herunter ...", a, b)
                self._pkg.install_from_path(p.download())
                return True
        return False

    def _route(self, src):
        if src in self._routes:
            return self._routes[src]
        self._load()
        if self._ensure(src, "de"):
            route = [(src, "de")]
        elif src != "en" and self._ensure(src, "en") and self._ensure("en", "de"):
            route = [(src, "en"), ("en", "de")]
        else:
            raise Unsupported(f"Argos hat kein Sprachpaket für {src}")
        self._routes[src] = route
        return route

    def translate(self, texts, src):
        if not src:
            raise Unsupported("Argos braucht eine bekannte Quellsprache")
        route = self._route(src)
        out = []
        for t in texts:
            t = re.sub(r"</?[a-zA-Z][^>]*>", "", t)  # Tags kann Argos nicht
            lines = []
            for line in t.split("\n"):
                # Notensymbole vor Argos entfernen (fix_notes setzt sie danach wieder)
                core = re.sub(f"[{NOTES}]", "", line)
                core = re.sub(r"\s+", " ", core).strip()
                if has_letters(core):
                    for a, b in route:
                        core = self._tr.translate(core, a, b)
                lines.append(core)
            out.append("\n".join(l for l in lines if l))
        return out


def translate_all(texts, backends, src):
    """Ganze Datei mit einem Backend; geht das nicht, komplett mit dem nächsten."""
    reasons = []
    for b in backends:
        try:
            if hasattr(b, "start_file"):
                b.start_file(len(texts))
            out = []
            if getattr(b, "whole_file", False):
                out = b.translate(texts, src)
            else:
                for i in range(0, len(texts), BATCH):
                    out += b.translate(texts[i:i + BATCH], src)
            chars = sum(len(re.sub(r"<[^>]+>", "", t)) for t in texts)
            log.info("  übersetzt mit %s (%d Zeichen)", b.name, chars)
            return out
        except OllamaDown as e:
            if b.within_grace():
                # kurzer Ausfall: lieber später nochmal, statt Azure/DeepL-Kontingent zu verbrauchen
                raise Postpone(str(e))
            log.warning("  %s seit über %d min nicht erreichbar, weiche auf nächsten Übersetzer aus",
                        b.name, b.grace // 60)
            reasons.append(f"{b.name}: {e}")
        except (QuotaExceeded, Unsupported) as e:
            log.warning("  %s nicht nutzbar (%s)", b.name, e)
            reasons.append(f"{b.name}: {e}")
    raise NoBackend("; ".join(reasons) or "kein Übersetzer aktiv")


# --------------------------------------------------------------------------
# Sprache erkennen, Untertitel finden / extrahieren
# --------------------------------------------------------------------------
def lang_of_stream(s):
    tags = s.get("tags") or {}
    code = LANG.get(tags.get("language", "").lower())
    if code:
        return code
    for word in re.findall(r"[a-zäöü]+", tags.get("title", "").lower()):
        if word in LANG and len(word) > 3:  # nur ausgeschriebene Namen im Titel
            return LANG[word]
    return None


def is_forced(s):
    title = ((s.get("tags") or {}).get("title", "")).lower()
    return bool((s.get("disposition") or {}).get("forced")) or "forced" in title


def special_rank(s):
    """Kommentar-Spuren (2) und reine Schilder/Lieder-Spuren (1) nur nehmen, wenn nichts anderes da ist."""
    title = ((s.get("tags") or {}).get("title", "")).lower()
    if re.search(r"comment|kommentar|director|regie", title):
        return 2
    if re.search(r"\bsigns?\b|\bsongs?\b|lyrics|schilder", title):
        return 1
    return 0


def is_sdh(s):
    title = ((s.get("tags") or {}).get("title", "")).lower()
    return bool((s.get("disposition") or {}).get("hearing_impaired")) or "sdh" in title


def external_srt(video):
    """Untertiteldatei mit Sprachkürzel neben dem Video (nicht Deutsch, nicht forced).
    Film.srt ohne Kürzel wird bewusst ignoriert, die Sprache ist dort unklar."""
    found = []
    for p in video.parent.glob(glob_escape(video.stem) + ".*.srt"):
        tokens = p.name[len(video.stem) + 1:-4].lower().split(".")
        code = LANG.get(tokens[0])
        if not code or code == "de" or "forced" in tokens:
            continue
        found.append((code != "en", p.name, p, code))
    if found:
        found.sort()
        return found[0][2], found[0][3]
    return None, None


def glob_escape(s):
    return re.sub(r"([\[\]*?])", r"[\1]", s)


def probe_subs(video):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "s",
         "-show_entries",
         "stream=index,codec_name:stream_tags=language,title:stream_disposition=forced,hearing_impaired",
         "-of", "json", str(video)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=True, creationflags=NO_WINDOW,
    ).stdout
    if not out:
        raise RuntimeError(f"ffprobe lieferte keine Ausgabe für {video.name}")
    return json.loads(out).get("streams", [])


def pick_track(streams, prefer_sdh=False):
    """Gibt (stream, sprache, None) oder (None, None, Grund) zurück."""
    normal = [s for s in streams if not is_forced(s)]
    if any(lang_of_stream(s) == "de" for s in normal):
        return None, None, GERMAN_REASON
    text = [s for s in normal if s.get("codec_name") in TEXT_CODECS]
    if not text:
        # Bei fremdsprachigen Filmen ist die einzige (vollständige) Untertitelspur oft als
        # "forced" markiert. Dann nehmen wir sie, sofern es keine normale Textspur gibt.
        forced_text = [s for s in streams if is_forced(s) and s.get("codec_name") in TEXT_CODECS
                       and lang_of_stream(s) != "de"]
        if forced_text:
            text = forced_text
            log.info("  nur als 'forced' markierte Textspur vorhanden, verwende diese")
        elif streams:
            codecs = ", ".join(sorted({s.get("codec_name", "?") for s in streams}))
            if any(s.get("codec_name") not in TEXT_CODECS for s in normal):
                return None, None, f"nur Bild-Untertitel ({codecs}), OCR nötig"
            return None, None, f"nur 'forced'-Spuren mit Bild-Untertiteln ({codecs})"
        else:
            return None, None, "keine Untertitelspur im Film"

    def rank(s):
        lang = lang_of_stream(s)
        lang_rank = 0 if lang == "en" else (1 if lang else 2)  # Englisch, andere, unbekannt
        sdh_rank = (not is_sdh(s)) if prefer_sdh else is_sdh(s)
        return special_rank(s), lang_rank, sdh_rank, s["index"]

    best = sorted(text, key=rank)[0]
    return best, lang_of_stream(best), None


# --------------------------------------------------------------------------
# OCR für Bild-Untertitel (Blu-ray PGS) mit Tesseract
# --------------------------------------------------------------------------
# Nur Schriften, die Tesseract zuverlässig erkennt (keine CJK)
TESS = {"en": "eng", "de": "deu", "fr": "fra", "es": "spa", "it": "ita", "pt": "por", "nl": "nld",
        "sv": "swe", "da": "dan", "nb": "nor", "fi": "fin", "pl": "pol", "cs": "ces", "hu": "hun",
        "ro": "ron", "bg": "bul", "el": "ell", "ru": "rus", "uk": "ukr", "tr": "tur", "id": "ind",
        "vi": "vie"}
PGS_CODECS = {"hdmv_pgs_subtitle"}


def _u16(b, o):
    return (b[o] << 8) | b[o + 1]


def _rle_decode(data, w, h):
    """PGS-RLE in Bytes mit Palettenindizes (w*h) dekodieren."""
    out = bytearray()
    i, n, line = 0, len(data), 0
    while i < n:
        b = data[i]
        i += 1
        if b:
            out.append(b)
            line += 1
            continue
        if i >= n:
            break
        f = data[i]
        i += 1
        if f == 0:  # Zeilenende: auf volle Breite auffüllen
            if line < w:
                out += bytes(w - line)
            line = 0
            continue
        length = f & 0x3F
        if f & 0x40:
            length = (length << 8) | data[i]
            i += 1
        color = 0
        if f & 0x80:
            color = data[i]
            i += 1
        out += bytes((color,)) * length
        line += length
    need = w * h
    return bytes(out[:need]) + bytes(max(0, need - len(out)))


def parse_pgs(path):
    """Liest eine .sup-Datei. Gibt Liste (start_ms, end_ms, PIL-Bild schwarz auf weiß) zurück."""
    from PIL import Image

    data = Path(path).read_bytes()
    pos, n = 0, len(data)
    palettes, objects, obj_buf = {}, {}, {}
    pcs = None
    events = []  # (pts_ms, bild oder None)

    def render(p):
        pal = palettes.get(p["palette_id"], {})
        # Text = hell und deckend; Umrandung (dunkel) und Hintergrund werden weiß
        table = bytes(0 if (e := pal.get(i)) and e[1] >= 128 and e[0] >= 100 else 255 for i in range(256))
        parts = []
        for ob in p["objects"]:
            o = objects.get(ob["id"])
            if not o:
                continue
            w, h, raw = o
            img = Image.frombytes("L", (w, h), _rle_decode(raw, w, h).translate(table))
            x, y = ob["x"], ob["y"]
            if ob.get("crop"):
                cx, cy, cw, ch = ob["crop"]
                img = img.crop((cx, cy, cx + cw, cy + ch))
            parts.append((x, y, img))
        if not parts:
            return None
        x0 = min(x for x, _, _ in parts)
        y0 = min(y for _, y, _ in parts)
        x1 = max(x + im.width for x, _, im in parts)
        y1 = max(y + im.height for _, y, im in parts)
        pad = 20
        canvas = Image.new("L", (x1 - x0 + 2 * pad, y1 - y0 + 2 * pad), 255)
        for x, y, im in parts:
            canvas.paste(im, (x - x0 + pad, y - y0 + pad))
        if canvas.getextrema()[0] == 255:  # nichts Lesbares
            return None
        if canvas.height < 60:  # sehr kleine Schrift vergrößern
            canvas = canvas.resize((canvas.width * 2, canvas.height * 2))
        return canvas

    while pos + 13 <= n:
        if data[pos:pos + 2] != b"PG":
            pos += 1  # resynchronisieren
            continue
        pts = int.from_bytes(data[pos + 2:pos + 6], "big") // 90
        typ = data[pos + 10]
        size = _u16(data, pos + 11)
        seg = data[pos + 13:pos + 13 + size]
        pos += 13 + size
        if typ == 0x16:  # PCS: Bildaufbau
            state = seg[7]
            if state & 0x80:  # Epochenbeginn: alles neu
                objects.clear()
                palettes.clear()
            objs, o = [], 11
            for _ in range(seg[10]):
                ob = {"id": _u16(seg, o), "x": _u16(seg, o + 4), "y": _u16(seg, o + 6)}
                cropped = seg[o + 3] & 0x40
                o += 8
                if cropped:
                    ob["crop"] = (_u16(seg, o), _u16(seg, o + 2), _u16(seg, o + 4), _u16(seg, o + 6))
                    o += 8
                objs.append(ob)
            pcs = {"pts": pts, "palette_id": seg[9], "objects": objs}
        elif typ == 0x14:  # PDS: Palette (Index -> Y, Cr, Cb, Alpha)
            pal = palettes.setdefault(seg[0], {})
            for o in range(2, len(seg) - 4, 5):
                pal[seg[o]] = (seg[o + 1], seg[o + 4])  # (Y, Alpha)
        elif typ == 0x15:  # ODS: Bildobjekt (evtl. in mehreren Teilen)
            oid, flag = _u16(seg, 0), seg[3]
            if flag & 0x80:  # erster Teil
                w, h = _u16(seg, 7), _u16(seg, 9)
                obj_buf[oid] = [w, h, bytearray(seg[11:])]
            elif oid in obj_buf:
                obj_buf[oid][2] += seg[4:]
            if flag & 0x40 and oid in obj_buf:  # letzter Teil
                w, h, raw = obj_buf.pop(oid)
                objects[oid] = (w, h, bytes(raw))
        elif typ == 0x80 and pcs is not None:  # END: Anzeigeeinheit abschließen
            events.append((pcs["pts"], render(pcs) if pcs["objects"] else None))
            pcs = None

    result = []
    for k, (t, img) in enumerate(events):
        if img is None:
            continue
        end = events[k + 1][0] if k + 1 < len(events) else t + 4000
        if end > t:
            result.append((t, end, img))
    return result


class OCR:
    def __init__(self, exe, german=False):
        self.exe = exe
        self.german = german
        try:
            out = subprocess.run([exe, "--list-langs"], capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=30,
                                 creationflags=NO_WINDOW)
            self.langs = set((out.stdout + out.stderr).split()[1:]) if out.returncode == 0 else set()
        except Exception:
            self.langs = set()
        self.langs = {l for l in self.langs if re.fullmatch(r"[a-z_]{3,8}", l) and l not in ("osd", "equ")}

    def supports(self, lang):
        return TESS.get(lang) in self.langs

    def _tesseract(self, files, lang3):
        """Mehrere Bilder in einem Tesseract-Aufruf; Seiten sind durch \\f getrennt."""
        lst = files[0].parent / f"list_{files[0].stem}.txt"
        lst.write_text("\n".join(str(f) for f in files) + "\n", encoding="utf-8")
        env = dict(os.environ, OMP_THREAD_LIMIT="1")  # Parallelität machen wir selbst
        out = subprocess.run([self.exe, str(lst), "stdout", "-l", lang3, "--psm", "6"],
                             capture_output=True, env=env, timeout=3600, creationflags=NO_WINDOW)
        if out.returncode != 0:
            raise RuntimeError(f"Tesseract-Fehler: {out.stderr.decode('utf-8', 'replace')[:300]}")
        pages = out.stdout.decode("utf-8", "replace").split("\f")
        if len(pages) < len(files):
            raise RuntimeError(f"Tesseract lieferte {len(pages)} statt {len(files)} Seiten")
        return pages[:len(files)]

    @staticmethod
    def _cleanup(text):
        lines = []
        for line in text.splitlines():
            line = re.sub(r"\s+", " ", line).strip()
            line = line.replace("|", "I")
            if line:
                lines.append(line)
        return "\n".join(lines)

    def run(self, video, stream, lang, tmp):
        """Bild-Untertitelspur extrahieren und erkennen. Gibt srt-Untertitel zurück."""
        from datetime import timedelta
        tmp = Path(tmp)
        sup = tmp / "src.sup"
        log_extract(video)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(video), "-map", f"0:{stream['index']}",
                        "-c", "copy", str(sup)], check=True, creationflags=NO_WINDOW)
        t0 = time.time()
        caps = parse_pgs(sup)
        if not caps:
            return []
        files = []
        for k, (_, _, img) in enumerate(caps):
            f = tmp / f"ocr_{k:05d}.png"
            img.save(f)
            files.append(f)
        workers = max(1, min(6, (os.cpu_count() or 2) // 2))
        size = (len(files) + workers - 1) // workers
        chunks = [files[i:i + size] for i in range(0, len(files), size)]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            texts = [t for part in pool.map(lambda c: self._tesseract(c, TESS[lang]), chunks) for t in part]
        subs = []
        for (start, end, _), text in zip(caps, texts):
            text = self._cleanup(text)
            if not text or not has_letters(text):
                continue
            if subs and subs[-1].content == text and subs[-1].end >= timedelta(milliseconds=start - 50):
                subs[-1].end = timedelta(milliseconds=end)  # nur Palettenwechsel o. Ä.: verlängern
                continue
            subs.append(srt.Subtitle(len(subs) + 1, timedelta(milliseconds=start),
                                     timedelta(milliseconds=end), text))
        log.info("  OCR: %d Untertitel aus %d Bildern erkannt (%s, %.0f s)",
                 len(subs), len(caps), TESS[lang], time.time() - t0)
        return subs


def find_tesseract(explicit=None):
    if explicit:
        return str(explicit) if Path(explicit).is_file() else None
    exe = shutil.which("tesseract")
    if exe:
        return exe
    for p in (r"C:\Program Files\Tesseract-OCR\tesseract.exe",
              r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"):
        if Path(p).is_file():
            return p
    return None


def pick_image_track(streams, ocr, prefer_sdh=False, want_lang=None):
    """Beste PGS-Spur für OCR: Englisch bevorzugt, sonst andere Sprache mit Tesseract-Paket."""
    cands = []
    for s in streams:
        if s.get("codec_name") not in PGS_CODECS or is_forced(s):
            continue
        lang = lang_of_stream(s)
        if want_lang and lang != want_lang:
            continue
        if not want_lang and lang == "de":
            continue
        if lang and ocr.supports(lang):
            cands.append(s)
    if not cands:
        return None, None

    def rank(s):
        sdh_rank = (not is_sdh(s)) if prefer_sdh else is_sdh(s)
        return (0 if lang_of_stream(s) == "en" else 1), sdh_rank, s["index"]

    best = sorted(cands, key=rank)[0]
    return best, lang_of_stream(best)


def read_text_guess(path):
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", errors="replace")


def clean(text):
    text = re.sub(r"\{\\[^}]*\}", "", text)  # ASS-Overrides wie {\an8}
    return text.replace("\\N", "\n").strip()


def has_letters(text):
    """False bei Zeilen ohne Buchstaben (nur ♪, Punkte, Zahlen): die werden nicht übersetzt."""
    return re.search(r"[^\W\d_]", text) is not None


def protect_notes(text):
    """Notensymbole für DeepL als 'nicht übersetzen' markieren."""
    return re.sub(f"[{NOTES}]+", lambda m: f'<span class="notranslate">{m.group(0)}</span>', text)


def fix_notes(src, dst):
    """Schutz-Tags entfernen; leere Ergebnisse durch das Original ersetzen;
    verlorene Notensymbole am Zeilenanfang/-ende wieder ergänzen."""
    dst = re.sub(r"</?span[^>]*>", "", dst).strip()
    if not dst:
        return src
    count = lambda s: sum(s.count(n) for n in NOTES)
    if count(dst) >= count(src):
        return dst  # Notensymbole sind erhalten geblieben
    plain_src = re.sub(r"<[^>]+>", "", src).strip()
    plain_dst = re.sub(r"<[^>]+>", "", dst).strip()
    if plain_src.startswith(tuple(NOTES)) and not plain_dst.startswith(tuple(NOTES)):
        dst = "♪ " + dst
    if plain_src.endswith(tuple(NOTES)) and not plain_dst.endswith(tuple(NOTES)):
        dst = dst + " ♪"
    return dst


# --------------------------------------------------------------------------
# Verarbeitung
# --------------------------------------------------------------------------
def log_extract(video):
    """Hinweis vor dem Auslesen: ffmpeg muss die ganze Datei lesen, das dauert bei großen Dateien."""
    gb = video.stat().st_size / 1024 ** 3
    if gb >= 2:
        net = str(video).startswith(("\\\\", "//"))
        log.info("  lese Untertitelspur aus %.1f GB%s, das dauert ca. %d-%d min ...", gb,
                 " über das Netzwerk" if net else "", max(1, int(gb * 1024 / 110 / 60)),
                 max(2, int(gb * 1024 / 50 / 60) + 1))


def compare(video, prefer_sdh=False, ocr=None):
    """Stellt Original und .de.srt Einblendung für Einblendung gegenüber (zur Kontrolle)."""
    de_path = video.with_name(video.stem + ".de.srt")
    if not de_path.exists():
        raise SystemExit(f"Keine deutsche Untertiteldatei gefunden: {de_path}")
    de = list(srt.parse(read_text_guess(de_path)))
    with tempfile.TemporaryDirectory() as tmp:
        streams = probe_subs(video)
        track, lang, reason = pick_track(streams, prefer_sdh)
        src, _ = (None, None) if track is not None or reason == GERMAN_REASON else external_srt(video)
        if src:
            what = f"externe Datei {src.name}"
            orig = list(srt.parse(read_text_guess(src)))
        else:
            if track is not None:
                what = f"Spur {track['index']} ({lang or 'unbekannt'})"
                p = Path(tmp) / "src.srt"
                subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(video), "-map", f"0:{track['index']}",
                                "-c:s", "srt", str(p)], check=True, creationflags=NO_WINDOW)
                orig = list(srt.parse(read_text_guess(p)))
            elif ocr and reason.startswith("nur Bild-Untertitel"):
                img, lang = pick_image_track(streams, ocr, prefer_sdh)
                what = f"Bild-Spur {img['index']} per OCR"
                orig = ocr.run(video, img, lang, tmp)
            else:
                raise SystemExit(f"Keine Originalspur zum Vergleichen: {reason}")
        all_tracks = [f"{s_['index']}: {lang_of_stream(s_) or '?'} {s_.get('codec_name')}"
                      f"{' SDH' if is_sdh(s_) else ''}{' forced' if is_forced(s_) else ''}"
                      for s_ in streams]

    def ts(t):
        t = int(t.total_seconds() * 1000)
        return f"{t // 3600000:02d}:{t // 60000 % 60:02d}:{t // 1000 % 60:02d},{t % 1000:03d}"

    lines, bad_time = [], 0
    for i in range(max(len(orig), len(de))):
        o = orig[i] if i < len(orig) else None
        d = de[i] if i < len(de) else None
        same = o and d and o.start == d.start and o.end == d.end
        bad_time += 0 if same else 1
        lines.append(f"#{i + 1:<5} {ts(o.start) if o else '-':>12} {'' if same else '  <<< ZEIT ABWEICHEND ' + (ts(d.start) if d else '-')}")
        lines.append(f"   ORIG: {(o.content if o else '-').replace(chr(10), ' / ')}")
        lines.append(f"   DE:   {(d.content if d else '-').replace(chr(10), ' / ')}")
    head = [f"Vergleich für {video.name}",
            f"Quelle der Übersetzung (heute ausgewählt): {what}",
            f"Untertitelspuren im Film: {', '.join(all_tracks) or '-'}",
            f"Einblendungen Original: {len(orig)}, Deutsch: {len(de)}, abweichende Zeiten: {bad_time}",
            "Hinweis: Stimmen alle Zeiten, aber der deutsche Text passt ab einer Stelle zur Nachbarzeile,",
            "wurde Text verschoben -> .de.srt löschen und neu übersetzen lassen.", ""]
    out = Path(__file__).resolve().parent / f"{video.stem}.vergleich.txt"
    out.write_text("\n".join(head + lines) + "\n", encoding="utf-8-sig")
    log.info("Vergleich geschrieben: %s (%d Einblendungen, %d mit abweichender Zeit)", out, len(orig), bad_time)


def write_srt(target, subs):
    tmp_out = target.with_name(target.name + ".tmp")
    tmp_out.write_text(srt.compose(subs), encoding="utf-8")
    os.replace(tmp_out, target)


def process(video, backends, prefer_sdh=False, ocr=None):
    """None bei Erfolg, sonst (Grund, Stunden bis zum neuen Versuch oder None für nie)."""
    target = video.with_name(video.stem + ".de.srt")
    log.info("Verarbeite %s", video.name)

    with tempfile.TemporaryDirectory() as tmp:
        subs = None
        try:
            streams = probe_subs(video)
        except subprocess.CalledProcessError as e:
            if not external_srt(video)[0]:
                raise
            log.warning("  ffprobe kann die Datei nicht lesen (%s), nutze die Untertiteldatei daneben", e.returncode)
            streams = []
        track, lang, reason = pick_track(streams, prefer_sdh)
        # Externe Datei (Film.en.srt) nur, wenn der Film keine eigene Textspur hat: das Timing
        # einer eingebetteten Spur passt sicher zum Film, das einer heruntergeladenen nicht immer.
        src, ext_lang = (None, None) if track is not None or reason == GERMAN_REASON else external_srt(video)
        if src:
            lang = ext_lang
            log.info("  nutze externe Datei %s (Sprache: %s)", src.name, lang)
        else:
            if track is None:
                german_text = any(lang_of_stream(s) == "de" and not is_forced(s)
                                  and s.get("codec_name") in TEXT_CODECS for s in streams)
                if ocr and ocr.german and reason == GERMAN_REASON and not german_text:
                    # Deutsche Bild-Untertitel -> per OCR in Text umwandeln (kein Transcoding in Jellyfin)
                    img, _ = pick_image_track(streams, ocr, prefer_sdh, want_lang="de")
                    if img is None:
                        return reason, None
                    log.info("  deutsche Bild-Untertitel (Spur %s), wandle per OCR in Text um", img["index"])
                    de_subs = ocr.run(video, img, "de", tmp)
                    if not de_subs:
                        return f"{GERMAN_REASON} (OCR ohne Ergebnis)", None
                    write_srt(target, de_subs)
                    log.info("  fertig: %s (OCR, ohne Übersetzung)", target.name)
                    return None
                if ocr and reason.startswith("nur Bild-Untertitel"):
                    img, lang = pick_image_track(streams, ocr, prefer_sdh)
                    if img is None:
                        return (f"{reason}; keine Spur in einer Sprache, für die OCR möglich ist "
                                f"(installierte Tesseract-Sprachen: {', '.join(sorted(ocr.langs)) or 'keine'})"), None
                    log.info("  nur Bild-Untertitel, OCR auf Spur %s (Sprache: %s)", img["index"], lang)
                    subs = ocr.run(video, img, lang, tmp)
                    if not subs:
                        return "OCR hat keinen Text erkannt", None
                else:
                    return reason, None
            else:
                src = Path(tmp) / "src.srt"
                log_extract(video)
                subprocess.run(
                    ["ffmpeg", "-y", "-v", "error", "-i", str(video),
                     "-map", f"0:{track['index']}", "-c:s", "srt", str(src)],
                    check=True, creationflags=NO_WINDOW,
                )
                log.info("  Spur %s extrahiert (Sprache: %s)", track["index"], lang or "unbekannt")

        if subs is None:
            subs = list(srt.parse(read_text_guess(src)))
        if not subs:
            return "Untertitel leer", None

        cleaned = [clean(s.content) for s in subs]
        for s, t in zip(subs, cleaned):
            s.content = t  # Zeilen ohne Buchstaben (z. B. nur ♪) bleiben unverändert
        idx = [i for i, t in enumerate(cleaned) if t and has_letters(t)]
        try:
            translated = translate_all([protect_notes(cleaned[i]) for i in idx], backends, lang)
        except NoBackend as e:
            return f"keine Übersetzung möglich ({e})", RETRY_HOURS
        except Postpone as e:
            return f"zurückgestellt: {e}", POSTPONE_MIN / 60
        for i, t in zip(idx, translated):
            subs[i].content = fix_notes(cleaned[i], t)

        write_srt(target, subs)
        log.info("  fertig: %s", target.name)
    return None


LOCK_STALE_HOURS = 3

# Größe/Änderungszeit jeder Datei beim ersten Sehen; erst wenn beides min_age lang gleich bleibt,
# gilt die Datei als fertig. Nötig, weil Explorer beim Kopieren oft das ALTE Änderungsdatum setzt
# und die Altersprüfung allein dann einen halb kopierten Film durchlassen würde.
_SEEN = {}
# Fehlversuche je Datei in diesem Lauf (nach MAX_FAILS als dauerhaft fehlerhaft merken)
_FAILS = {}
MAX_FAILS = 3


def is_stable(video, st, min_age):
    sig = (st.st_size, st.st_mtime)
    old = _SEEN.get(video)
    now = time.time()
    if old is None or old[0] != sig:
        _SEEN[video] = (sig, now)
        return min_age <= 0
    return now - old[1] >= min_age


def take_lock(lock):
    """Sperrdatei anlegen, damit zwei gleichzeitige Läufe nicht denselben Film bearbeiten.
    Alte Sperren (z. B. nach Absturz) verfallen nach LOCK_STALE_HOURS."""
    try:
        if lock.exists() and time.time() - lock.stat().st_mtime > LOCK_STALE_HOURS * 3600:
            lock.unlink(missing_ok=True)
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False


POSTPONE_MIN = 10


def fullscreen_reason():
    """Windows: läuft gerade ein Spiel/Programm im Vollbild? (SHQueryUserNotificationState)"""
    if os.name != "nt":
        return None
    try:
        import ctypes
        state = ctypes.c_int(0)
        if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state)) != 0:
            return None
        return {2: "Vollbildanwendung aktiv", 3: "Spiel im Vollbild (Direct3D)",
                4: "Präsentationsmodus"}.get(state.value)
    except Exception:
        return None


def gpu_busy_reason(threshold):
    """Grafikkarte stark ausgelastet (z. B. Spiel im randlosen Fenster)? Misst 3 s lang per nvidia-smi."""
    exe = shutil.which("nvidia-smi")
    if not exe or threshold <= 0:
        return None
    values = []
    for _ in range(3):
        try:
            out = subprocess.run([exe, "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10, creationflags=NO_WINDOW).stdout
            values.append(max(int(x) for x in out.split()))
        except Exception:
            return None
        time.sleep(1)
    avg = sum(values) / len(values)
    return f"Grafikkarte zu {avg:.0f} % ausgelastet" if avg >= threshold else None


class Gate:
    """Hält die Verarbeitung an, solange gespielt wird oder Ollama kurz nicht erreichbar ist."""

    def __init__(self, ollama, game_pause=True, gpu_threshold=40, check_every=60):
        self.ollama = ollama
        self.game_pause = game_pause
        self.gpu_threshold = gpu_threshold
        self.check_every = check_every

    def reasons(self):
        found = []
        if self.game_pause:
            r = fullscreen_reason() or gpu_busy_reason(self.gpu_threshold)
            if r:
                found.append(("game", r))
        if self.ollama and not self.ollama.available() and self.ollama.within_grace():
            found.append(("ollama", "Ollama nicht erreichbar"))
        return found

    def wait(self):
        last = None
        while True:
            found = self.reasons()
            if not found:
                if last:
                    log.info("Pause beendet, es geht weiter.")
                return
            if [r for _, r in found] != [r for _, r in (last or [])]:
                text = ", ".join(r for _, r in found)
                log.info("Pause: %s. Prüfe alle %d s erneut.", text, self.check_every)
                if any(k == "game" for k, _ in found) and self.ollama:
                    self.ollama.unload()  # Grafikspeicher fürs Spiel freigeben
            last = found
            time.sleep(self.check_every)


GERMAN_REASON = "Film hat schon deutsche Untertitel eingebaut"


class Mover:
    """Verschiebt fertige Filme samt Untertiteln in den Zielordner (z. B. Netzlaufwerk).
    Sicher bei Abbrüchen: erst alles als .part kopieren, dann umbenennen, dann Quelle löschen."""

    SKIP_SUFFIXES = (".lock", ".tmp", ".part")

    def __init__(self, dest, root, move_skipped=False):
        self.dest = Path(dest)
        self.root = Path(root)
        self.move_skipped = move_skipped
        self._warned_unreachable = False
        self._warned_clash = set()

    def reachable(self):
        try:
            ok = self.dest.is_dir()
        except OSError:
            ok = False
        if not ok and not self._warned_unreachable:
            log.warning("Zielordner %s nicht erreichbar, fertige Filme bleiben vorerst liegen "
                        "(wird bei jeder Prüfung erneut versucht).", self.dest)
            self._warned_unreachable = True
        elif ok and self._warned_unreachable:
            log.info("Zielordner %s wieder erreichbar.", self.dest)
            self._warned_unreachable = False
        return ok

    def _files_for(self, video):
        """Eigener Unterordner mit nur diesem einen Video -> ganzen Ordner mitnehmen.
        Sonst nur Dateien mit gleichem Namensanfang (Film.mkv, Film.de.srt, Film.en.srt, Film.nfo ...)."""
        folder = video.parent
        if folder != self.root:
            videos = [p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in VIDEO_EXT]
            if videos == [video]:
                return [p for p in folder.rglob("*") if p.is_file()
                        and not p.name.endswith(self.SKIP_SUFFIXES)]
        prefix = video.stem + "."
        return [p for p in folder.iterdir() if p.is_file() and p.name.startswith(prefix)
                and not p.name.endswith(self.SKIP_SUFFIXES)
                and (p == video or p.suffix.lower() not in VIDEO_EXT)]

    def move(self, video, lock=None, note=""):
        if not self.reachable():
            return False
        files = self._files_for(video)
        pairs = [(f, self.dest / f.relative_to(self.root)) for f in files]
        clash = [d for _, d in pairs if d.exists()]
        if clash:
            if video not in self._warned_clash:  # nur einmal melden, nicht bei jeder Prüfung
                log.warning("Nicht verschoben: %s existiert im Ziel schon (bitte von Hand klären)", clash[0])
                self._warned_clash.add(video)
            return False
        log.info("Verschiebe %s%s", video.name, f" ({note})" if note else "")
        t0 = time.time()
        before = {f: (f.stat().st_size, f.stat().st_mtime) for f in files}
        total = sum(sz for sz, _ in before.values())
        parts = []
        try:
            # 1) alles als .part ins Ziel kopieren und Größe prüfen
            for src, dst in sorted(pairs, key=lambda x: x[0] == video):  # Video zuletzt
                dst.parent.mkdir(parents=True, exist_ok=True)
                part = dst.with_name(dst.name + ".part")
                parts.append(part)
                shutil.copy2(src, part)
                if part.stat().st_size != src.stat().st_size:
                    raise OSError(f"Größe stimmt nach dem Kopieren nicht: {src.name}")
            # Quelle während des Kopierens verändert (z. B. noch im Download)? Dann abbrechen.
            for f, sig in before.items():
                if (f.stat().st_size, f.stat().st_mtime) != sig:
                    raise OSError(f"{f.name} hat sich während des Kopierens verändert")
            # 2) .part -> endgültiger Name (Video zuletzt, damit Jellyfin es erst komplett sieht)
            for (src, dst), part in zip(sorted(pairs, key=lambda x: x[0] == video), parts):
                os.replace(part, dst)
        except Exception as e:
            log.error("  Verschieben fehlgeschlagen (%s), Film bleibt im Quellordner", e)
            for part in parts:
                try:
                    part.unlink(missing_ok=True)
                except OSError:
                    pass
            return False
        # 3) Quelle löschen und leere Ordner aufräumen
        for src, _ in pairs:
            try:
                src.unlink()
            except OSError as e:
                log.warning("  konnte %s nicht löschen: %s", src.name, e)
        if lock:
            lock.unlink(missing_ok=True)
        folder = video.parent
        while folder != self.root and self.root in folder.parents:
            try:
                folder.rmdir()  # klappt nur, wenn leer
            except OSError:
                break
            folder = folder.parent
        mb = total / 1024 / 1024
        secs = max(time.time() - t0, 0.1)
        log.info("  verschoben nach %s (%.0f MB, %.0f s, %.0f MB/s)",
                 self.dest / video.relative_to(self.root), mb, secs, mb / secs)
        return True

    def wants(self, reason, retry_hours):
        """Soll ein übersprungener Film trotzdem verschoben werden?"""
        if reason.startswith(GERMAN_REASON):
            return True
        return self.move_skipped and retry_hours is None and not reason.startswith("zurückgestellt")


def scan(root, backends, state, min_age, min_size, prefer_sdh=False, gate=None, mover=None, ocr=None):
    changed = False
    explicit = root.is_file()  # einzelne Datei angegeben: Gründe fürs Überspringen melden

    def is_system(p):
        # Papierkorb, Systemordner usw. auf Laufwerkswurzeln auslassen
        return any(part.startswith("$") or part.lower() == "system volume information"
                   for part in p.relative_to(root).parts)

    candidates = [root] if explicit else sorted(p for p in root.rglob("*") if not is_system(p))
    for video in candidates:
        if video.suffix.lower() not in VIDEO_EXT or not video.is_file():
            if explicit:
                log.warning("Übersprungen: %s ist keine Videodatei (erlaubt: mkv, mp4, m4v)", video.name)
            continue
        if video.with_name(video.stem + ".de.srt").exists():
            if mover and is_stable(video, video.stat(), min_age):
                lock = video.with_name(video.stem + ".de.srt.lock")
                if take_lock(lock):
                    try:
                        mover.move(video, lock)
                    finally:
                        lock.unlink(missing_ok=True)
            elif explicit:
                log.warning("Übersprungen: %s existiert schon. Zum Neuübersetzen diese Datei löschen.",
                            video.stem + ".de.srt")
            continue
        st = video.stat()
        if st.st_size < min_size or (not explicit and not is_stable(video, st, min_age)) \
                or (explicit and time.time() - st.st_mtime < min_age):
            if explicit:
                log.warning("Übersprungen: Datei ist kleiner als --min-size-mb oder jünger als --min-age "
                            "(mit --min-age 0 --min-size-mb 0 erzwingen)")
            continue  # zu klein (Sample) oder noch im Download
        key = str(video)
        entry = state.get(key)
        # Untertiteldatei daneben (z. B. nachträglich abgelegte Film.en.srt) mit Änderungszeit
        side, _ = external_srt(video)
        side_mtime = side.stat().st_mtime if side else None
        reason_old = (entry or {}).get("reason", "")
        ocr_retry = bool(ocr) and (
            (reason_old.startswith("nur Bild-Untertitel") and not entry.get("ocr"))
            or (ocr.german and reason_old.startswith(GERMAN_REASON) and not entry.get("ocr_german")))
        if entry and entry.get("mtime") == st.st_mtime and not explicit and not ocr_retry \
                and entry.get("sidecar") == side_mtime:  # neue/geänderte .srt daneben -> neu versuchen
            retry_at = entry.get("retry_at")
            if retry_at is None or time.time() < retry_at:
                if mover and mover.wants(entry.get("reason", ""), None if retry_at is None else 1):
                    lock = video.with_name(video.stem + ".de.srt.lock")
                    if take_lock(lock):
                        try:
                            if mover.move(video, lock, entry.get("reason", "")):
                                del state[key]
                                changed = True
                        finally:
                            lock.unlink(missing_ok=True)
                continue  # schon als nicht machbar bekannt
        if gate and not explicit:
            gate.wait()  # wartet bei Spiel/Vollbild bzw. kurzem Ollama-Ausfall
            if video.with_name(video.stem + ".de.srt").exists():
                continue  # während des Wartens von einem anderen Lauf erledigt
        lock = video.with_name(video.stem + ".de.srt.lock")
        if not take_lock(lock):
            if explicit:
                log.warning("Übersprungen: wird gerade von einem anderen Lauf bearbeitet (%s)", lock.name)
            continue
        try:
            try:
                result = process(video, backends, prefer_sdh, ocr)
                _FAILS.pop(key, None)
            except Exception as e:
                n = _FAILS.get(key, 0) + 1
                _FAILS[key] = n
                if n < MAX_FAILS:
                    log.exception("Fehler bei %s (Versuch %d von %d, neuer Versuch im nächsten Durchlauf)",
                                  video.name, n, MAX_FAILS)
                    continue
                log.exception("Fehler bei %s, %d Versuche fehlgeschlagen, wird übersprungen", video.name, n)
                result = (f"wiederholter Fehler ({e.__class__.__name__}: {str(e)[:150]})", None)
                _FAILS.pop(key, None)
            if result:
                reason, retry_hours = result
                if reason.startswith("zurückgestellt"):
                    log.info("  %s (neuer Versuch in %d min)", reason, POSTPONE_MIN)
                else:
                    log.warning("  übersprungen: %s", reason)
                state[key] = {
                    "mtime": st.st_mtime,
                    "sidecar": side_mtime,
                    "ocr": bool(ocr),
                    "ocr_german": bool(ocr and ocr.german),
                    "reason": reason,
                    "retry_at": time.time() + retry_hours * 3600 if retry_hours else None,
                }
                changed = True
                if mover and mover.wants(reason, retry_hours) and mover.move(video, lock):
                    del state[key]
            else:
                if key in state:
                    del state[key]
                    changed = True
                if mover:
                    mover.move(video, lock)
        finally:
            lock.unlink(missing_ok=True)
    return changed


_instance_lock = None


def single_instance(path):
    """Verhindert eine zweite gleichzeitige Instanz im Dauerbetrieb. Die Sperre gibt das
    Betriebssystem automatisch frei, wenn der Prozess endet (auch bei Absturz)."""
    global _instance_lock
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return False
    _instance_lock = f  # offen halten, solange das Skript läuft
    return True


def read_secret(env_name, file_name):
    """Wert aus Umgebungsvariable oder aus einer Datei neben dem Skript (eine Zeile pro Wert)."""
    val = os.environ.get(env_name, "").strip()
    if val:
        return [val], f"Umgebungsvariable {env_name}"
    f = Path(__file__).resolve().with_name(file_name)
    if f.is_file():
        lines = [l.strip() for l in f.read_text(encoding="utf-8-sig").splitlines() if l.strip()]
        if lines:
            return lines, str(f)
    return None, None


def build_backends(spec, args):
    backends = []
    for name in [s.strip().lower() for s in spec.split(",") if s.strip()]:
        if name == "ollama":
            o = Ollama(args.ollama_model, args.ollama_url, args.ollama_parallel, args.ollama_grace_min,
                       args.ollama_keep_alive, args.ollama_batch)
            try:
                r = requests.get(args.ollama_url.rstrip("/") + "/api/tags", timeout=5)
                models = [m.get("name", "") for m in r.json().get("models", [])]
            except Exception:
                models = None
            # Nicht dauerhaft aussortieren: Ollama startet nach der Anmeldung evtl. später als das Skript.
            # Ist es beim Übersetzen nicht erreichbar, übernimmt der nächste Übersetzer.
            want = args.ollama_model if ":" in args.ollama_model else args.ollama_model + ":latest"
            if models is None:
                log.warning("Ollama gerade nicht erreichbar unter %s (wird bei jedem Film neu versucht).",
                            args.ollama_url)
            elif want not in models:
                log.warning("Ollama-Modell %s fehlt, einmal ausführen: ollama pull %s",
                            args.ollama_model, args.ollama_model)
            log.info("Übersetzer: %s, %d Anfragen gleichzeitig, %s", o.name, o.parallel,
                     "jede Einblendung einzeln" if o.batch == 1 else f"{o.batch} Zeilen pro Anfrage")
            backends.append(o)
        elif name == "azure":
            vals, source = read_secret("AZURE_TRANSLATOR_KEY", "azure_key.txt")
            if not vals:
                log.info("Kein Azure-Key gefunden (AZURE_TRANSLATOR_KEY oder azure_key.txt), wird übersprungen.")
                continue
            region = os.environ.get("AZURE_TRANSLATOR_REGION", "").strip() or (vals[1] if len(vals) > 1 else None)
            log.info("Übersetzer: Azure (Key aus %s, Region %s)", source, region or "global")
            backends.append(Azure(vals[0], region))
        elif name == "deepl":
            vals, source = read_secret("DEEPL_API_KEY", "deepl_key.txt")
            if not vals:
                log.info("Kein DeepL-Key gefunden (DEEPL_API_KEY oder deepl_key.txt), wird übersprungen.")
                continue
            log.info("Übersetzer: DeepL (Key aus %s)", source)
            backends.append(DeepL(vals[0]))
        elif name == "argos":
            log.info("Übersetzer: Argos (offline)")
            backends.append(Argos())
        else:
            log.warning("Unbekannter Übersetzer '%s' in --use, wird ignoriert.", name)
    return backends


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", type=Path, nargs="?", default=DEFAULT_FOLDER,
                    help=f"Ordner (mit Unterordnern) oder einzelne Videodatei (Standard: {DEFAULT_FOLDER})")
    ap.add_argument("--watch", action="store_true", help="dauerhaft laufen und regelmäßig prüfen")
    ap.add_argument("--interval", type=int, default=300, help="Sekunden zwischen Prüfungen (Standard 300)")
    ap.add_argument("--min-age", type=int, default=180, help="Datei muss so lange unverändert sein (Sekunden)")
    ap.add_argument("--min-size-mb", type=int, default=100, help="kleinere Videos (Samples) ignorieren")
    ap.add_argument("--use", default="ollama,azure,deepl,argos",
                    help="Übersetzer und Reihenfolge, kommagetrennt (Standard: ollama,azure,deepl,argos)")
    ap.add_argument("--ollama-model", default="gemma3:12b", help="Ollama-Modell (Standard: gemma3:12b)")
    ap.add_argument("--ollama-url", default="http://localhost:11434")
    ap.add_argument("--ollama-batch", type=int, default=1,
                    help="Zeilen pro Ollama-Anfrage (Standard 1 = jede Einblendung einzeln, kein Verrutschen "
                         "möglich; größere Werte sind schneller, aber mit Restrisiko)")
    ap.add_argument("--ollama-parallel", type=int, default=3,
                    help="gleichzeitige Anfragen an Ollama (Standard 3; OLLAMA_NUM_PARALLEL passend setzen)")
    ap.add_argument("--ollama-keep-alive", default="1m",
                    help="wie lange das Modell nach der letzten Anfrage geladen bleibt (Standard 1m, z. B. 30s, 5m)")
    ap.add_argument("--ollama-grace-min", type=int, default=60,
                    help="so lange (Minuten) auf Ollama warten, bevor auf Azure/DeepL/Argos ausgewichen wird "
                         "(Standard 60, 0 = sofort ausweichen)")
    ap.add_argument("--no-game-pause", action="store_true",
                    help="nicht pausieren, wenn ein Spiel/Vollbildprogramm läuft")
    ap.add_argument("--gpu-busy-percent", type=int, default=40,
                    help="ab dieser GPU-Auslastung (%%) als 'Spiel läuft' werten (Standard 40, 0 = nicht prüfen)")
    ap.add_argument("--argos-only", action="store_true", help="Kurzform für --use argos")
    ap.add_argument("--prefer-sdh", action="store_true",
                    help="SDH-Spur (mit ♪ und [Musik]-Hinweisen) bevorzugen")
    ap.add_argument("--no-ocr", action="store_true",
                    help="keine Texterkennung für Bild-Untertitel (PGS) verwenden")
    ap.add_argument("--ocr-german", action="store_true",
                    help="deutsche Bild-Untertitel per OCR in eine .de.srt umwandeln (vermeidet Transcoding)")
    ap.add_argument("--tesseract", type=Path, default=None,
                    help="Pfad zu tesseract.exe (Standard: automatisch suchen)")
    ap.add_argument("--compare", action="store_true",
                    help="nur prüfen: Original und vorhandene .de.srt gegenüberstellen (Datei *.vergleich.txt)")
    ap.add_argument("--move-to", type=Path, default=None,
                    help="fertige Filme samt Untertiteln hierhin verschieben (z. B. Y:/Filme)")
    ap.add_argument("--move-skipped", action="store_true",
                    help="auch Filme verschieben, die nicht übersetzt werden können (nur Bild-Untertitel, keine Spur)")
    ap.add_argument("--log-file", type=Path, default=None,
                    help="Log zusätzlich in diese Datei schreiben (praktisch für den Windows-Aufgabenplaner)")
    ap.add_argument("--state", type=Path,
                    default=Path.home() / ".cache" / "sub_translate_state_v2.json",
                    help="Merkliste übersprungener Filme")
    args = ap.parse_args()

    handlers = []
    if sys.stderr is not None:  # bei pythonw.exe (ohne Fenster) gibt es keine Konsole
        handlers.append(logging.StreamHandler())
    if args.log_file:
        args.log_file.parent.mkdir(parents=True, exist_ok=True)
        # Log wächst nicht endlos: max. 5 MB, danach log.txt.1 ... log.txt.3
        handlers.append(RotatingFileHandler(args.log_file, maxBytes=5_000_000, backupCount=3,
                                            encoding="utf-8"))
    if not handlers:
        handlers.append(logging.NullHandler())
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(message)s")
    if args.watch and not args.folder.exists():
        # Dauerbetrieb: auf den Ordner warten (z. B. externes Laufwerk noch nicht angeschlossen)
        log.warning("Ordner %s nicht vorhanden, warte darauf (Prüfung alle %d s) ...", args.folder, args.interval)
        while not args.folder.exists():
            time.sleep(args.interval)
        log.info("Ordner %s ist jetzt da.", args.folder)
    if not args.folder.exists():
        hint = ""
        parent = args.folder.parent
        if args.folder.suffix and parent.is_dir():
            near = sorted(p.relative_to(parent) for p in parent.rglob(f"*{args.folder.stem[:12]}*")
                          if p.suffix.lower() in VIDEO_EXT)[:5]
            if near:
                hint = " Meintest du: " + ", ".join(str(parent / n) for n in near)
        log.error("Pfad nicht gefunden: %s.%s", args.folder, hint)
        raise SystemExit(1)
    if args.watch and not single_instance(args.state.with_name("sub_translate.watch.lock")):
        log.warning("Dauerbetrieb läuft bereits in einem anderen Prozess, diese Instanz beendet sich.")
        raise SystemExit(0)
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            hint = "winget install Gyan.FFmpeg" if os.name == "nt" else "sudo apt install ffmpeg"
            raise SystemExit(f"{tool} fehlt ({hint}); danach das Terminal neu öffnen")

    backends = build_backends("argos" if args.argos_only else args.use, args)
    if not backends:
        raise SystemExit("Kein Übersetzer verfügbar, siehe Meldungen oben.")

    args.state.parent.mkdir(parents=True, exist_ok=True)
    try:
        state = json.loads(args.state.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}

    ollama = next((b for b in backends if isinstance(b, Ollama)), None)
    gate = None
    if ollama:  # nur Ollama nutzt die Grafikkarte
        gate = Gate(ollama, game_pause=not args.no_game_pause, gpu_threshold=args.gpu_busy_percent)
        if not args.no_game_pause:
            log.info("Spielpause aktiv (Vollbild oder GPU-Auslastung ab %d %%).", args.gpu_busy_percent)
        log.info("Bei Ollama-Ausfall bis zu %d min warten, bevor ausgewichen wird.", args.ollama_grace_min)

    ocr = None
    if not args.no_ocr:
        exe = find_tesseract(args.tesseract)
        try:
            import PIL  # noqa: F401
            have_pil = True
        except ImportError:
            have_pil = False
        if not exe:
            log.info("OCR aus: Tesseract nicht gefunden (Bild-Untertitel werden übersprungen).")
        elif not have_pil:
            log.warning("OCR aus: Python-Paket Pillow fehlt (pip install pillow).")
        else:
            ocr = OCR(exe, german=args.ocr_german)
            usable = sorted(k for k, v in TESS.items() if v in ocr.langs)
            if not ocr.langs:
                log.warning("OCR aus: Tesseract liefert keine Sprachpakete (%s).", exe)
                ocr = None
            else:
                log.info("OCR aktiv für Bild-Untertitel (Tesseract, Sprachen: %s)%s.", ", ".join(usable),
                         ", auch deutsche Bild-Untertitel -> .de.srt" if args.ocr_german else "")
                if args.ocr_german and "deu" not in ocr.langs:
                    log.warning("--ocr-german gesetzt, aber das deutsche Tesseract-Paket (deu) fehlt.")

    if args.compare:
        if not args.folder.is_file():
            raise SystemExit("--compare braucht den Pfad zu einer einzelnen Videodatei.")
        compare(args.folder, args.prefer_sdh, ocr)
        return

    mover = None
    if args.move_to:
        root = args.folder if args.folder.is_dir() else args.folder.parent
        mover = Mover(args.move_to, root, args.move_skipped)
        log.info("Fertige Filme werden nach %s verschoben%s.", args.move_to,
                 " (auch nicht übersetzbare)" if args.move_skipped else "")
        mover.reachable()

    while True:
        if scan(args.folder, backends, state, args.min_age, args.min_size_mb * 1024 * 1024,
                args.prefer_sdh, gate, mover, ocr):
            args.state.write_text(json.dumps(state, indent=1))
        if not args.watch:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""EdgePowerMeter i18n tool: extract UI strings and machine-translate them locally.

Commands:
    extract                  Scan app/ for tr("...") / N_("...") literals -> strings.json
    translate [--lang ...]   Fill missing catalog entries with local MT, merge overrides
    check                    Report missing / stale keys per language

Translation runs fully offline after a one-time model download: Argos Translate
model packages (.argosmodel = CTranslate2 model + SentencePiece) are loaded
directly with ctranslate2 + sentencepiece, so neither the argostranslate
package nor torch/stanza is needed. See tools/i18n/README.md.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import urllib.request
import zipfile
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[2]
APP_DIR = ROOT / "app"
I18N_DIR = APP_DIR / "i18n"
LOCALES_DIR = I18N_DIR / "locales"
OVERRIDES_DIR = I18N_DIR / "overrides"
STRINGS_FILE = Path(__file__).resolve().parent / "strings.json"
CACHE_DIR = Path.home() / ".cache" / "edgepowermeter-i18n"
INDEX_URL = "https://raw.githubusercontent.com/argosopentech/argospm-index/main/index.json"

TARGET_LANGUAGES = ["it", "es", "fr", "de"]

# Terms that must come out of translation unchanged. Longest first so that
# e.g. "mA" wins over "A". Matched as whole words only.
PROTECTED_TERMS = sorted([
    "EdgePowerMeter", "INA226", "DS3231", "SSD1306", "ESP32-C3", "ESP32", "OpenGL",
    "FFT", "THD", "CSV", "PDF", "RTC", "OLED", "SQW", "NVIDIA",
    "mWh", "Wh", "mAh", "Ah", "mV", "mA", "mW", "kHz", "Hz", "ms", "µs", "ppm",
], key=len, reverse=True)

# Single-letter units are ambiguous ("A" is also an article), so they are only
# protected right after a number or placeholder, e.g. "{value} V", "5 s".
_UNIT_AFTER_VALUE_RE = re.compile(r"(?<=[\d}\]])\s?(?:V|A|W|s|%)(?![\w])")
# Bracketed units / short codes such as "[V]", "(mA)", "[s]".
_BRACKETED_RE = re.compile(r"[\[(][A-Za-zµ%/]{1,5}[\])]")

_PLACEHOLDER_RE = re.compile(r"\{[^{}]*\}")
_TERM_RE = re.compile(
    r"(?<![\w-])(" + "|".join(re.escape(t) for t in PROTECTED_TERMS) + r")(?![\w-])"
)
_TOKEN_RE = re.compile(r"\[\s*(\d+)\s*\]")


# -----------------------------------------------------------------------------
# Extraction
# -----------------------------------------------------------------------------

def _call_name(node: ast.Call) -> Optional[str]:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "i18n":
        return func.attr
    return None


def extract_strings(app_dir: Optional[Path] = None) -> List[str]:
    """Collect literals passed as first positional argument to tr() / N_()."""
    app_dir = app_dir or APP_DIR
    found = set()
    for path in sorted(app_dir.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as e:
            print(f"[WARN] Skipping {path}: {e}", file=sys.stderr)
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _call_name(node) not in ("tr", "N_"):
                continue
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                found.add(node.args[0].value)
            elif node.args:
                rel = path.relative_to(ROOT)
                print(f"[WARN] {rel}:{node.lineno}: non-literal {_call_name(node)}() argument "
                      "is not extractable", file=sys.stderr)
    return sorted(found)


# -----------------------------------------------------------------------------
# Catalog I/O
# -----------------------------------------------------------------------------

def _read_json(path: Path, default):
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=isinstance(data, dict))
        f.write("\n")


def load_strings() -> List[str]:
    strings = _read_json(STRINGS_FILE, None)
    if strings is None:
        sys.exit(f"{STRINGS_FILE} not found - run 'extract' first")
    return strings


# -----------------------------------------------------------------------------
# Local machine translation (Argos models via ctranslate2 + sentencepiece)
# -----------------------------------------------------------------------------

def _download(url: str, dest: Path) -> None:
    """Download to dest atomically (the model host rejects urllib's default User-Agent)."""
    print(f"Downloading {url} ...")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (edgepowermeter-i18n)"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as f:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
    tmp.replace(dest)


def _package_info(lang: str) -> dict:
    index_path = CACHE_DIR / "index.json"
    if not index_path.exists():
        _download(INDEX_URL, index_path)
    candidates = [p for p in _read_json(index_path, [])
                  if p.get("from_code") == "en" and p.get("to_code") == lang]
    if not candidates:
        sys.exit(f"No en->{lang} package in the Argos index")
    return max(candidates, key=lambda p: [int(x) for x in str(p.get("package_version", "0")).split(".")])


def _ensure_model(lang: str) -> Path:
    """Download and unpack the en->lang model once; return its directory."""
    model_root = CACHE_DIR / f"en_{lang}"
    if (model_root / "ready").exists():
        return Path((model_root / "ready").read_text().strip())

    info = _package_info(lang)
    url = next(link for link in info["links"] if link.startswith("http"))
    archive = CACHE_DIR / f"en_{lang}.argosmodel"
    if not archive.exists():
        _download(url, archive)
    with zipfile.ZipFile(archive) as zf:
        # Skip the bundled stanza sentence splitter: UI strings are short.
        members = [m for m in zf.namelist() if "/stanza/" not in m]
        zf.extractall(model_root, members)
    sp_files = list(model_root.rglob("sentencepiece.model"))
    if not sp_files:
        sys.exit(f"Unexpected package layout in {archive}")
    package_dir = sp_files[0].parent
    (model_root / "ready").write_text(str(package_dir))
    return package_dir


class LocalTranslator:
    """en->xx translator backed by an Argos CTranslate2 model."""

    def __init__(self, lang: str):
        import ctranslate2
        import sentencepiece

        package_dir = _ensure_model(lang)
        self.lang = lang
        self._sp = sentencepiece.SentencePieceProcessor(model_file=str(package_dir / "sentencepiece.model"))
        self._model = ctranslate2.Translator(str(package_dir / "model"), device="cpu")

    def translate_batch(self, texts: List[str]) -> List[str]:
        if not texts:
            return []
        tokens = self._sp.encode(texts, out_type=str)
        results = self._model.translate_batch(tokens, beam_size=4, max_decoding_length=256)
        # Join pieces manually: some packages emit merged pieces that are not in
        # the SentencePiece vocab, which sp.decode() would leave with raw "▁".
        return ["".join(r.hypotheses[0]).replace("\u2581", " ").strip() for r in results]


def _protect(text: str) -> Tuple[str, List[str]]:
    """Replace placeholders and protected terms with numbered [N] tokens."""
    saved: List[str] = []

    def keep(match: re.Match) -> str:
        saved.append(match.group(0))
        return f"[{len(saved) - 1}]"

    text = _PLACEHOLDER_RE.sub(keep, text)
    text = _BRACKETED_RE.sub(keep, text)
    text = _TERM_RE.sub(keep, text)
    text = _UNIT_AFTER_VALUE_RE.sub(keep, text)
    return text, saved


def _restore(text: str, saved: List[str]) -> Optional[str]:
    """Undo _protect(); None if any token was lost or mangled by the model."""
    seen = set()

    def put_back(match: re.Match) -> str:
        idx = int(match.group(1))
        if idx >= len(saved):
            return match.group(0)
        seen.add(idx)
        return saved[idx]

    restored = _TOKEN_RE.sub(put_back, text)
    if len(seen) != len(saved) or _TOKEN_RE.search(restored):
        return None
    return restored


# Sentence boundary: punctuation + space + capital letter / placeholder / token.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+(?=[A-Z\[{])")


def _match_case_and_punct(source: str, translated: str) -> str:
    """Keep the source's leading capitalization and trailing punctuation."""
    translated = translated.strip()
    if not translated:
        return translated
    if source[:1].isupper() and translated[:1].islower():
        translated = translated[0].upper() + translated[1:]
    src_end = source[-1:] if source[-1:] in ".:!?…" else ""
    while translated[-1:] in ".!?:" and translated[-1:] != src_end:
        translated = translated[:-1].rstrip()
    if src_end and not translated.endswith(src_end):
        translated += src_end
    return translated


def machine_translate(translator: LocalTranslator, texts: Iterable[str]) -> Dict[str, str]:
    """Translate texts, protecting placeholders/terms. Untranslatable -> omitted.

    Each text is split into sentences (the models drop everything after the
    first sentence otherwise); the pieces are translated in one batch and
    re-joined. Short labels are sent as-is: experiments with context tricks
    (trailing period, quoting, "Menu item: X") made results worse.
    """
    texts = list(texts)
    pieces: List[str] = []
    # Per text: saved tokens and, per line, (first piece, count). Lines are
    # translated separately so explicit newlines survive.
    layout: List[Tuple[List[Tuple[int, int]], List[str]]] = []
    for text in texts:
        protected, saved = _protect(text)
        lines = []
        for line in protected.split("\n"):
            sentences = [s for s in _SENTENCE_SPLIT_RE.split(line) if s.strip()]
            lines.append((len(pieces), len(sentences)))
            pieces.extend(sentences)
        layout.append((lines, saved))

    translated_pieces: List[str] = []
    batch_size = 32
    for start in range(0, len(pieces), batch_size):
        translated_pieces.extend(translator.translate_batch(pieces[start:start + batch_size]))

    out: Dict[str, str] = {}
    for i, (lines, saved) in enumerate(layout):
        source = texts[i]
        # Pure-token strings (e.g. "{value} V") need no translation.
        if not _TOKEN_RE.sub("", _protect(source)[0]).strip(" .,:;-/()|"):
            out[source] = source
            continue
        raw = "\n".join(" ".join(translated_pieces[first:first + count]) for first, count in lines)
        restored = _restore(raw, saved)
        if restored is None:
            print(f"[WARN] {translator.lang}: placeholders lost, keeping English: {source!r} -> {raw!r}",
                  file=sys.stderr)
            continue
        if not _brackets_balanced_like(source, restored):
            print(f"[WARN] {translator.lang}: unbalanced brackets, keeping English: {source!r} -> {restored!r}",
                  file=sys.stderr)
            continue
        out[source] = "\n".join(_match_case_and_punct(s, t) if s.strip() else t
                                 for s, t in zip(source.split("\n"), restored.split("\n")))
    return out


def _brackets_balanced_like(source: str, translated: str) -> bool:
    """The model sometimes drops a closing bracket; such output is rejected."""
    return all(translated.count(o) - translated.count(c) == source.count(o) - source.count(c)
               for o, c in ("()", "[]", "{}"))


# -----------------------------------------------------------------------------
# Commands
# -----------------------------------------------------------------------------

def cmd_extract(_args) -> None:
    strings = extract_strings()
    _write_json(STRINGS_FILE, strings)
    print(f"Extracted {len(strings)} strings -> {STRINGS_FILE.relative_to(ROOT)}")


def cmd_translate(args) -> None:
    strings = load_strings()
    wanted = set(strings)
    langs = args.lang.split(",") if args.lang else TARGET_LANGUAGES
    for lang in langs:
        catalog_path = LOCALES_DIR / f"{lang}.json"
        catalog: Dict[str, str] = _read_json(catalog_path, {})
        overrides: Dict[str, str] = _read_json(OVERRIDES_DIR / f"{lang}.json", {})

        missing = [s for s in strings if s not in catalog and s not in overrides]
        if missing:
            print(f"[{lang}] translating {len(missing)} strings locally...")
            catalog.update(machine_translate(LocalTranslator(lang), missing))

        catalog.update({k: v for k, v in overrides.items() if k in wanted or not args.prune})
        if args.prune:
            catalog = {k: v for k, v in catalog.items() if k in wanted}
        _write_json(catalog_path, catalog)
        print(f"[{lang}] {len(catalog)} entries -> {catalog_path.relative_to(ROOT)}")


def cmd_check(_args) -> None:
    strings = set(load_strings())
    ok = True
    for lang in TARGET_LANGUAGES:
        catalog = _read_json(LOCALES_DIR / f"{lang}.json", {})
        missing = sorted(strings - set(catalog))
        stale = sorted(set(catalog) - strings)
        print(f"[{lang}] {len(catalog)} entries, {len(missing)} missing, {len(stale)} stale")
        for s in missing:
            print(f"    missing: {s!r}")
        for s in stale:
            print(f"    stale:   {s!r}")
        ok = ok and not missing
    sys.exit(0 if ok else 1)


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("extract", help="collect tr()/N_() strings from app/")
    p_tr = sub.add_parser("translate", help="fill missing translations with local MT")
    p_tr.add_argument("--lang", help="comma-separated codes (default: it,es,fr,de)")
    p_tr.add_argument("--prune", action="store_true", help="drop keys no longer in strings.json")
    sub.add_parser("check", help="report missing/stale keys")
    args = parser.parse_args(argv)
    {"extract": cmd_extract, "translate": cmd_translate, "check": cmd_check}[args.command](args)


if __name__ == "__main__":
    main()

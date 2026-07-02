from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import re
import sys
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from jsonschema import Draft202012Validator
from openai import OpenAI


DEFAULT_MODEL = "gpt-5.4-mini"
DEFAULT_TTS_MODEL = "gpt-4o-mini-tts"
DEFAULT_INPUT = Path("input/NGSL_1.2_stats.csv")
DEFAULT_OUTPUT = Path("intermediate/cards.jsonl")
DEFAULT_BATCH_OUTPUT = Path("intermediate/batch_requests.jsonl")
DEFAULT_AUDIO_DIR = Path("output/audio")
DEFAULT_LOG = Path("intermediate/errors.log")
DEFAULT_PROMPTS_DIR = Path("prompts")
DEFAULT_SECRETS_FILE = Path("secrets/openai.env")

KAIIKI_BASE_URL = "https://kaikki.org/dictionary/English/meaning"
HTTP_HEADERS = {
    "User-Agent": "anki-generator/0.1 (personal vocabulary deck builder; https://kaikki.org/dictionary/English/)",
}
WEAK_FORM_WORDS = {"a", "an", "and", "are", "as", "at", "be", "been", "can", "for", "from", "had", "has", "have", "he", "her", "him", "his", "is", "must", "of", "or", "our", "shall", "she", "should", "some", "than", "that", "the", "them", "there", "to", "us", "was", "we", "were", "will", "would", "you", "your"}
UNWANTED_PRONUNCIATION_TAGS = {
    "UK",
    "British",
    "Received-Pronunciation",
    "General-Australian",
    "Australia",
    "Australian",
    "Canada",
    "Canadian",
    "Ireland",
    "Irish",
    "New-Zealand",
    "Northumbria",
    "Scotland",
    "South-Asia",
    "South-African",
}
AMERICAN_PRONUNCIATION_TAGS = {
    "US",
    "General-American",
    "American",
    "United-States",
}
PRONUNCIATION_OVERRIDES = {
    "to": {"english_ipa": "/tu/", "pronunciation_notes": "Карточка использует полную форму; слабая форма /tə/ очень частая."},
    "a": {"english_ipa": "/eɪ/", "pronunciation_notes": "Карточка использует полную форму; слабая форма /ə/ очень частая."},
    "of": {"english_ipa": "/əv/", "pronunciation_notes": "Обычная слабая форма."},
    "with": {"english_ipa": "/wɪθ/", "pronunciation_notes": "Здесь используется вариант с глухим /θ/."},
}
EXAMPLE_OVERRIDES = {
    "to be": {
        "english_example": "I want to be ready.",
        "english_example_ipa": "/aɪ wɑnt tu bi ˈɹɛdi/",
        "russian_example_translation": "Я хочу быть готовым.",
    },
    "to have": {
        "english_example": "I want to have a dog.",
        "english_example_ipa": "/aɪ wɑnt tu hæv ə dɔɡ/",
        "russian_example_translation": "Я хочу завести собаку.",
    },
}
FORCE_TTS_WORD_AUDIO = {"with"}
TTS_PROFILES = [
    {"voice": "coral", "persona": "Warm female tutor; clear, friendly, natural General American."},
    {"voice": "onyx", "persona": "Calm male narrator; clear, steady, natural General American."},
    {"voice": "nova", "persona": "Bright female teacher; concise, upbeat, natural General American."},
    {"voice": "echo", "persona": "Relaxed male speaker; conversational, natural General American."},
    {"voice": "shimmer", "persona": "Gentle female coach; clear, patient, natural General American."},
    {"voice": "fable", "persona": "Expressive male storyteller; clear, natural General American."},
]

CARD_FIELD_ORDER = [
    "word",
    "source_word",
    "part_of_speech",
    "english_ipa",
    "ipa_source",
    "pronunciation_notes",
    "russian_translations",
    "main_translation",
    "english_example",
    "english_example_ipa",
    "russian_example_translation",
    "common_collocations",
    "inflection_forms",
    "extra_form_entries",
    "related_base_word",
    "needs_review",
    "review_reason",
    "source_file",
    "source_row_number",
    "source_rank",
    "source_frequency",
    "created_at_utc",
    "audio_source",
    "english_example_audio_source",
    "audio_url",
    "audio_file_path",
    "english_example_audio_file_path",
]


ENTRY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "word": {"type": "string"},
        "source_word": {"type": "string"},
        "part_of_speech": {
            "type": "string",
            "enum": [
                "noun",
                "verb",
                "adjective",
                "adverb",
                "pronoun",
                "preposition",
                "conjunction",
                "determiner",
                "article",
                "interjection",
                "numeral",
                "particle",
                "phrase",
                "verb_form",
                "noun_form",
                "adjective_form",
                "other",
            ],
        },
        "english_ipa": {"type": "string"},
        "ipa_source": {"type": "string", "enum": ["kaikki", "llm", "mixed", "missing"]},
        "pronunciation_notes": {"type": "string"},
        "russian_translations": {"type": "array", "items": {"type": "string"}},
        "main_translation": {"type": "string"},
        "english_example": {"type": "string"},
        "english_example_ipa": {"type": "string"},
        "russian_example_translation": {"type": "string"},
        "common_collocations": {"type": "array", "items": {"type": "string"}},
        "inflection_forms": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "verb_base": {"type": ["string", "null"]},
                "verb_past": {"type": ["string", "null"]},
                "verb_past_participle": {"type": ["string", "null"]},
                "verb_present_participle": {"type": ["string", "null"]},
                "verb_third_person_singular": {"type": ["string", "null"]},
                "noun_plural": {"type": ["string", "null"]},
                "adjective_comparative": {"type": ["string", "null"]},
                "adjective_superlative": {"type": ["string", "null"]},
            },
            "required": [
                "verb_base",
                "verb_past",
                "verb_past_participle",
                "verb_present_participle",
                "verb_third_person_singular",
                "noun_plural",
                "adjective_comparative",
                "adjective_superlative",
            ],
        },
        "extra_form_entries": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "word": {"type": "string"},
                    "part_of_speech": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["word", "part_of_speech", "reason"],
            },
        },
        "related_base_word": {"type": ["string", "null"]},
        "needs_review": {"type": "boolean"},
        "review_reason": {"type": "string"},
        "audio_source": {"type": "string", "enum": ["wikimedia", "openai_tts", "missing", "not_checked"]},
        "english_example_audio_source": {"type": "string", "enum": ["openai_tts", "missing", "not_checked"]},
        "audio_url": {"type": "string"},
        "audio_file_path": {"type": "string"},
        "english_example_audio_file_path": {"type": "string"},
    },
    "required": [
        "word",
        "source_word",
        "part_of_speech",
        "english_ipa",
        "ipa_source",
        "pronunciation_notes",
        "russian_translations",
        "main_translation",
        "english_example",
        "english_example_ipa",
        "russian_example_translation",
        "common_collocations",
        "inflection_forms",
        "extra_form_entries",
        "related_base_word",
        "needs_review",
        "review_reason",
        "audio_source",
        "english_example_audio_source",
        "audio_url",
        "audio_file_path",
        "english_example_audio_file_path",
    ],
}

CARD_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "entries": {"type": "array", "items": ENTRY_SCHEMA},
    },
    "required": ["entries"],
}

FINAL_CARD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        **ENTRY_SCHEMA["properties"],
        "source_file": {"type": "string"},
        "source_row_number": {"type": "integer"},
        "source_rank": {"type": ["string", "null"]},
        "source_frequency": {"type": ["string", "null"]},
        "created_at_utc": {"type": "string"},
    },
    "required": [
        *ENTRY_SCHEMA["required"],
        "source_file",
        "source_row_number",
        "source_rank",
        "source_frequency",
        "created_at_utc",
    ],
}


@dataclass(frozen=True)
class InputRow:
    word: str
    part_of_speech: str | None
    row_number: int
    source_file: str
    rank: str | None = None
    frequency: str | None = None
    is_extra_form: bool = False
    related_base_word: str | None = None


class TtsRotator:
    def __init__(self) -> None:
        self.index = 0

    def next(self) -> dict[str, str]:
        profile = TTS_PROFILES[self.index % len(TTS_PROFILES)]
        self.index += 1
        return profile


def main() -> int:
    args = parse_args()
    setup_logging(args.log)
    try:
        validate_schemas()
        return run(args)
    except KeyboardInterrupt:
        logging.warning("Interrupted")
        return 130
    except Exception:
        logging.exception("Fatal pipeline error")
        return 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate Anki-ready card JSONL from a frequency list.")
    parser.add_argument("input", nargs="?", type=Path, default=DEFAULT_INPUT, help="CSV or TXT input file.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="JSONL card cache/output.")
    parser.add_argument("--batch-output", type=Path, default=DEFAULT_BATCH_OUTPUT, help="Batch API JSONL request file.")
    parser.add_argument("--audio-dir", type=Path, default=DEFAULT_AUDIO_DIR, help="Directory for retrieved or generated audio.")
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG, help="Error log path.")
    parser.add_argument("--prompts-dir", type=Path, default=DEFAULT_PROMPTS_DIR, help="Directory containing prompt templates.")
    parser.add_argument("--secrets-file", type=Path, default=DEFAULT_SECRETS_FILE, help="Env file containing OPENAI_API_KEY.")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="OpenAI model for card generation.")
    parser.add_argument("--tts-model", default=DEFAULT_TTS_MODEL, help="OpenAI TTS model used when Wiktionary audio is absent.")
    parser.add_argument("--word-column", help="CSV column containing the word or lemma.")
    parser.add_argument("--pos-column", help="CSV column containing part of speech.")
    parser.add_argument("--limit", type=int, help="Maximum source input rows to process.")
    parser.add_argument("--dry-run", action="store_true", help="Process only the first 20 source rows.")
    parser.add_argument("--batch", action="store_true", help="Write OpenAI Batch API JSONL requests instead of calling the API.")
    parser.add_argument("--skip-audio", action="store_true", help="Do not download Wiktionary audio or generate fallback TTS.")
    parser.add_argument("--confirm-full-run", action="store_true", help="Allow processing the full input list.")
    parser.add_argument("--request-timeout", type=float, default=20.0, help="Network request timeout in seconds.")
    return parser.parse_args()


def setup_logging(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stderr),
            logging.FileHandler(log_path, encoding="utf-8"),
        ],
    )


def validate_schemas() -> None:
    Draft202012Validator.check_schema(CARD_RESPONSE_SCHEMA)
    Draft202012Validator.check_schema(FINAL_CARD_SCHEMA)


def run(args: argparse.Namespace) -> int:
    load_env_file(args.secrets_file)
    rows = read_input(args.input, args.word_column, args.pos_column)
    if not rows:
        logging.warning("No input rows found in %s", args.input)
        return 0

    source_limit = args.limit
    if args.dry_run:
        source_limit = 20
    elif source_limit is None and not args.confirm_full_run:
        source_limit = 20
        logging.warning("Safety limit active: processing first 20 source rows. Pass --confirm-full-run to process all rows.")

    source_rows = rows[:source_limit] if source_limit is not None else rows
    input_word_keys = {cache_key(row.word) for row in rows}
    cached_source_keys, cached_card_keys = read_cache_keys(args.output)
    system_prompt, user_template = load_prompts(args.prompts_dir)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.batch_output.parent.mkdir(parents=True, exist_ok=True)
    args.audio_dir.mkdir(parents=True, exist_ok=True)

    if args.batch:
        write_batch_requests(
            args=args,
            rows=source_rows,
            cached_source_keys=cached_source_keys,
            system_prompt=system_prompt,
            user_template=user_template,
        )
        return 0

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(f"OPENAI_API_KEY is missing. Add it to {args.secrets_file}.")

    client = OpenAI(api_key=api_key)
    queue: deque[InputRow] = deque(source_rows)
    scheduled_extra_keys: set[str] = set()
    tts_rotator = TtsRotator()
    processed_source_rows = 0
    written_cards = 0

    while queue:
        row = queue.popleft()
        source_key = cache_key(row.word)
        if source_key in cached_source_keys:
            logging.info("Skipping cached source word: %s", row.word)
            continue

        if not row.is_extra_form:
            processed_source_rows += 1

        try:
            wiktionary = fetch_kaikki_summary(row.word, args.request_timeout)
            response_data = generate_card_response(client, args.model, system_prompt, user_template, row, wiktionary)
            response_data = normalize_response(response_data, row)
            validate_response(response_data, row.word)
        except Exception:
            logging.exception("Failed to generate card data for %s", row.word)
            continue

        extra_scheduled_for_row = False
        for entry in response_data["entries"]:
            final_card = enrich_card(entry, row, wiktionary)
            card_key = cache_key(final_card["word"])
            if card_key in cached_card_keys:
                logging.warning("Skipping duplicate card word: %s", final_card["word"])
                continue

            selected_extra_entries: list[dict[str, str]] = []
            if not row.is_extra_form and not extra_scheduled_for_row:
                for extra in final_card["extra_form_entries"]:
                    extra_key = cache_key(extra["word"])
                    if extra_key in input_word_keys or extra_key in cached_card_keys or extra_key in scheduled_extra_keys:
                        continue
                    selected_extra_entries = [extra]
                    scheduled_extra_keys.add(extra_key)
                    extra_scheduled_for_row = True
                    queue.append(
                        InputRow(
                            word=extra["word"],
                            part_of_speech=extra["part_of_speech"],
                            row_number=0,
                            source_file=row.source_file,
                            is_extra_form=True,
                            related_base_word=final_card["word"],
                        )
                    )
                    break
            final_card["extra_form_entries"] = selected_extra_entries

            try:
                attach_audio(final_card, wiktionary, client, args, tts_rotator)
                validate_final_card(final_card)
            except Exception:
                logging.exception("Failed to finalize card for %s", final_card.get("word", row.word))
                continue

            append_jsonl(args.output, final_card)
            cached_source_keys.add(source_key)
            cached_card_keys.add(card_key)
            written_cards += 1

    logging.info("Processed %s source rows; wrote %s cards to %s", processed_source_rows, written_cards, args.output)
    return 0


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def read_input(path: Path, word_column: str | None, pos_column: str | None) -> list[InputRow]:
    suffix = path.suffix.lower()
    if suffix == ".txt":
        return read_txt_input(path)
    if suffix == ".csv":
        return read_csv_input(path, word_column, pos_column)
    raise ValueError(f"Unsupported input format: {path.suffix}. Use .csv or .txt.")


def read_txt_input(path: Path) -> list[InputRow]:
    rows: list[InputRow] = []
    for row_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        word = line.strip()
        if word:
            rows.append(InputRow(word=word, part_of_speech=None, row_number=row_number, source_file=str(path)))
    return rows


def read_csv_input(path: Path, word_column: str | None, pos_column: str | None) -> list[InputRow]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            return []
        fields = [field.strip() for field in reader.fieldnames]
        word_field = resolve_column(fields, word_column, ["lemma", "word", "term", "headword"])
        pos_field = resolve_column(fields, pos_column, ["part_of_speech", "part of speech", "pos"])
        rank_field = resolve_column(fields, None, ["sfi rank", "rank", "frequency rank"])
        frequency_field = resolve_column(fields, None, ["adjusted frequency per million (u)", "frequency", "freq", "count"])
        rows: list[InputRow] = []
        for row_number, raw_row in enumerate(reader, start=2):
            row = {key.strip(): (value or "").strip() for key, value in raw_row.items() if key is not None}
            word = row.get(word_field, "").strip()
            if not word:
                continue
            rows.append(
                InputRow(
                    word=word,
                    part_of_speech=empty_to_none(row.get(pos_field)) if pos_field else None,
                    row_number=row_number,
                    source_file=str(path),
                    rank=empty_to_none(row.get(rank_field)) if rank_field else None,
                    frequency=empty_to_none(row.get(frequency_field)) if frequency_field else None,
                )
            )
        return rows


def resolve_column(fields: list[str], explicit: str | None, preferred: list[str]) -> str | None:
    normalized = {field.casefold(): field for field in fields}
    if explicit:
        if explicit in fields:
            return explicit
        match = normalized.get(explicit.casefold())
        if match:
            return match
        raise ValueError(f"Column not found: {explicit}")
    for candidate in preferred:
        match = normalized.get(candidate.casefold())
        if match:
            return match
    return fields[0] if preferred and preferred[0] in {"lemma", "word", "term", "headword"} else None


def empty_to_none(value: str | None) -> str | None:
    return value if value else None


def read_cache_keys(path: Path) -> tuple[set[str], set[str]]:
    source_keys: set[str] = set()
    card_keys: set[str] = set()
    if not path.exists():
        return source_keys, card_keys
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                card = json.loads(line)
            except json.JSONDecodeError:
                logging.warning("Ignoring invalid JSONL cache line %s in %s", line_number, path)
                continue
            if card.get("source_word"):
                source_keys.add(cache_key(card["source_word"]))
            if card.get("word"):
                card_keys.add(cache_key(card["word"]))
    return source_keys, card_keys


def load_prompts(prompts_dir: Path) -> tuple[str, str]:
    system_path = prompts_dir / "card_system.md"
    user_path = prompts_dir / "card_user.md"
    return system_path.read_text(encoding="utf-8"), user_path.read_text(encoding="utf-8")


def fetch_kaikki_summary(word: str, timeout: float) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    used_url = ""
    for url in kaikki_url_candidates(word):
        try:
            response = requests.get(url, headers=HTTP_HEADERS, timeout=timeout)
        except requests.RequestException as error:
            logging.warning("Kaikki request failed for %s at %s: %s", word, url, error)
            continue
        if response.status_code == 404:
            continue
        response.raise_for_status()
        used_url = url
        entries = [json.loads(line) for line in response.text.splitlines() if line.strip()]
        break

    if not entries:
        return {
            "available": False,
            "url": "",
            "preferred_ipa": "",
            "preferred_audio_url": "",
            "preferred_audio_name": "",
            "preferred_audio_tags": [],
            "ipa_candidates": [],
            "pos_candidates": [],
            "form_candidates": [],
            "glosses": [],
        }

    summary = summarize_kaikki_entries(word, used_url, entries)
    return summary


def kaikki_url_candidates(word: str) -> list[str]:
    clean = word.strip()
    if clean.startswith("to "):
        clean = clean[3:].strip()
    quoted_word = quote(clean, safe="")
    lower = clean.casefold()
    first = quote(lower[:1] or "_", safe="")
    second = quote(lower[:2] or lower[:1] or "_", safe="")
    candidates = [
        f"{KAIIKI_BASE_URL}/{first}/{second}/{quoted_word}.jsonl",
        f"{KAIIKI_BASE_URL}/{first}/{first}/{quoted_word}.jsonl",
        f"{KAIIKI_BASE_URL}/{first}/{first}-/{quoted_word}.jsonl",
    ]
    lower_quoted = quote(lower, safe="")
    if lower_quoted != quoted_word:
        candidates.extend(
            [
                f"{KAIIKI_BASE_URL}/{first}/{second}/{lower_quoted}.jsonl",
                f"{KAIIKI_BASE_URL}/{first}/{first}/{lower_quoted}.jsonl",
                f"{KAIIKI_BASE_URL}/{first}/{first}-/{lower_quoted}.jsonl",
            ]
        )
    return list(dict.fromkeys(candidates))


def summarize_kaikki_entries(word: str, url: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
    sounds: list[dict[str, Any]] = []
    pos_candidates: list[str] = []
    form_candidates: list[dict[str, Any]] = []
    glosses: list[str] = []
    for entry in entries:
        if entry.get("lang_code") not in {None, "en"} and entry.get("lang") != "English":
            continue
        if entry.get("pos"):
            pos_candidates.append(str(entry["pos"]))
        for sound in entry.get("sounds", []) or []:
            if isinstance(sound, dict):
                sound_copy = dict(sound)
                sound_copy["entry_pos"] = entry.get("pos", "")
                sounds.append(sound_copy)
        for form in entry.get("forms", []) or []:
            if isinstance(form, dict) and form.get("form"):
                form_candidates.append({"form": form.get("form"), "tags": form.get("tags", []), "source_pos": entry.get("pos", "")})
        for sense in entry.get("senses", []) or []:
            if not isinstance(sense, dict):
                continue
            tags = set(sense.get("tags", []) or [])
            if tags.intersection({"obsolete", "archaic", "rare"}):
                continue
            for gloss in sense.get("glosses", []) or []:
                if gloss and len(glosses) < 8:
                    glosses.append(str(gloss))

    ipa_candidates = [sound_summary(sound) for sound in sounds if sound.get("ipa")]
    best_sound = choose_best_sound(word, sounds)
    audio_sound = choose_best_audio_sound(sounds, best_sound)
    return {
        "available": True,
        "url": url,
        "preferred_ipa": best_sound.get("ipa", "") if best_sound else "",
        "preferred_audio_url": preferred_audio_url(audio_sound) if audio_sound else "",
        "preferred_audio_name": audio_sound.get("audio", "") if audio_sound else "",
        "preferred_audio_tags": audio_sound.get("tags", []) if audio_sound else [],
        "ipa_candidates": ipa_candidates[:12],
        "pos_candidates": sorted(set(pos_candidates)),
        "form_candidates": form_candidates[:30],
        "glosses": glosses,
    }


def sound_summary(sound: dict[str, Any]) -> dict[str, Any]:
    return {
        "ipa": sound.get("ipa", ""),
        "tags": sound.get("tags", []),
        "raw_tags": sound.get("raw_tags", []),
        "note": sound.get("note", ""),
        "entry_pos": sound.get("entry_pos", ""),
    }


def choose_best_sound(word: str, sounds: list[dict[str, Any]]) -> dict[str, Any] | None:
    ipa_sounds = [sound for sound in sounds if sound.get("ipa") and not has_unwanted_tags(sound)]
    if not ipa_sounds:
        return None

    weak = word.casefold().removeprefix("to ") in WEAK_FORM_WORDS

    def score(sound: dict[str, Any]) -> tuple[int, int, int, int, int]:
        tags = tag_set(sound)
        note = str(sound.get("note", "")).casefold()
        ipa = str(sound.get("ipa", ""))
        weak_score = 1 if weak and is_weak_form_candidate(ipa, note, tags) else 0
        us_score = 1 if tags.intersection({tag.casefold() for tag in AMERICAN_PRONUNCIATION_TAGS}) else 0
        slash_score = 1 if ipa.startswith("/") else 0
        clean_note_score = 0 if is_regional_note(note) else 1
        untagged_score = 1 if not tags else 0
        if weak:
            return weak_score, slash_score, us_score, clean_note_score, untagged_score
        return us_score, slash_score, clean_note_score, untagged_score, 0

    return max(ipa_sounds, key=score)


def choose_best_audio_sound(sounds: list[dict[str, Any]], best_sound: dict[str, Any] | None) -> dict[str, Any] | None:
    if best_sound and preferred_audio_url(best_sound):
        return best_sound
    audio_sounds = [sound for sound in sounds if preferred_audio_url(sound) and not has_unwanted_tags(sound)]
    if not audio_sounds:
        return None

    def score(sound: dict[str, Any]) -> tuple[int, int]:
        tags = tag_set(sound)
        us_score = 1 if tags.intersection({tag.casefold() for tag in AMERICAN_PRONUNCIATION_TAGS}) else 0
        untagged_score = 1 if not tags else 0
        return us_score, untagged_score

    return max(audio_sounds, key=score)


def tag_set(sound: dict[str, Any]) -> set[str]:
    tags = sound.get("tags", []) or []
    raw_tags = sound.get("raw_tags", []) or []
    return {str(tag).casefold() for tag in [*tags, *raw_tags]}


def has_unwanted_tags(sound: dict[str, Any]) -> bool:
    tags = tag_set(sound)
    if tags.intersection({tag.casefold() for tag in AMERICAN_PRONUNCIATION_TAGS}):
        return False
    return bool(tags.intersection({tag.casefold() for tag in UNWANTED_PRONUNCIATION_TAGS}))


def is_weak_form_candidate(ipa: str, note: str, tags: set[str]) -> bool:
    if "ˈ" in ipa or "ˌ" in ipa:
        return False
    if "after a vowel" in note:
        return False
    return (
        "weak form" in note
        or "unstressed" in note
        or "unstressed" in tags
        or "before a consonant" in note
        or "ə" in ipa
    )


def is_regional_note(note: str) -> bool:
    regional_markers = ["merger", "lancashire", "scotland", "south africa", "northumbria"]
    return any(marker in note for marker in regional_markers)


def preferred_audio_url(sound: dict[str, Any]) -> str:
    return str(sound.get("mp3_url") or sound.get("ogg_url") or "")


def generate_card_response(
    client: OpenAI,
    model: str,
    system_prompt: str,
    user_template: str,
    row: InputRow,
    wiktionary: dict[str, Any],
) -> dict[str, Any]:
    response = client.responses.create(
        model=model,
        input=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": render_user_prompt(user_template, row, wiktionary)},
        ],
        text={"format": response_format()},
    )
    text = extract_response_text(response)
    data = json.loads(text)
    return data


def normalize_response(data: dict[str, Any], row: InputRow) -> dict[str, Any]:
    entries = data.get("entries", [])
    if not isinstance(entries, list):
        return {"entries": []}

    normalized_entries: list[dict[str, Any]] = []
    seen_words: set[str] = set()
    entry_limit = 1 if row.is_extra_form else 2
    for raw_entry in entries:
        if not isinstance(raw_entry, dict):
            continue
        entry = dict(raw_entry)
        entry["source_word"] = row.word
        entry["word"] = normalize_word_field(str(entry.get("word", row.word)), str(entry.get("part_of_speech", "")))
        entry["russian_translations"] = string_list(entry.get("russian_translations"))[:3]
        entry["common_collocations"] = string_list(entry.get("common_collocations"))[:3]
        entry["pronunciation_notes"] = normalize_russian_note(entry.get("pronunciation_notes"), "Проверьте произношение.")
        entry["review_reason"] = normalize_russian_note(entry.get("review_reason"), "Проверьте карточку.")
        if entry.get("needs_review") and not entry["review_reason"]:
            entry["review_reason"] = "Проверьте карточку."
        if not entry.get("needs_review"):
            entry["review_reason"] = ""
        if is_unhelpful_entry(entry):
            continue
        entry["audio_file_path"] = ""
        entry["audio_url"] = ""
        entry["audio_source"] = "not_checked"
        entry["english_example_audio_file_path"] = ""
        entry["english_example_audio_source"] = "not_checked"
        if row.is_extra_form:
            entry["extra_form_entries"] = []
        else:
            entry["extra_form_entries"] = normalize_extra_entries(entry.get("extra_form_entries"))

        key = cache_key(entry["word"])
        if key in seen_words:
            continue
        seen_words.add(key)
        normalized_entries.append(entry)
        if len(normalized_entries) >= entry_limit:
            break
    return {"entries": normalized_entries}


def is_unhelpful_entry(entry: dict[str, Any]) -> bool:
    text = " ".join(
        [
            str(entry.get("pronunciation_notes", "")),
            str(entry.get("review_reason", "")),
            " ".join(string_list(entry.get("russian_translations"))),
            " ".join(string_list(entry.get("common_collocations"))),
        ]
    ).casefold()
    blocked_markers = ["редк", "устар", "диалект", "сленг", "простореч", "разговорн", "техническ", "специальн"]
    return any(marker in text for marker in blocked_markers)


def normalize_word_field(word: str, part_of_speech: str) -> str:
    clean_word = re.sub(r"\s+", " ", word.strip())
    if part_of_speech == "verb" and not clean_word.casefold().startswith("to "):
        return f"to {clean_word}"
    return clean_word


def normalize_extra_entries(value: Any) -> list[dict[str, str]]:
    extras: list[dict[str, str]] = []
    if not isinstance(value, list):
        return extras
    for raw_extra in value:
        if not isinstance(raw_extra, dict):
            continue
        word = str(raw_extra.get("word", "")).strip()
        part_of_speech = normalize_extra_part_of_speech(str(raw_extra.get("part_of_speech", "")))
        if not word or not part_of_speech:
            continue
        if part_of_speech not in {"verb_form", "noun_form", "adjective_form"}:
            continue
        if not re.fullmatch(r"[A-Za-z -]+", word):
            continue
        extras.append(
            {
                "word": normalize_word_field(word, part_of_speech),
                "part_of_speech": part_of_speech,
                "reason": normalize_russian_note(raw_extra.get("reason"), "Частая отдельная форма."),
            }
        )
        break
    return extras


def normalize_extra_part_of_speech(value: str) -> str:
    normalized = re.sub(r"[\s-]+", "_", value.strip().casefold())
    aliases = {
        "verbform": "verb_form",
        "verb_form": "verb_form",
        "nounform": "noun_form",
        "noun_form": "noun_form",
        "adjectiveform": "adjective_form",
        "adjective_form": "adjective_form",
    }
    return aliases.get(normalized, normalized)


def string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def normalize_russian_note(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if not re.search(r"[А-Яа-яЁё]", text):
        return fallback
    return text[:160]


def extract_response_text(response: Any) -> str:
    output_text = getattr(response, "output_text", None)
    if output_text:
        return output_text
    if hasattr(response, "model_dump"):
        dumped = response.model_dump()
    elif isinstance(response, dict):
        dumped = response
    else:
        dumped = json.loads(response.model_dump_json())
    found = find_text_value(dumped)
    if found:
        return found
    raise ValueError("Could not find text output in OpenAI response.")


def find_text_value(value: Any) -> str | None:
    if isinstance(value, dict):
        if value.get("type") in {"output_text", "text"} and isinstance(value.get("text"), str):
            return value["text"]
        for child in value.values():
            found = find_text_value(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_text_value(child)
            if found:
                return found
    return None


def response_format() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "name": "anki_card_response",
        "schema": CARD_RESPONSE_SCHEMA,
        "strict": True,
    }


def render_user_prompt(template: str, row: InputRow, wiktionary: dict[str, Any]) -> str:
    payload = {
        "word": row.word,
        "known_part_of_speech": row.part_of_speech,
        "is_extra_form": row.is_extra_form,
        "related_base_word": row.related_base_word,
        "rank": row.rank,
        "frequency": row.frequency,
        "wiktionary": wiktionary,
    }
    return template.replace("{{PAYLOAD_JSON}}", json.dumps(payload, ensure_ascii=False, indent=2))


def validate_response(data: dict[str, Any], word: str) -> None:
    errors = sorted(Draft202012Validator(CARD_RESPONSE_SCHEMA).iter_errors(data), key=lambda error: list(error.path))
    if errors:
        messages = "; ".join(error.message for error in errors)
        raise ValueError(f"Structured response for {word!r} failed schema validation: {messages}")


def validate_final_card(card: dict[str, Any]) -> None:
    errors = sorted(Draft202012Validator(FINAL_CARD_SCHEMA).iter_errors(card), key=lambda error: list(error.path))
    if errors:
        messages = "; ".join(error.message for error in errors)
        raise ValueError(f"Final card for {card.get('word')!r} failed schema validation: {messages}")


def enrich_card(entry: dict[str, Any], row: InputRow, wiktionary: dict[str, Any]) -> dict[str, Any]:
    card = dict(entry)
    card["source_word"] = row.word
    if row.related_base_word and not card["related_base_word"]:
        card["related_base_word"] = row.related_base_word
    if wiktionary.get("preferred_ipa") and card["english_ipa"] == wiktionary["preferred_ipa"]:
        card["ipa_source"] = "kaikki"
    card["source_file"] = row.source_file
    card["source_row_number"] = row.row_number
    card["source_rank"] = row.rank
    card["source_frequency"] = row.frequency
    card["created_at_utc"] = datetime.now(timezone.utc).isoformat()
    apply_pronunciation_override(card)
    return card


def apply_pronunciation_override(card: dict[str, Any]) -> None:
    override = PRONUNCIATION_OVERRIDES.get(cache_key(card["word"]))
    if override:
        card["english_ipa"] = override["english_ipa"]
        card["ipa_source"] = "mixed"
        card["pronunciation_notes"] = override["pronunciation_notes"]

    example_override = EXAMPLE_OVERRIDES.get(cache_key(card["word"]))
    if example_override:
        card.update(example_override)


def attach_audio(
    card: dict[str, Any],
    wiktionary: dict[str, Any],
    client: OpenAI,
    args: argparse.Namespace,
    tts_rotator: TtsRotator,
) -> None:
    if args.skip_audio:
        card["audio_file_path"] = ""
        card["audio_url"] = wiktionary.get("preferred_audio_url", "")
        card["audio_source"] = "not_checked"
        card["english_example_audio_file_path"] = ""
        card["english_example_audio_source"] = "not_checked"
        return

    audio_url = "" if cache_key(card["word"]) in FORCE_TTS_WORD_AUDIO else wiktionary.get("preferred_audio_url", "")
    if audio_url:
        try:
            audio_path = download_audio(audio_url, card["word"], args.audio_dir, args.request_timeout)
            card["audio_file_path"] = str(audio_path)
            card["audio_url"] = audio_url
            card["audio_source"] = "wikimedia"
        except Exception:
            logging.warning("Wikimedia audio unavailable for %s; using TTS fallback.", card["word"])

    if not card["audio_file_path"]:
        try:
            audio_path = generate_tts_audio(
                client=client,
                text=card["word"],
                path=args.audio_dir / f"{safe_slug(card['word'])}.mp3",
                model=args.tts_model,
                profile=tts_rotator.next(),
                content_kind="word",
            )
            card["audio_file_path"] = str(audio_path)
            card["audio_url"] = ""
            card["audio_source"] = "openai_tts"
        except Exception:
            logging.exception("Failed to generate TTS audio for %s", card["word"])
            card["audio_file_path"] = ""
            card["audio_url"] = ""
            card["audio_source"] = "missing"
            card["needs_review"] = True
            card["review_reason"] = append_reason(card["review_reason"], "Нет аудио слова.")

    try:
        example_audio_path = generate_tts_audio(
            client=client,
            text=card["english_example"],
            path=example_audio_path_for(card, args.audio_dir),
            model=args.tts_model,
            profile=tts_rotator.next(),
            content_kind="example",
        )
        card["english_example_audio_file_path"] = str(example_audio_path)
        card["english_example_audio_source"] = "openai_tts"
    except Exception:
        logging.exception("Failed to generate example TTS audio for %s", card["word"])
        card["english_example_audio_file_path"] = ""
        card["english_example_audio_source"] = "missing"
        card["needs_review"] = True
        card["review_reason"] = append_reason(card["review_reason"], "Нет аудио примера.")


def download_audio(url: str, word: str, audio_dir: Path, timeout: float) -> Path:
    suffix = ".mp3" if ".mp3" in url.casefold() else ".ogg"
    path = audio_dir / f"{safe_slug(word)}{suffix}"
    if path.exists() and path.stat().st_size > 0:
        return path
    response = requests.get(url, headers=HTTP_HEADERS, timeout=timeout)
    response.raise_for_status()
    path.write_bytes(response.content)
    return path


def generate_tts_audio(
    client: OpenAI,
    text: str,
    path: Path,
    model: str,
    profile: dict[str, str],
    content_kind: str,
) -> Path:
    if path.exists() and path.stat().st_size > 0:
        return path
    if content_kind == "example":
        instructions = f"{profile['persona']} Read the sentence once, naturally and clearly."
    else:
        instructions = f"{profile['persona']} Read the vocabulary item once, carefully and clearly."
    with client.audio.speech.with_streaming_response.create(
        model=model,
        voice=profile["voice"],
        input=text,
        instructions=instructions,
    ) as response:
        response.stream_to_file(path)
    return path


def example_audio_path_for(card: dict[str, Any], audio_dir: Path) -> Path:
    digest = hashlib.sha1(card["english_example"].encode("utf-8")).hexdigest()[:8]
    return audio_dir / f"{safe_slug(card['word'])}-example-{digest}.mp3"


def append_reason(existing: str, addition: str) -> str:
    if not existing:
        return addition
    if addition in existing:
        return existing
    return f"{existing} {addition}"


def write_batch_requests(
    args: argparse.Namespace,
    rows: list[InputRow],
    cached_source_keys: set[str],
    system_prompt: str,
    user_template: str,
) -> None:
    written = 0
    with args.batch_output.open("w", encoding="utf-8") as handle:
        for row in rows:
            if cache_key(row.word) in cached_source_keys:
                continue
            wiktionary = fetch_kaikki_summary(row.word, args.request_timeout)
            request = {
                "custom_id": batch_custom_id(row),
                "method": "POST",
                "url": "/v1/responses",
                "body": {
                    "model": args.model,
                    "input": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": render_user_prompt(user_template, row, wiktionary)},
                    ],
                    "text": {"format": response_format()},
                },
            }
            handle.write(json.dumps(request, ensure_ascii=False) + "\n")
            written += 1
    logging.info("Wrote %s batch requests to %s", written, args.batch_output)


def batch_custom_id(row: InputRow) -> str:
    digest = hashlib.sha1(f"{row.row_number}:{row.word}".encode("utf-8")).hexdigest()[:10]
    return f"card-{row.row_number}-{safe_slug(row.word)}-{digest}"


def append_jsonl(path: Path, data: dict[str, Any]) -> None:
    ordered_data = order_card_fields(data)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(ordered_data, ensure_ascii=False) + "\n")


def order_card_fields(data: dict[str, Any]) -> dict[str, Any]:
    ordered = {field: data[field] for field in CARD_FIELD_ORDER if field in data}
    for key, value in data.items():
        if key not in ordered:
            ordered[key] = value
    return ordered


def cache_key(word: str) -> str:
    return re.sub(r"\s+", " ", word.strip().casefold())


def safe_slug(word: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", word.strip().casefold()).strip("-")
    if not slug:
        slug = hashlib.sha1(word.encode("utf-8")).hexdigest()[:12]
    return slug[:80]


if __name__ == "__main__":
    raise SystemExit(main())

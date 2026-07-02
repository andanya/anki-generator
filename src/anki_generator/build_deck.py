from __future__ import annotations

import argparse
import base64
import html
import json
import re
import sqlite3
import tempfile
import time
import zipfile
from dataclasses import dataclass
from hashlib import sha1
from pathlib import Path
from typing import Any


DEFAULT_INPUT = Path("intermediate/cards.jsonl")
DEFAULT_OUTPUT = Path("output/anki_generator.apkg")
DEFAULT_DECK_NAME = "NGSL Russian-English"
FIELD_SEPARATOR = "\x1f"
NEW_CARDS_PER_DAY = 4


FIELDS = [
    "RussianWord",
    "RussianExample",
    "EnglishWord",
    "EnglishIPA",
    "EnglishExample",
    "EnglishExampleIPA",
    "Notes",
    "WordAudio",
    "ExampleAudio",
]


@dataclass(frozen=True)
class MediaRef:
    source: Path
    name: str


def main() -> int:
    args = parse_args()
    cards = read_cards(args.input)
    cards = select_card_range(cards, args.start, args.end)
    if not cards:
        raise SystemExit(f"No cards found in {args.input}")
    build_apkg(cards, args.output, args.deck_name, args.media_base)
    print(f"Wrote {len(cards)} cards to {args.output}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build an Anki .apkg deck from intermediate card JSONL.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="First-stage JSONL card file.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Output .apkg path.")
    parser.add_argument("--deck-name", default=DEFAULT_DECK_NAME, help="Deck name shown in Anki.")
    parser.add_argument("--media-base", type=Path, default=Path("."), help="Base path for relative audio file paths.")
    parser.add_argument("--start", type=int, default=1, help="1-based first card number to include.")
    parser.add_argument("--end", type=int, help="1-based last card number to include, inclusive.")
    return parser.parse_args()


def read_cards(path: Path) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                cards.append(json.loads(line))
            except json.JSONDecodeError as error:
                print(f"Skipping invalid JSONL line {line_number}: {error}")
    return cards


def select_card_range(cards: list[dict[str, Any]], start: int, end: int | None) -> list[dict[str, Any]]:
    if start < 1:
        raise SystemExit("--start must be 1 or greater")
    if end is not None and end < start:
        raise SystemExit("--end must be greater than or equal to --start")
    start_index = start - 1
    end_index = end if end is not None else None
    return cards[start_index:end_index]


def build_apkg(cards: list[dict[str, Any]], output_path: Path, deck_name: str, media_base: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        db_path = tmp / "collection.anki2"
        media_refs: dict[str, MediaRef] = {}
        create_collection(db_path, cards, deck_name, media_base, media_refs)
        write_package(output_path, db_path, media_refs)


def create_collection(
    db_path: Path,
    cards: list[dict[str, Any]],
    deck_name: str,
    media_base: Path,
    media_refs: dict[str, MediaRef],
) -> None:
    now_sec = int(time.time())
    base_id = int(time.time() * 1000)
    deck_id = base_id + 1
    model_id = base_id + 2
    config_id = base_id + 3

    con = sqlite3.connect(db_path)
    try:
        create_schema(con)
        con.execute(
            "insert into col values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                1,
                now_sec,
                now_sec,
                now_sec,
                11,
                0,
                -1,
                0,
                json.dumps(collection_config(deck_id, model_id), ensure_ascii=False),
                json.dumps({str(model_id): model(model_id, deck_id, now_sec)}, ensure_ascii=False),
                json.dumps(decks(deck_id, config_id, deck_name, now_sec), ensure_ascii=False),
                json.dumps(deck_configs(config_id, now_sec), ensure_ascii=False),
                "{}",
            ),
        )

        for index, card in enumerate(cards, start=1):
            note_id = base_id + 1000 + index
            card_id = base_id + 100000 + index
            fields = note_fields(card, media_base, media_refs)
            sfld = strip_html(fields[0])
            con.execute(
                "insert into notes values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    note_id,
                    guid_for(card, index),
                    model_id,
                    now_sec,
                    -1,
                    "",
                    FIELD_SEPARATOR.join(fields),
                    sfld,
                    checksum(sfld),
                    0,
                    "",
                ),
            )
            con.execute(
                "insert into cards values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    card_id,
                    note_id,
                    deck_id,
                    0,
                    now_sec,
                    -1,
                    0,
                    0,
                    index,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    "",
                ),
            )
        con.commit()
    finally:
        con.close()


def create_schema(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        create table col (
          id integer primary key,
          crt integer not null,
          mod integer not null,
          scm integer not null,
          ver integer not null,
          dty integer not null,
          usn integer not null,
          ls integer not null,
          conf text not null,
          models text not null,
          decks text not null,
          dconf text not null,
          tags text not null
        );
        create table notes (
          id integer primary key,
          guid text not null,
          mid integer not null,
          mod integer not null,
          usn integer not null,
          tags text not null,
          flds text not null,
          sfld integer not null,
          csum integer not null,
          flags integer not null,
          data text not null
        );
        create table cards (
          id integer primary key,
          nid integer not null,
          did integer not null,
          ord integer not null,
          mod integer not null,
          usn integer not null,
          type integer not null,
          queue integer not null,
          due integer not null,
          ivl integer not null,
          factor integer not null,
          reps integer not null,
          lapses integer not null,
          left integer not null,
          odue integer not null,
          odid integer not null,
          flags integer not null,
          data text not null
        );
        create table revlog (
          id integer primary key,
          cid integer not null,
          usn integer not null,
          ease integer not null,
          ivl integer not null,
          lastIvl integer not null,
          factor integer not null,
          time integer not null,
          type integer not null
        );
        create table graves (
          usn integer not null,
          oid integer not null,
          type integer not null
        );
        create index ix_notes_usn on notes (usn);
        create index ix_cards_usn on cards (usn);
        create index ix_revlog_usn on revlog (usn);
        create index ix_cards_nid on cards (nid);
        create index ix_cards_sched on cards (did, queue, due);
        create index ix_revlog_cid on revlog (cid);
        create index ix_notes_csum on notes (csum);
        """
    )


def collection_config(deck_id: int, model_id: int) -> dict[str, Any]:
    return {
        "timeLim": 0,
        "estTimes": True,
        "dueCounts": True,
        "dayLearnFirst": False,
        "newSpread": 0,
        "creationOffset": 0,
        "nextPos": 1,
        "schedVer": 2,
        "sortBackwards": False,
        "curModel": str(model_id),
        "collapseTime": 1200,
        "activeDecks": [1, deck_id],
        "addToCur": True,
        "curDeck": deck_id,
        "sortType": "noteFld",
    }


def decks(deck_id: int, config_id: int, deck_name: str, now_sec: int) -> dict[str, Any]:
    return {
        "1": {
            "id": 1,
            "mod": now_sec,
            "name": "Default",
            "usn": -1,
            "lrnToday": [0, 0],
            "revToday": [0, 0],
            "newToday": [0, 0],
            "timeToday": [0, 0],
            "collapsed": True,
            "browserCollapsed": True,
            "desc": "",
            "dyn": 0,
            "conf": 1,
            "extendNew": 0,
            "extendRev": 0,
        },
        str(deck_id): {
            "id": deck_id,
            "mod": now_sec,
            "name": deck_name,
            "usn": -1,
            "lrnToday": [0, 0],
            "revToday": [0, 0],
            "newToday": [0, 0],
            "timeToday": [0, 0],
            "collapsed": True,
            "browserCollapsed": True,
            "desc": "",
            "dyn": 0,
            "conf": config_id,
            "extendNew": 0,
            "extendRev": 0,
        },
    }


def deck_configs(config_id: int, now_sec: int) -> dict[str, Any]:
    default = deck_config(1, "Default", now_sec, 20)
    custom = deck_config(config_id, "4 new cards per day", now_sec, NEW_CARDS_PER_DAY)
    return {"1": default, str(config_id): custom}


def deck_config(config_id: int, name: str, now_sec: int, new_per_day: int) -> dict[str, Any]:
    return {
        "id": config_id,
        "mod": now_sec,
        "name": name,
        "usn": -1,
        "maxTaken": 60,
        "autoplay": True,
        "timer": 0,
        "replayq": True,
        "new": {
            "bury": False,
            "delays": [1.0, 10.0],
            "initialFactor": 2500,
            "ints": [1, 4, 0],
            "order": 1,
            "perDay": new_per_day,
        },
        "rev": {"bury": False, "ease4": 1.3, "ivlFct": 1.0, "maxIvl": 36500, "perDay": 200, "hardFactor": 1.2},
        "lapse": {"delays": [10.0], "leechAction": 0, "leechFails": 5, "minInt": 1, "mult": 0.0},
        "dyn": False,
        "newMix": 0,
        "newPerDayMinimum": 0,
        "interdayLearningMix": 0,
        "reviewOrder": 0,
        "newSortOrder": 0,
        "newGatherPriority": 0,
        "buryInterdayLearning": False,
    }


def model(model_id: int, deck_id: int, now_sec: int) -> dict[str, Any]:
    return {
        "id": model_id,
        "name": "Russian-English production",
        "type": 0,
        "mod": now_sec,
        "usn": -1,
        "sortf": 0,
        "did": deck_id,
        "latexPre": "\\documentclass[12pt]{article}\n\\begin{document}\n",
        "latexPost": "\\end{document}",
        "latexsvg": False,
        "req": [[0, "any", [0]]],
        "tags": [],
        "vers": [],
        "flds": [field(name, index) for index, name in enumerate(FIELDS)],
        "tmpls": [
            {
                "name": "Card 1",
                "ord": 0,
                "qfmt": FRONT_TEMPLATE,
                "afmt": BACK_TEMPLATE,
                "bqfmt": "",
                "bafmt": "",
                "did": None,
                "bfont": "Arial",
                "bsize": 20,
            }
        ],
        "css": CARD_CSS,
    }


def field(name: str, order: int) -> dict[str, Any]:
    return {"name": name, "ord": order, "sticky": False, "rtl": False, "font": "Arial", "size": 20, "description": "", "media": []}


FRONT_TEMPLATE = """
<div class="wrap front">
  <div class="ru-word">{{RussianWord}}</div>
  <div class="ru-example">{{RussianExample}}</div>
</div>
"""


BACK_TEMPLATE = """
<div class="wrap back">
  <div class="en-word">{{EnglishWord}}</div>
  <div class="ipa">{{EnglishIPA}}</div>

  <div class="audio-row">
    <span class="audio-control word-audio">{{WordAudio}}</span>
    <span class="audio-control example-audio">{{ExampleAudio}}</span>
  </div>

  <div class="en-example">{{EnglishExample}}</div>
  <div class="example-ipa">{{EnglishExampleIPA}}</div>

  <div class="notes">{{Notes}}</div>
</div>

<script>
var wordAudio = document.querySelector(".word-audio .soundLink, .word-audio .replaybutton");
if (wordAudio) { wordAudio.click(); }
</script>
"""


CARD_CSS = """
.card {
  background: #ffffff;
  color: #2f3440;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  font-size: 22px;
  line-height: 1.4;
  padding: 28px;
}

.wrap {
  max-width: 720px;
  margin: 0 auto;
  text-align: center;
}

.ru-word,
.en-word {
  font-size: 42px;
  font-weight: 700;
}

.ru-example {
  margin-top: 18px;
  color: #555d6b;
  font-size: 19px;
}

.ipa,
.example-ipa {
  color: #657080;
  font-size: 20px;
}

.audio-row {
  margin: 28px 0 18px;
}

.audio-control {
  display: inline-block;
  margin: 0 6px;
}

.en-example {
  margin-top: 20px;
  font-size: 25px;
}

.notes {
  margin-top: 32px;
  color: #8a919e;
  font-size: 15px;
  text-align: left;
}

.notes div {
  margin-top: 4px;
}

.replay-button,
.replaybutton {
  transform: scale(0.9);
}

.card.nightMode {
  background: #2f3440;
  color: #eceff4;
}

.nightMode .ru-example,
.nightMode .ipa,
.nightMode .example-ipa,
.nightMode .notes {
  color: #b8c0cc;
}
"""


def note_fields(card: dict[str, Any], media_base: Path, media_refs: dict[str, MediaRef]) -> list[str]:
    word_audio = sound_field(card.get("audio_file_path"), media_base, media_refs)
    example_audio = sound_field(card.get("english_example_audio_file_path"), media_base, media_refs)
    return [
        escape(card.get("main_translation") or first_text(card.get("russian_translations"))),
        russian_example_html(card),
        escape(card.get("word")),
        escape(card.get("english_ipa")),
        english_example_html(card),
        escape(card.get("english_example_ipa")),
        notes_html(card),
        word_audio,
        example_audio,
    ]


def russian_example_html(card: dict[str, Any]) -> str:
    text = str(card.get("russian_example_translation") or "")
    translations = [card.get("main_translation"), *list_value(card.get("russian_translations"))]
    highlighted = highlight_first_match(text, [str(item) for item in translations if item])
    if highlighted:
        return highlighted
    target = escape(card.get("main_translation") or first_text(card.get("russian_translations")))
    sentence = escape(text)
    return f"<strong>{target}</strong><span class=\"context-separator\"> — </span>{sentence}" if sentence else f"<strong>{target}</strong>"


def english_example_html(card: dict[str, Any]) -> str:
    text = str(card.get("english_example") or "")
    candidates = english_highlight_candidates(str(card.get("word") or ""))
    return highlight_first_match(text, candidates) or escape(text)


def english_highlight_candidates(word: str) -> list[str]:
    clean = word.strip()
    candidates = [clean]
    if clean.casefold().startswith("to "):
        base = clean[3:].strip()
        candidates.extend([base, third_person_singular(base)])
    return [candidate for candidate in candidates if candidate]


def third_person_singular(base: str) -> str:
    lower = base.casefold()
    if lower.endswith("y") and len(base) > 1 and lower[-2] not in "aeiou":
        return f"{base[:-1]}ies"
    if lower.endswith(("s", "sh", "ch", "x", "z", "o")):
        return f"{base}es"
    return f"{base}s"


def highlight_first_match(text: str, candidates: list[str]) -> str:
    escaped = escape(text)
    for candidate in sorted(set(candidates), key=len, reverse=True):
        pattern = re.compile(rf"(?<!\w)({re.escape(escape(candidate))})(?!\w)", re.IGNORECASE)
        if pattern.search(escaped):
            return pattern.sub(r"<strong>\1</strong>", escaped, count=1)
    return ""


def notes_html(card: dict[str, Any]) -> str:
    rows: list[str] = []
    collocations = list_value(card.get("common_collocations"))
    if collocations:
        rows.append(f"<div>Сочетания: {escape(', '.join(collocations))}</div>")
    pronunciation_notes = str(card.get("pronunciation_notes") or "").strip()
    if pronunciation_notes:
        rows.append(f"<div>Произношение: {escape(pronunciation_notes)}</div>")
    review_reason = str(card.get("review_reason") or "").strip()
    if card.get("needs_review") and review_reason:
        rows.append(f"<div>Проверьте: {escape(review_reason)}</div>")
    return "".join(rows)


def sound_field(path_value: Any, media_base: Path, media_refs: dict[str, MediaRef]) -> str:
    if not path_value:
        return ""
    source = Path(str(path_value))
    if not source.is_absolute():
        source = media_base / source
    if not source.is_file() or source.stat().st_size == 0:
        print(f"Missing audio file, skipping: {source}")
        return ""
    media_name = media_name_for(source, media_refs)
    return f"[sound:{media_name}]"


def media_name_for(source: Path, media_refs: dict[str, MediaRef]) -> str:
    key = str(source.resolve())
    existing = media_refs.get(key)
    if existing:
        return existing.name
    base_name = sanitize_media_name(source.name)
    used_names = {ref.name for ref in media_refs.values()}
    media_name = base_name
    if media_name in used_names:
        stem = Path(base_name).stem
        suffix = Path(base_name).suffix
        media_name = f"{stem}-{sha1(key.encode('utf-8')).hexdigest()[:8]}{suffix}"
    media_refs[key] = MediaRef(source=source, name=media_name)
    return media_name


def sanitize_media_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "-", name).strip()
    return cleaned or "audio.mp3"


def write_package(output_path: Path, db_path: Path, media_refs: dict[str, MediaRef]) -> None:
    media_items = list(media_refs.values())
    media_manifest = {str(index): ref.name for index, ref in enumerate(media_items)}
    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(db_path, "collection.anki2")
        zf.write(db_path, "collection.anki21")
        zf.writestr("media", json.dumps(media_manifest, ensure_ascii=False))
        for index, ref in enumerate(media_items):
            zf.write(ref.source, str(index))


def guid_for(card: dict[str, Any], index: int) -> str:
    raw = f"{index}:{card.get('source_word')}:{card.get('word')}:{card.get('english_example')}"
    digest = sha1(raw.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")[:12]


def checksum(value: str) -> int:
    return int(sha1(value.encode("utf-8")).hexdigest()[:8], 16)


def strip_html(value: str) -> str:
    return re.sub(r"<[^>]+>", "", value)


def escape(value: Any) -> str:
    return html.escape(str(value or ""), quote=False)


def first_text(value: Any) -> str:
    items = list_value(value)
    return items[0] if items else ""


def list_value(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


if __name__ == "__main__":
    raise SystemExit(main())

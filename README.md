# Anki Generator

First-stage pipeline for turning an English frequency list into validated Anki-ready JSONL card data.

## Setup

Install dependencies with uv:

```bash
uv sync
```

Create `secrets/openai.env` with this exact format:

```dotenv
OPENAI_API_KEY=sk-your-api-key-here
```

The script also reads an existing `OPENAI_API_KEY` environment variable, so the secrets file is optional if your shell already exports it.

## Generate the first 20 cards

The script is intentionally guarded so it will not process the full NGSL list by accident.

```bash
uv run anki-generate-cards --dry-run
```

Defaults:

- input: `input/NGSL_1.2_stats.csv`
- output/cache: `intermediate/cards.jsonl`
- error log: `intermediate/errors.log`
- audio files: `output/audio/`
- model: `gpt-5.4-mini`

The cache key is `source_word`, so a word already present in `intermediate/cards.jsonl` is skipped on later runs.

## Other inputs

CSV inputs use a `Lemma`, `word`, `term`, or first column as the word. If a part-of-speech column exists, name it `part_of_speech`, `part of speech`, or `pos`.

TXT inputs should contain one word per line:

```bash
uv run anki-generate-cards input/my_words.txt --dry-run
```

For custom CSV columns:

```bash
uv run anki-generate-cards input/my_words.csv --word-column Word --pos-column POS --dry-run
```

## Batch request mode

To write OpenAI Batch API JSONL requests instead of calling the model:

```bash
uv run anki-generate-cards --batch --dry-run
```

This writes `intermediate/batch_requests.jsonl` for the `/v1/responses` endpoint. Upload it with purpose `batch`, then create a batch with endpoint `/v1/responses`.

## Full run

Only after inspecting the first 20 generated cards, run:

```bash
uv run anki-generate-cards --confirm-full-run
```

Useful safety options:

```bash
uv run anki-generate-cards --limit 100
uv run anki-generate-cards --skip-audio --dry-run
```

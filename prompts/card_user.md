Create card data from this payload:

{{PAYLOAD_JSON}}

Use the Wiktionary/Kaikki fields as evidence:
- preferred_ipa is the first choice for english_ipa when present and suitable.
- ipa_candidates may contain alternatives; choose American or untagged entries only.
- preferred_audio_url is handled by the pipeline, but keep audio fields as empty strings and audio_source as "not_checked".
- english_example_audio_file_path is handled by the pipeline; keep it empty and english_example_audio_source as "not_checked".
- pos_candidates and glosses can help infer part_of_speech and common meanings.

Return exactly the schema object with an entries array.

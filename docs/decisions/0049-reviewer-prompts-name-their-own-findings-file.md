# 0049 Reviewer prompts name their own findings file
Status: accepted 2026-11-24
Decision: Each generated reviewer prompt spells out its own findings/<member>.json, and ingest lists any other .json file in findings/ as stray.
Why: The <your-name> placeholder left the file name to the model; four times in 27 reviews a language reviewer wrote another name, ingest called it missing and its findings were lost (#105).
Rejected: Teaching ingest to guess a reviewer from a stray file name: a guess could attach findings to the wrong member.

# Replay labels

`hermetic-pr2.json` lists the 17 findings that code-check made, round after round, on
`subhiandrews/hermetic` PR 2 (a 1,200-line scaffold). Every one of them had a site in the first reviewed head
(`7ec839e`), so a single complete review could have reported all of them at once. `round` is when the PR's review
first reported it and `fixed_in` is the commit that resolved it. `hermetic-pr6.json` does the same for PR 6 (26 findings in seven rounds, base `1b9acf3031254df3461d919eef6a0f3022f211f2`, heads `8495805 650b862 ae6e19d 02db36b ce3a32d b49a08f ee7be3b`). The `kind` calls (`tail` in particular) are the maintainer's judgement. It is a label file only: the code stays in the clone. The `kind` values and how the score reads them are in the "PR replay" section of `docs/MAINTAINING.md`.

`hermetic-pr8.json` lists 24 findings the reviews of PR 8 (secret redaction) made over 13 rounds, every one with a site in the first head `5285f7a` (base `94bcbe9758c64be7ee1fbad59853b136f0089b3d`); nine of them are routes by which a secret reaches stored or printed output. Replay only that first head.

For `hermetic-pr2.json` the base is `593ffc7ab13eeedf1e52130b884caa7f81746494` and the heads are `7ec839e fed3f54 2f1d453`.
See the "PR replay" section of `docs/MAINTAINING.md` for the commands and the score.

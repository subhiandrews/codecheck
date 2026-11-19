# Third-party material

The golden eval set in `tests/golden/` contains pull request diffs taken from the open-source projects below, each
under a license that allows redistribution with attribution. The diffs are used only as test inputs for the reviewer.
They remain the property of their authors and keep their original license; nothing here changes that. The full
license text of each project is in `licenses/`, and the copyright notice below is reproduced from that project.

`shadow promote --finalize` refuses to add a case unless its repository's license is on the allowed list
(`golden.licenses`; by default MIT, Apache-2.0, BSD-2-Clause, BSD-3-Clause, ISC, 0BSD and Unlicense). Add the project
to the table and its license text to `licenses/` when you add its first case; a test fails until you do.

| Project | License | Copyright notice | License text |
|---|---|---|---|
| [google/guava](https://github.com/google/guava) | Apache-2.0 | Copyright (C) The Guava Authors | [licenses/Apache-2.0-google-guava.txt](licenses/Apache-2.0-google-guava.txt) |
| [tokio-rs/tokio](https://github.com/tokio-rs/tokio) | MIT | Copyright (c) Tokio Contributors | [licenses/MIT-tokio-rs-tokio.txt](licenses/MIT-tokio-rs-tokio.txt) |

`tests/golden/seed-pipeline.json` is a case written for this project and is not taken from any repository.

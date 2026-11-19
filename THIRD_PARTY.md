# Third-party material

The golden eval set in `tests/golden/` contains pull request diffs taken from the open-source projects below, each
under a license that allows redistribution with attribution. The diffs are used only as test inputs for the reviewer.
They remain the property of their authors and keep their original license; nothing here changes that.

`shadow promote --finalize` refuses to add a case unless its repository's license is on the allowed list
(`golden.licenses`; by default MIT, Apache-2.0, BSD-2-Clause, BSD-3-Clause, ISC, 0BSD and Unlicense). Add the project
here when you add its first case; a test fails until you do.

| Project | License | Copyright and notice |
|---|---|---|
| [google/guava](https://github.com/google/guava) | Apache-2.0 | Copyright The Guava Authors. Licensed under the Apache License, Version 2.0: https://www.apache.org/licenses/LICENSE-2.0 |
| [tokio-rs/tokio](https://github.com/tokio-rs/tokio) | MIT | Copyright (c) Tokio Contributors. Licensed under the MIT License: https://github.com/tokio-rs/tokio/blob/master/LICENSE |

`tests/golden/seed-pipeline.json` is a synthetic case written for this project (`"origin": "synthetic"`) and is not
taken from any repository.

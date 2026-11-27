# 0050 Address-comments reports failing CI, babysit-pr fixes it
Status: superseded by 0060 (accepted 2026-11-24)
Decision: address-comments names failing checks in its report (and when it has nothing to address) but does not fix them; babysit-pr keeps the CI fixes and goes on to them after address-comments stops.
Why: A PR with only a failing check got no mention from address-comments, and its 'nothing to address' stop could be read as ending a babysit run (#106).
Rejected: Moving CI fixing into address-comments: it would duplicate the attempt limit and the caused-by-this-PR judgement.

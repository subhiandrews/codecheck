"""Behavioral suite every provider adapter must pass. Subclass and implement make_provider()."""

from codecheck.models import Check, PostResult, PRRef, PullRequest, Thread

VALID_CHECK_STATES = {"pass", "fail", "pending", "skipped", "cancelled"}


class ProviderContract:
    def make_provider(self):  # -> tuple[Provider, PRRef]
        raise NotImplementedError

    def test_whoami(self):
        provider, _ = self.make_provider()
        assert isinstance(provider.whoami(), str) and provider.whoami()

    def test_get_pr(self):
        provider, ref = self.make_provider()
        pr = provider.get_pr(ref)
        assert isinstance(pr, PullRequest) and pr.ref == ref
        assert pr.state in {"open", "closed", "merged"} and pr.head_sha

    def test_changed_files_and_refspec(self):
        provider, ref = self.make_provider()
        files = provider.list_changed_files(ref)
        assert files and all(isinstance(f, str) for f in files)
        refspec, local_ref = provider.fetch_refspec(ref)
        assert refspec and local_ref.startswith("refs/")

    def test_threads(self):
        provider, ref = self.make_provider()
        threads = provider.list_threads(ref)
        assert all(isinstance(t, Thread) and t.id and t.comments for t in threads)

    def test_checks_have_valid_states(self):
        provider, ref = self.make_provider()
        checks = provider.get_checks(ref)
        assert all(isinstance(c, Check) and c.state in VALID_CHECK_STATES for c in checks)

    def test_repo_from_remote_and_clone_argv(self):
        provider, _ = self.make_provider()
        assert provider.repo_from_remote("::: not a url") is None or isinstance(provider.repo_from_remote("x"), str)
        argv = provider.clone_argv("o/r", "/tmp/dest")
        assert argv and argv[-1] == "/tmp/dest" and "o/r" in " ".join(argv)

    def test_write_side_does_not_raise(self):
        from codecheck.models import Decision, ReviewComment

        provider, ref = self.make_provider()
        pr = provider.get_pr(ref)
        thread = provider.list_threads(ref)[0]
        result = provider.post_review(
            ref, pr.head_sha, "summary", [ReviewComment("a.py", 3, "RIGHT", "body")], Decision("COMMENT", "r")
        )
        assert isinstance(result, PostResult)
        provider.reply(ref, thread, "thanks")
        provider.resolve(thread)
        provider.request_review(ref, ["alice"])
        provider.add_label(ref, "code-check")
        assert isinstance(ref, PRRef)

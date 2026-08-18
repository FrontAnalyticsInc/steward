"""What makes a reply read as a reply, on both sides of the wire.

Gmail threads our own drafts by `threadId`, which means every mistake in the
RFC headers is invisible from the sending mailbox — the draft looks perfectly
threaded in Gmail while arriving detached for anyone on a client that honours
References. These tests exist because that failure has no local symptom.
"""

from __future__ import annotations

from app import mailer_as_principal as M


class TestReplySubject:
    def test_a_reply_is_prefixed(self):
        message = M.build_message(
            to="them@example.com",
            subject="Quarterly numbers",
            body="b",
            in_reply_to="<parent@mail.example.com>",
        )
        assert message["Subject"] == "Re: Quarterly numbers"

    def test_the_prefix_is_not_stacked(self):
        """A thread must not accumulate `Re: Re: Re:` over several rounds."""
        message = M.build_message(
            to="them@example.com",
            subject="Re: Quarterly numbers",
            body="b",
            in_reply_to="<parent@mail.example.com>",
        )
        assert message["Subject"] == "Re: Quarterly numbers"

    def test_an_existing_prefix_is_recognised_whatever_its_case(self):
        for given in ("RE: thing", "re: thing", "Re:thing", " re : thing"):
            message = M.build_message(
                to="t@example.com", subject=given, body="b", in_reply_to="<p@x.com>"
            )
            assert message["Subject"].lower().count("re") >= 1
            assert not message["Subject"].lower().startswith("re: re")

    def test_the_rest_of_the_subject_is_untouched(self):
        """Gmail requires a message posted to a threadId to carry that thread's
        subject. Rewriting anything but the prefix would detach the draft."""
        message = M.build_message(
            to="t@example.com",
            subject="[Ext] Invoice #4471 — Acme",
            body="b",
            in_reply_to="<p@x.com>",
        )
        assert message["Subject"] == "Re: [Ext] Invoice #4471 — Acme"

    def test_a_new_message_is_not_prefixed(self):
        message = M.build_message(to="t@example.com", subject="Hello", body="b")
        assert message["Subject"] == "Hello"

    def test_an_empty_subject_stays_empty_rather_than_becoming_re(self):
        message = M.build_message(
            to="t@example.com", subject="", body="b", in_reply_to="<p@x.com>"
        )
        assert message["Subject"] == ""


class TestReplyHeaders:
    def test_both_threading_headers_are_set(self):
        message = M.build_message(
            to="t@example.com",
            subject="s",
            body="b",
            in_reply_to="<parent@mail.example.com>",
        )
        assert message["In-Reply-To"] == "<parent@mail.example.com>"
        assert message["References"] == "<parent@mail.example.com>"

    def test_a_new_message_carries_neither(self):
        message = M.build_message(to="t@example.com", subject="s", body="b")
        assert message["In-Reply-To"] is None
        assert message["References"] is None


class TestNormalizeCarriesTheRfcMessageId:
    def test_the_header_is_extracted_alongside_the_api_id(self):
        from app import gmail_api

        raw = {
            "id": "19a3f0c2ab",
            "threadId": "19a3f0c200",
            "payload": {
                "headers": [
                    {"name": "Message-ID", "value": "<CAF=abc@mail.gmail.com>"},
                    {"name": "From", "value": "Jane <jane@example.com>"},
                    {"name": "Subject", "value": "hi"},
                ]
            },
        }
        out = gmail_api._normalize(raw)
        # The two must both survive and stay distinct: one addresses the Gmail
        # API, the other addresses every other mail system on earth.
        assert out["id"] == "19a3f0c2ab"
        assert out["rfc_message_id"] == "<CAF=abc@mail.gmail.com>"

    def test_a_message_without_the_header_normalizes_to_none(self):
        from app import gmail_api

        out = gmail_api._normalize({"id": "x", "threadId": "t", "payload": {"headers": []}})
        assert out["rfc_message_id"] is None

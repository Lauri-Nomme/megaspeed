# 79 — Author notes are not review remarks

Status: **draft; needs sign-off.**

## Problem

The largest gold-nofit family (39 of 97) is an **author explaining or
justifying their own change**. No text pattern should catch it: the text is
ordinary prose, and a pattern broad enough to match it would also flag real
review remarks. But it is one of the deterministic signals we already have:
**the opener's author is the PR author.** An author's own note is not a review
remark and should not enter the comment tax.

`is_notice` (prd/78) deliberately does not target this; 0 false positives is
worth more than catching author prose with a fuzzy rule.

## Rule

For an **opener** only:

- `actor == PR author` (pseudonyms, both already deterministic), and
- the text is **not a question or a request to reviewers**: no real `?` outside
  URLs/code (`citext.has_real_question`) and no question/request opener
  (`why/what/how/should/could/can/is/are/does/do/where/when/who/please review…`),
- then `comment_kind=admin` (the existing kind; a distinct `author_note` is a
  later refinement), **no** `comment_type`, **no** model call.

Replies stay exempt (prd/75/78): an author status note as a reply is
intent/outcome evidence, not an opener.

## Consequences

- The comment tax drops the author's own threads; review remarks by others stay.
- An author's **question** to reviewers stays `review` (the guard), even though
  it is by the author: it is still a question about the code.
- A DC-mandatory thread is never affected: it has no author-note path.
- The outcome of an author-note thread is not computed (no `comment_type`); its
  replies remain in the log for intent work.

## Store migration

`recategorize` compares the opener actor (from the `comment_added` event) to the
PR object's `actor` (the author). The question guard needs the opener text, so
the existing `--trace` is still required; a thread without text stays `review`
(the safe default).

## Acceptance

- On the frozen gold, the author-explanation family (A) is caught; **0 reviewer
  remarks** are caught; author questions are not caught.
- On a recategorize dry run: the new `admin` count equals the author-opener
  count (minus questions), reported separately; a second apply changes nothing.
- Tests: author explanation → admin; author question → review; reviewer opener →
  review; author reply → not admin (reply exemption).

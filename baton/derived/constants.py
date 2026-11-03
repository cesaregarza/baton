"""Kinds, scope, and representation limits shared by the indexing contract."""

KINDS = ("summary", "decisions", "open_questions")
# A summary of only the latest messages, off unless `[search] rolling_window_messages` is set. Its window starts
# from the end of the conversation, so it never counts toward any kind's coverage.
RECENT = "summary_recent"
SCOPE = "all"  # a local producer reads the host's own database: every conversation
ITEM_REVISION = 1
# Why an item may go without a vector. `empty_text` is checked; the others are the producer's word.
SKIP_REASONS = ("empty_text", "over_token_limit", "excluded_kind")
UNIT_TOLERANCE = 1e-3  # messages are immutable; a later edit would raise this
MAX_REQUEST_ID = 200

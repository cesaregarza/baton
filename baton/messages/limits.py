"""Mailbox admission and delivery page bounds, shared by direct and group messages."""

MAX_UNREAD = 500
MAX_PAGE_MESSAGES = 100
# MCP carries structured data and a text copy, so page sizing doubles the JSON size.
MAX_PAGE_BYTES = 512 * 1024

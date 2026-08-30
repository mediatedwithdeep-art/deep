"""Shared domain logic for the Sentinel VMS.

Everything in here is used by more than one service (backend API, AI
pipeline, video ingestion, event processor). Anything used by exactly one
service belongs in that service, not here -- a shared package that accretes
single-consumer code becomes a distributed monolith.
"""
__version__ = "1.0.0"

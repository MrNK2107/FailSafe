"""
FailSafe - a gated, audited revenue-recovery agent for Razorpay.

The LLM proposes; deterministic code decides. Every proposal passes through
a policy gate before anything touches Razorpay, and every step is audited.

Layout: this package contains all pipeline code. Datasets live in
``data/``, merchant-editable policy tables in ``config/``, audit trails in
``logs/`` - all anchored by :mod:`failsafe.paths`.

Run any pipeline stage as a module, e.g.::

    python -m failsafe generate-data
    python -m failsafe agent --inject-failure llm_parse_failure
    python -m failsafe integrated [N]
"""

__version__ = "1.0.0"

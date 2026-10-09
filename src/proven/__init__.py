"""PROVEN — clause accounting and settlement arithmetic for SME insurance.

Non-negotiable design rule (technical-implementation.md §14):
  1. The model never decides anything consequential. It locates and types text.
  2. Never state a figure whose basis is UNESTABLISHED.
  3. Never assert breach. Locate, quote, escalate.

Precedence resolution and all arithmetic are ordinary deterministic code.
"""

__version__ = "1.0.0"

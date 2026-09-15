# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Read these first

**`AGENTS.md`** — operating instructions for agents in this repo: what needs
human sign-off, what needs an ADR, directory and testing conventions, and the
distinction between payroll-affecting code and everything else.

**`SOUL.md`** — the rules that settle arguments. Short.

`README.md` lists the rest of the document set.

## Status

**M0 closed, M1 (rig + ingest) and M2 (backend abstraction + parity) implemented
(2026-09-16); the Linux/CPU leg is verified.** No payroll logic exists yet.
Build/lint/test commands are in `AGENTS.md` ("Commands") and `DEV_SETUP.md`;
technology choices are recorded in `DECISIONS.md` (ADR-0009 through 0018 and
0025/0026 accepted; the rest are open with leanings).

## The one thing to get right

Some code paths can reduce a worker's pay (canteen pairing, dwell, overage,
export). They are held to a higher standard than everything else: full case
coverage, property tests, human review, ADR before behaviour changes. If you
cannot tell whether you are in one, stop and find out — do not guess.
`AGENTS.md` §1.

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Read these first

**`AGENTS.md`** — operating instructions for agents in this repo: what needs
human sign-off, what needs an ADR, directory and testing conventions, and the
distinction between payroll-affecting code and everything else.

**`SOUL.md`** — the rules that settle arguments. Short.

`README.md` lists the rest of the document set.

## Status

**Documentation only. No code, no scaffolding, no configuration exists** as of
2026-09-13. Technology choices are open — see `DECISIONS.md` before assuming a
stack. Build, lint and test commands are not documented because there is nothing
to build yet; re-run `/init` once the first code lands.

## The one thing to get right

Some code paths can reduce a worker's pay (canteen pairing, dwell, overage,
export). They are held to a higher standard than everything else: full case
coverage, property tests, human review, ADR before behaviour changes. If you
cannot tell whether you are in one, stop and find out — do not guess.
`AGENTS.md` §1.

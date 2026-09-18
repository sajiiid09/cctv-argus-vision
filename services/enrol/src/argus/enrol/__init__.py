"""argus.enrol -- the only way a face template comes into existence.

Every write here names a human. `--by` is required on every subcommand that
changes anything and has no default: never `$USER`, never `getlogin()`. An
inferred actor is a fake audit trail, and RISKS.md §10 puts enrolment above the
tier table precisely because it needs a named person behind it.

Consent is enforced twice -- once here for the error message, once by
`face_template.consent_id NOT NULL` for the guarantee (ADR-0027).
"""

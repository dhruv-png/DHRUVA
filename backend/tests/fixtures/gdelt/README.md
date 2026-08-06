# GDELT fixtures

**Provenance: hand-constructed, not captured from a live response.**

DHRUVA's build environment has no route to `api.gdeltproject.org`, so these
files were written by hand against the field names and formats documented at
<https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/> for
`mode=artlist&format=json`. They are representative of the documented schema;
they are not evidence about what the live endpoint returns today.

Every domain is under a `.test` name reserved by RFC 2606, every headline is
invented, and no real publisher, URL or article is referenced. Nothing here is
copied from any third party's content.

The first real response the owner captures during the live smoke test should
replace `artlist_sanitized.json` after sanitising publisher names and URLs, at
which point this note should record the capture date.

# Certificate lifecycle presentation

Domain details display recorded origin certificate expiry, renewal state, last
update and next scheduled check. Completed checks are not described as newly
issued certificates. Missing attempts remain explicitly unrecorded. Retry and
expiry states have distinct notices; private backend error strings are not shown.
The Open application gate requires both unexpired certificate and fresh external
verification. Local clock updates remove stale readiness without page refresh.

The shared Hint now consumes Escape while its tooltip is open so that the enclosing
dialog stays open. Explicit Close still dismisses the dialog. This was found during
rendered keyboard testing, reproduced and rechecked after the shared fix.

Browser validation used the existing local product runtime on port 19191, with an
explicit captured-domain fixture and synthetic retry/expiry states. No provider
or production action was connected. Desktop and 390x844 mobile review verified
date wrapping, fixed footer, 358px dialog client/scroll width, touch hint, keyboard
Escape, expired warning and absent application link. The temporary fixture record
was removed afterwards. This is presentation acceptance, not live scheduler
delivery to this preview instance or production acceptance.

Screenshots: `/tmp/monitor-renewal-desktop.png`,
`/tmp/monitor-renewal-mobile.png`, `/tmp/monitor-renewal-expired-mobile.png`.
All 75 frontend tests pass and Vite build succeeds. The existing large main-bundle
warning remains. Shared hint siblings were code-reviewed; other modal consumers
were not individually exercised in this slice.

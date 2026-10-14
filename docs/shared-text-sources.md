# Shared text sources preparation

Compared against the text entries in the 2026-10-13 shared list. Existing entries
cover Dario Amodei, Shunyu Yao, Lilian Weng, NFX, First Round, Chemistry VC,
Compound VC, Every, SemiAnalysis and Stratechery. First Round uses its public
Review AI index rather than the corporate landing page.

Added public text discovery for [Latent Space](https://www.latent.space/feed)
and [Madrona](https://www.madrona.com/). Their existing podcast RSS entries are
preserved. Latent Space uses `free_only` to reject paid previews. Madrona uses
homepage article links because its `/feed/` returned HTTP 403; the homepage
only exposes a small set of featured articles, so this is not exhaustive coverage.

Andrew Ng's LinkedIn activity URL is recorded with `enabled: false`: unauthenticated
requests returned HTTP 999, and no verified public full-text adapter is available.
Enabling the global flag does not enable this entry. Video-only sources and the
generic CEO interview category require no text entry.

After this PR is merged and the intended daily layout deployment is complete,
set `NEWS_OFFICER_TEXT_SOURCES=true` in the runtime environment. No additional
variable is required for these sources. Default/unset/false leaves text discovery
inactive. Roll back with `NEWS_OFFICER_TEXT_SOURCES=false`.

Enabling activates all 12 enabled text entries and adds HTTP requests, stored
article text and downstream scoring/summarization costs. Website layout changes,
paywalls and access restrictions can yield empty scans or source health failures.
Existing text/podcast overlap may produce separate candidates. Undated articles
follow the existing first-seen behavior from #65. Monitor scan health and article
counts on the first run. No deployment or message delivery was performed here.

Local validation: offline flag-off byte equality and no-load/no-fetch tests;
flag-on discovery fixtures for both new entries; real public pages fetched and
parsed into full-text records. The workspace DNS caused the production downloader's
public-address check to reject these hosts, so live parsing used an isolated
test fetch replacement. Production download protection was not changed; runtime
connectivity remains to be verified after activation.
